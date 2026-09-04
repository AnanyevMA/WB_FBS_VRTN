"""
Adversarial Challenge & Empirical Boundary Test Suite for Milestone M1.
Challenger: teamwork_preview_challenger_r3_m1_2

Focus Areas:
1. Multi-scenario stress test of Order #5647931541 normalization.
2. LP_RETURN document builder ki formatting (must be strictly 31 chars).
3. Database batch normalization on large synthetic dirty order sets (idempotency, completeness).
4. Extreme boundary conditions: empty, whitespace, control chars, non-digit GTINs, ReDoS/huge strings, injection attacks.
5. Ingestion validation and DB healing verification.
"""
import time
import json
import uuid
import random
from datetime import datetime, timezone
import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, func

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizProductInfo
from app.services.encryption import encrypt
from app.services.kiz_service import (
    normalize_kiz_light_industry,
    parse_kiz_code,
    normalize_existing_orders_kiz,
)
from app.services.cz_client import CZClient


CANONICAL_GTIN = "04630199251318"
CANONICAL_SERIAL = "5QTSRh>4sVc+."
CANONICAL_31_CIS = f"01{CANONICAL_GTIN}21{CANONICAL_SERIAL}"


# ==============================================================================
# 1. Order #5647931541 Multi-Scenario Adversarial Stress Tests
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("dirty_variant,expected_serial", [
    (f"0104630199251318215QTSRh>4sVc+. 91EE12 92{uuid.uuid4().hex[:12]}", "5QTSRh>4sVc+."),
    (f"0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92{uuid.uuid4().hex[:12]}", "5QTSRh>4sVc+."),
    (f"0104630199251318215QTSRh>4sVc+.\u001d91EE12\u001d92{uuid.uuid4().hex[:12]}", "5QTSRh>4sVc+."),
    (f"0104630199251318215QTSRh>4sVc+.91ffd92{uuid.uuid4().hex[:8]}", "5QTSRh>4sVc+."),
    (f"0104630199251318215QTSRh>4sVc+.91ABCD92{uuid.uuid4().hex[:16]}", "5QTSRh>4sVc+."),
    (f"(01)04630199251318(21)5QTSRh>4sVc+.(91)EE12(92)xyz123", "5QTSRh>4sVc+."),
    (f"04630199251318215QTSRh>4sVc+. 91EE12 92xyz123", "5QTSRh>4sVc+."),
    (f"\x1d0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz   ", "5QTSRh>4sVc+."),
    (f"0104630199251318215QTSRh>4sVc+.\t91EE12\t92xyz", "5QTSRh>4sVc+."),
    ("0104630199251332215ZEdKVT_00e737", "5ZEdKVT_00e73"),  # 14-char serial truncated to 13
    (f"0104630199251318215QTSRh>4sVc+. 91EE1292xyz", "5QTSRh>4sVc+."),
    (f"  0104630199251318215QTSRh>4sVc+.91ABCD  ", "5QTSRh>4sVc+."),
])
async def test_order_5647931541_dirty_variants(dirty_variant, expected_serial):
    """
    Empirically creates order #5647931541 in the DB with dirty KIZ,
    executes normalize_existing_orders_kiz, and validates strict 31-char result.
    """
    await init_db()
    target_order_id = 5647931541
    gtin = "04630199251332" if "04630199251332" in dirty_variant else "04630199251318"

    async with AsyncSessionLocal() as session:
        # Create seller if not exists
        seller = await session.get(Seller, "test-seller-5647931541")
        if not seller:
            seller = Seller(
                id="test-seller-5647931541",
                name="Test Seller 5647931541",
                wb_supplier_id="WB-5647931541",
                cz_inn="7700123456",
                wb_api_token_encrypted=encrypt("wb_test"),
                cz_token_encrypted=encrypt("cz_test"),
                is_active=True,
            )
            session.add(seller)
            await session.commit()

        # Get or create target order #5647931541
        order = await session.get(Order, target_order_id)
        if not order:
            order = Order(
                id=target_order_id,
                seller_id=seller.id,
                status=OrderStatus.ASSEMBLING,
                wb_created_at=datetime.now(timezone.utc),
                article="test.article",
                tech_size="M",
                name="Капор утепленный",
                kiz_required=True,
                kiz_status=KizStatus.ERROR,
                kiz_code=dirty_variant,
            )
            session.add(order)
        else:
            order.kiz_code = dirty_variant
            order.kiz_status = KizStatus.ERROR

        # Also get or create KPI
        res_kpi = await session.execute(
            select(KizProductInfo).where(KizProductInfo.order_id == target_order_id)
        )
        kpi = res_kpi.scalars().first()
        if not kpi:
            # Delete any orphaned KPI with same kiz_code to avoid unique constraint
            orphans = await session.execute(
                select(KizProductInfo).where(KizProductInfo.kiz_code == dirty_variant)
            )
            for o_kpi in orphans.scalars().all():
                await session.delete(o_kpi)

            kpi = KizProductInfo(
                id=str(uuid.uuid4()),
                kiz_code=dirty_variant,
                gtin="04630199251318",
                clean_cis=dirty_variant,
                order_id=target_order_id,
                seller_id=seller.id,
            )
            session.add(kpi)
        else:
            kpi.kiz_code = dirty_variant
            kpi.clean_cis = dirty_variant

        await session.commit()

        # Run database normalization routine
        norm_result = await normalize_existing_orders_kiz(db=session, target_order_id=target_order_id)

        assert norm_result["target_found"] is True
        assert norm_result["target_normalized"] is True

        # Refresh order from DB
        await session.refresh(order)
        cleaned_order_kiz = order.kiz_code

        # Strict checks on length and structure
        assert len(cleaned_order_kiz) == 31, f"Expected 31 chars, got {len(cleaned_order_kiz)}: {cleaned_order_kiz}"
        assert cleaned_order_kiz.startswith(f"01{gtin}21{expected_serial}")
        assert cleaned_order_kiz[16:18] == "21"
        assert len(cleaned_order_kiz[18:]) == 13
        assert " " not in cleaned_order_kiz
        assert "\x1d" not in cleaned_order_kiz
        assert "91" not in cleaned_order_kiz[18:] or expected_serial.startswith("91") is False

        # Refresh KPI
        await session.refresh(kpi)
        assert len(kpi.clean_cis) == 31
        assert kpi.clean_cis == cleaned_order_kiz


# ==============================================================================
# 2. LP_RETURN Document Builder ki Formatting Stress Tests
# ==============================================================================

def test_lp_return_document_ki_formatting_kiz_codes():
    """
    Tests _build_return_document using kiz_codes argument with diverse dirty inputs.
    Verifies that every ki in products_list is strictly 31 characters.
    """
    client = CZClient(inn="190207495060", token="mock-cz-token")
    dirty_codes = [
        "0104630199251318215QTSRh>4sVc+. 91EE12 92xyz123456789",
        "0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz123",
        "0104630199251318215QTSRh>4sVc+.\u001d91EE12\u001d92xyz123",
        "0104630199251318215QTSRh>4sVc+.91ffd92qwe",
        "0104630199251318215QTSRh>4sVc+.91ABCD92xyz",
        "(01)04630199251318(21)5QTSRh>4sVc+.(91)EE12(92)xyz",
        "04630199251318215QTSRh>4sVc+. 91EE12 92xyz",
        "\x1d0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz",
        "0104630199251332215ZEdKVT_00e737",
        "0104630199251318215QTSRh>4sVc+.\t91EE12\t92xyz",
    ]

    doc = client._build_return_document(
        kiz_codes=dirty_codes,
        primary_document_number="5647931541",
        primary_document_type="RECEIPT",
        certificate_type="CONFORMITY_DECLARATION",
        certificate_number="ЕАЭС N RU Д-RU.РА05.В.88154/22",
        certificate_date="29.08.2022",
    )

    assert doc["type"] == "LP_RETURN"
    payload = json.loads(doc["productDocument"])
    assert payload["trade_participant_inn"] == "190207495060"
    assert payload["return_type"] == "REMOTE_SALE_RETURN"
    assert payload["paid"] is True

    products = payload["products_list"]
    assert len(products) == len(dirty_codes)

    for i, prod in enumerate(products):
        ki = prod["ki"]
        assert len(ki) == 31, f"Product {i} has invalid ki length {len(ki)}: {ki}"
        assert ki.startswith("01")
        assert ki[2:16].isdigit()
        assert ki[16:18] == "21"
        assert len(ki[18:]) == 13
        assert prod["primary_document_type"] == "RECEIPT"
        assert prod["primary_document_number"] == "5647931541"
        assert prod["certificate_number"] == "ЕАЭС N RU Д-RU.РА05.В.88154/22"


def test_lp_return_document_ki_formatting_items_parameter():
    """
    Tests _build_return_document using items argument with both 'ki' and 'kiz_code' keys.
    """
    client = CZClient(inn="190207495060", token="mock-cz-token")
    items = [
        {"ki": "0104630199251318215QTSRh>4sVc+. 91EE12 92xyz123456789", "receipt_number": "R-101"},
        {"kiz_code": "0104630199251318215QTSRh>4sVc+.91ffd92qwe", "primary_document_number": "R-102"},
        {"ki": "(01)04630199251318(21)5QTSRh>4sVc+.(91)EE12(92)xyz", "receipt_number": "R-103"},
        {"kiz_code": "0104630199251332215ZEdKVT_00e737", "receipt_number": "R-104"},
    ]

    doc = client._build_return_document(items=items)
    payload = json.loads(doc["productDocument"])
    products = payload["products_list"]
    assert len(products) == 4

    for prod in products:
        assert len(prod["ki"]) == 31
        assert prod["ki"].startswith("01")
        assert prod["ki"][2:16].isdigit()
        assert prod["ki"][16:18] == "21"
        assert len(prod["ki"][18:]) == 13


# ==============================================================================
# 3. Database Batch Normalization & Idempotency Stress Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_database_normalization_batch_stress():
    """
    Creates 40 dirty orders with varying lengths (32, 39, 45, 53) and executes
    normalize_existing_orders_kiz. Verifies 100% resolution and zero remaining >31 chars.
    Then executes a second pass to assert idempotency.
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        seller_id = f"batch-seller-{uuid.uuid4().hex[:6]}"
        seller = Seller(
            id=seller_id,
            name="Batch Test Seller",
            wb_supplier_id=f"WB-{seller_id[:8]}",
            cz_inn="7700998877",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True,
        )
        session.add(seller)

        tok = uuid.uuid4().hex[:6]
        created_order_ids = []
        dirty_templates = [
            lambda i: f"0104630199251318215QTSRh>4sVc+. 91EE12 92{tok}{i}",
            lambda i: f"0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92{tok}{i}",
            lambda i: f"0104630199251318215QTSRh>4sVc+.91ffd92{tok[:4]}{i}",
            lambda i: f"0104630199251332215ZEdKVT_{tok[:4]}{i:02d}",
        ]

        for i in range(40):
            oid = random.randint(810000000, 899999999)
            while oid in created_order_ids:
                oid = random.randint(810000000, 899999999)
            created_order_ids.append(oid)
            raw_kiz = dirty_templates[i % len(dirty_templates)](i)
            o = Order(
                id=oid,
                seller_id=seller_id,
                status=OrderStatus.NEW,
                wb_created_at=datetime.now(timezone.utc),
                article=f"art.{i}",
                tech_size="L",
                name=f"Товар {i}",
                kiz_required=True,
                kiz_status=KizStatus.ATTACHED,
                kiz_code=raw_kiz,
            )
            session.add(o)

            kpi = KizProductInfo(
                id=str(uuid.uuid4()),
                kiz_code=raw_kiz,
                gtin="04630199251318",
                clean_cis=raw_kiz,
                order_id=oid,
                seller_id=seller_id,
            )
            session.add(kpi)

        await session.commit()

        # Run Pass 1
        res1 = await normalize_existing_orders_kiz(db=session, target_order_id=None)
        assert res1["orders_updated"] >= 40
        assert res1["kpi_updated"] >= 40

        # Query orders in DB to ensure no orders with kiz_code > 31 exist
        res_check = await session.execute(
            select(Order).where(Order.id.in_(created_order_ids), func.length(Order.kiz_code) > 31)
        )
        remaining_dirty = res_check.scalars().all()
        assert len(remaining_dirty) == 0, f"Found {len(remaining_dirty)} orders with length > 31"

        # Verify all created orders are now strictly 31 chars
        res_orders = await session.execute(select(Order).where(Order.id.in_(created_order_ids)))
        for o in res_orders.scalars().all():
            assert len(o.kiz_code) == 31
            assert o.kiz_code.startswith("01")

        # Run Pass 2 (Idempotency test)
        res2 = await normalize_existing_orders_kiz(db=session, target_order_id=None)
        # Should update 0 orders and 0 kpis because all are already clean
        assert res2["orders_updated"] == 0
        assert res2["kpi_updated"] == 0


# ==============================================================================
# 4. Extreme Boundary Conditions & Fuzzing
# ==============================================================================

def test_extreme_boundaries_empty_and_whitespace():
    """Verify handling of empty, whitespace, and control character inputs."""
    assert normalize_kiz_light_industry("") == ""
    assert normalize_kiz_light_industry(None) == ""
    assert normalize_kiz_light_industry("   ") == ""
    assert normalize_kiz_light_industry("\t\r\n") == ""
    assert normalize_kiz_light_industry("\x1d\x1e\x1f") == ""
    assert normalize_kiz_light_industry("\u001d\u001e\u001f") == ""

    parsed_empty = parse_kiz_code("")
    assert parsed_empty["raw_code"] == ""
    assert parsed_empty["clean_cis"] is None

    parsed_none = parse_kiz_code(None)
    assert parsed_none["clean_cis"] is None


def test_extreme_boundaries_corrupt_gtin():
    """Non-digit inputs without 14 digits should not crash and degrade gracefully."""
    non_digit_code = "01ABCDEFGHIJKLMN21XYZ!@#$"
    out = normalize_kiz_light_industry(non_digit_code)
    assert out == non_digit_code

    short_gtin = "0112345678"
    out_short = normalize_kiz_light_industry(short_gtin)
    assert out_short == short_gtin


def test_extreme_boundaries_massive_strings_redos_resistance():
    """
    Stress-test with very large strings (up to 50,000 characters) to ensure
    no ReDoS catastrophic backtracking occurs. Must complete in < 150ms.
    """
    # 1. 20,000 chars of repeated valid KIZ
    huge_repeated = CANONICAL_31_CIS * 600
    t0 = time.perf_counter()
    out1 = normalize_kiz_light_industry(huge_repeated)
    dt1 = time.perf_counter() - t0
    assert len(out1) == 31
    assert dt1 < 0.15, f"Execution took too long: {dt1:.4f}s"

    # 2. 50,000 chars of '91' and '92' alternation without GTIN
    huge_crypto_junk = ("91ABCD" * 4000) + ("92XYZ" * 4000)
    t0 = time.perf_counter()
    out2 = normalize_kiz_light_industry(huge_crypto_junk)
    dt2 = time.perf_counter() - t0
    assert dt2 < 0.15, f"Execution took too long: {dt2:.4f}s"

    # 3. 20,000 chars of spaces and control characters
    huge_whitespace = " \x1d\u001d\t\n" * 4000
    t0 = time.perf_counter()
    out3 = normalize_kiz_light_industry(huge_whitespace)
    dt3 = time.perf_counter() - t0
    assert out3 == ""
    assert dt3 < 0.10, f"Execution took too long: {dt3:.4f}s"


def test_extreme_boundaries_injection_attacks():
    """
    Test SQL injection, HTML/script injection, and shell meta-characters.
    They should be sanitized, truncated, or safely converted without crashing JSON builders.
    """
    client = CZClient(inn="190207495060", token="mock-cz-token")

    # SQL injection attempt with spaces: normalizer cuts off at the space delimiter
    sql_injection_kiz_spaces = "0104630199251318215QTSRh'; DROP TABLE orders; --"
    out_sql_spaces = normalize_kiz_light_industry(sql_injection_kiz_spaces)
    # The injection payload after the space is completely eliminated
    assert "DROP TABLE" not in out_sql_spaces
    assert out_sql_spaces == "0104630199251318215QTSRh';"

    # SQL injection attempt without spaces: truncated to strictly 13-char serial (31 chars total)
    sql_injection_kiz_nospace = "0104630199251318215QTSRh'DROP;--"
    out_sql_nospace = normalize_kiz_light_industry(sql_injection_kiz_nospace)
    assert len(out_sql_nospace) == 31
    assert out_sql_nospace == "0104630199251318215QTSRh'DROP;-"

    # Script tag attempt
    xss_kiz = "010463019925131821<script>alert(1)</script>"
    out_xss = normalize_kiz_light_industry(xss_kiz)
    assert len(out_xss) == 31
    assert out_xss == "010463019925131821<script>alert"

    # Verify building LP_RETURN with injection payloads produces valid JSON
    doc = client._build_return_document(
        kiz_codes=[sql_injection_kiz_spaces, sql_injection_kiz_nospace, xss_kiz],
        primary_document_number='5647931541" OR 1=1 --',
    )
    # Must be 100% valid JSON
    payload = json.loads(doc["productDocument"])
    assert len(payload["products_list"]) == 3
    assert payload["products_list"][0]["ki"] == out_sql_spaces
    assert payload["products_list"][1]["ki"] == out_sql_nospace
    assert payload["products_list"][2]["ki"] == out_xss


def test_extreme_boundaries_special_gs1_characters_in_serial():
    """
    GS1 specifications permit punctuation in serials: ! % & ' * + - . / : ; < = > ? _
    Ensure permitted characters are preserved in the 13-character serial.
    """
    special_serial = "5QTSRh>4sVc+."
    assert len(special_serial) == 13
    raw = f"010463019925131821{special_serial} 91EE12 92xyz"
    out = normalize_kiz_light_industry(raw)
    assert out == f"010463019925131821{special_serial}"
    assert len(out) == 31

    # Underscores and hyphens
    serial_dash = "AB-CD_EF+12.3"
    raw2 = f"010463019925131821{serial_dash}\x1d91ffd92qwe"
    out2 = normalize_kiz_light_industry(raw2)
    assert out2 == f"010463019925131821{serial_dash}"
    assert len(out2) == 31


# ==============================================================================
# 5. API Ingestion & Validation Edge Cases
# ==============================================================================

@pytest.mark.asyncio
async def test_api_attach_kiz_short_code_rejection():
    """Inputs shorter than 10 characters must be rejected with 400."""
    from fastapi import HTTPException
    from app.api.kiz import attach_kiz
    from app.schemas.order import KIZAttachRequest

    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = f"test-seller-short-{uuid.uuid4().hex[:6]}"
        seller = Seller(
            id=seller_id,
            name="Test Seller Short",
            wb_supplier_id=f"WB-{uuid.uuid4().hex[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        req = KIZAttachRequest(kiz_code="010463")
        with pytest.raises(HTTPException) as exc_info:
            await attach_kiz(seller_id=seller_id, order_id=12345, req=req, db=session)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_api_validate_kiz_heals_unnormalized_order():
    """
    If an order exists in the DB with an unnormalized dirty KIZ,
    GET /orders/{order_id}/kiz/validate should heal the record in DB
    and return valid=True with 31-character normalized code.
    """
    await init_db()
    from app.api.kiz import validate_kiz

    async with AsyncSessionLocal() as session:
        seller_id = f"test-seller-heal-{uuid.uuid4().hex[:6]}"
        seller = Seller(
            id=seller_id,
            name="Test Seller Healing",
            wb_supplier_id=f"WB-{uuid.uuid4().hex[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True,
        )
        session.add(seller)

        order_id = random.randint(700000000, 799999999)
        dirty_code = f"0104630199251318215QTSRh>4sVc+. 91EE12 92{uuid.uuid4().hex[:6]}"
        order = Order(
            id=order_id,
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            article="dress.heal",
            tech_size="S",
            name="Платье",
            kiz_required=True,
            kiz_status=KizStatus.ATTACHED,
            kiz_code=dirty_code,
        )
        session.add(order)
        await session.commit()

        # Call validate endpoint
        res = await validate_kiz(seller_id=seller_id, order_id=order_id, db=session)
        assert res["valid"] is True
        assert res["details"]["kiz_code"] == CANONICAL_31_CIS
        assert res["details"]["length"] == 31

        # Check DB was healed
        await session.refresh(order)
        assert order.kiz_code == CANONICAL_31_CIS
        assert len(order.kiz_code) == 31
