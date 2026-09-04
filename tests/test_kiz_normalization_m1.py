"""
Milestone M1 Test Suite: Guaranteed KIZ Normalization & Ingestion Integrity.

Verifies:
1. normalize_kiz_light_industry handles spaces, GS delimiters, concatenated tails,
   varying key lengths, parentheses, and guarantees exact 31-character output.
2. parse_kiz_code extracts correct GTIN, serial, crypto key/tail, and clean_cis (31 chars).
3. CZClient._clean_cis_for_true_api delegates to canonical normalizer.
4. _build_withdrawal_document (LK_RECEIPT) and _build_return_document (LP_RETURN)
   guarantee strictly 31-character CIs in cis and ki fields.
5. attach_kiz normalizes raw barcodes with crypto tails before persisting to order.kiz_code.
6. validate_kiz validates and returns 31-character normalized code.
7. normalize_existing_orders_kiz normalizes DB records and order #5647931541.
"""
import json
import uuid
import random
from datetime import datetime, timezone

import pytest
from unittest.mock import patch, AsyncMock

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


# Canonical test samples
CLEAN_31_KIZ = "0104630199251318215QTSRh>4sVc+."
DIRTY_SPACES_TAIL = "0104630199251318215QTSRh>4sVc+. 91EE12 92xyz123456789"
DIRTY_SPACE_KEY_NO_SPACE_SIG = "0104630199251318215QTSRh>4sVc+. 91EE1292xyz123"
DIRTY_GS_DELIMITERS = "0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz123"
DIRTY_GS_UNICODE = "0104630199251318215QTSRh>4sVc+.\u001d91EE12\u001d92xyz123"
DIRTY_CONCATENATED_3KEY = "0104630199251318215QTSRh>4sVc+.91ffd92qwe"
DIRTY_CONCATENATED_4KEY = "0104630199251318215QTSRh>4sVc+.91ABCD92xyz123456"
DIRTY_PARENS_WITH_SPACES = "(01)04630199251318(21)5QTSRh>4sVc+. 91ABCD 92xyz"
DIRTY_PARENS_CONCATENATED = "(01)04630199251318(21)5QTSRh>4sVc+.(91)EE12(92)xyz"
DIRTY_NO_01_PREFIX = "04630199251318215QTSRh>4sVc+."
DIRTY_14_CHAR_SERIAL = "0104630199251332215ZEdKVT_00e737"
DIRTY_SCANNER_FNC1_START = "\x1d0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz"


# ==============================================================================
# 1. Tests for normalize_kiz_light_industry
# ==============================================================================

def test_normalize_kiz_light_industry_clean_31():
    out = normalize_kiz_light_industry(CLEAN_31_KIZ)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_spaces_tail():
    out = normalize_kiz_light_industry(DIRTY_SPACES_TAIL)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_space_key_no_space_sig():
    out = normalize_kiz_light_industry(DIRTY_SPACE_KEY_NO_SPACE_SIG)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_gs_delimiters():
    out = normalize_kiz_light_industry(DIRTY_GS_DELIMITERS)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_gs_unicode():
    out = normalize_kiz_light_industry(DIRTY_GS_UNICODE)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_concatenated_tails():
    out3 = normalize_kiz_light_industry(DIRTY_CONCATENATED_3KEY)
    assert len(out3) == 31
    assert out3 == CLEAN_31_KIZ

    out4 = normalize_kiz_light_industry(DIRTY_CONCATENATED_4KEY)
    assert len(out4) == 31
    assert out4 == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_parentheses():
    out1 = normalize_kiz_light_industry(DIRTY_PARENS_WITH_SPACES)
    assert len(out1) == 31
    assert out1 == CLEAN_31_KIZ

    out2 = normalize_kiz_light_industry(DIRTY_PARENS_CONCATENATED)
    assert len(out2) == 31
    assert out2 == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_no_01_prefix():
    out = normalize_kiz_light_industry(DIRTY_NO_01_PREFIX)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_14_char_serial_truncated_to_31():
    out = normalize_kiz_light_industry(DIRTY_14_CHAR_SERIAL)
    assert len(out) == 31
    assert out == "0104630199251332215ZEdKVT_00e73"


def test_normalize_kiz_light_industry_scanner_fnc1_prefix():
    out = normalize_kiz_light_industry(DIRTY_SCANNER_FNC1_START)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_scanner_aim_symbology_prefix():
    aim_code = "]d20104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz"
    out = normalize_kiz_light_industry(aim_code)
    assert len(out) == 31
    assert out == CLEAN_31_KIZ


def test_normalize_kiz_light_industry_internal_91_92_serial():
    internal_91_92 = "010463019925131821ABC91DEF92GHI"
    out = normalize_kiz_light_industry(internal_91_92)
    assert len(out) == 31
    assert out == "010463019925131821ABC91DEF92GHI"


def test_normalize_kiz_light_industry_empty_and_whitespace():
    assert normalize_kiz_light_industry("") == ""
    assert normalize_kiz_light_industry(None) == ""
    assert normalize_kiz_light_industry("   ") == ""


def test_normalize_kiz_light_industry_short_synthetic_mock_compatibility():
    short_mock = "0104630199251318215QTSRh"
    out = normalize_kiz_light_industry(short_mock)
    assert out == short_mock


# ==============================================================================
# 2. Tests for parse_kiz_code
# ==============================================================================

def test_parse_kiz_code_with_space_and_crypto_tail():
    res = parse_kiz_code(DIRTY_SPACES_TAIL)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh>4sVc+."
    assert res["crypto_key"] == "EE12"
    assert res["crypto_tail"] == "xyz123456789"
    assert res["clean_cis"] == CLEAN_31_KIZ
    assert len(res["clean_cis"]) == 31


def test_parse_kiz_code_with_gs_delimiter():
    res = parse_kiz_code(DIRTY_GS_DELIMITERS)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh>4sVc+."
    assert res["clean_cis"] == CLEAN_31_KIZ
    assert len(res["clean_cis"]) == 31


def test_parse_kiz_code_concatenated_3key():
    res = parse_kiz_code(DIRTY_CONCATENATED_3KEY)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh>4sVc+."
    assert res["crypto_key"] == "ffd"
    assert res["crypto_tail"] == "qwe"
    assert res["clean_cis"] == CLEAN_31_KIZ
    assert len(res["clean_cis"]) == 31


def test_parse_kiz_code_aim_prefix():
    aim_code = "]d20104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz"
    res = parse_kiz_code(aim_code)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh>4sVc+."
    assert res["crypto_key"] == "EE12"
    assert res["crypto_tail"] == "xyz"
    assert res["clean_cis"] == CLEAN_31_KIZ
    assert len(res["clean_cis"]) == 31


def test_parse_kiz_code_internal_91_92_serial():
    code = "010463019925131821ABC91DEF92GHI"
    res = parse_kiz_code(code)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "ABC91DEF92GHI"
    assert res["crypto_key"] is None
    assert res["crypto_tail"] is None
    assert res["clean_cis"] == "010463019925131821ABC91DEF92GHI"


# ==============================================================================
# 3. Tests for CZClient._clean_cis_for_true_api
# ==============================================================================

def test_cz_client_clean_cis_for_true_api():
    client = CZClient(inn="7701234567", token="mock-token")
    assert client._clean_cis_for_true_api(DIRTY_SPACES_TAIL) == CLEAN_31_KIZ
    assert client._clean_cis_for_true_api(DIRTY_GS_DELIMITERS) == CLEAN_31_KIZ
    assert client._clean_cis_for_true_api(DIRTY_CONCATENATED_3KEY) == CLEAN_31_KIZ
    assert client._clean_cis_for_true_api(DIRTY_PARENS_WITH_SPACES) == CLEAN_31_KIZ
    assert len(client._clean_cis_for_true_api(DIRTY_SPACES_TAIL)) == 31


# ==============================================================================
# 4. Tests for _build_withdrawal_document (LK_RECEIPT) and _build_return_document (LP_RETURN)
# ==============================================================================

def test_withdrawal_document_guarantees_31_char_cis():
    client = CZClient(inn="7701234567", token="mock-token")
    doc = client._build_withdrawal_document(
        kiz_codes=[DIRTY_SPACES_TAIL, DIRTY_GS_DELIMITERS, DIRTY_CONCATENATED_3KEY],
        price_kopecks=150000,
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    products = payload["products"]
    assert len(products) == 3
    for p in products:
        assert len(p["cis"]) == 31
        assert p["cis"] == CLEAN_31_KIZ


def test_withdrawal_document_items_guarantees_31_char_cis():
    client = CZClient(inn="7701234567", token="mock-token")
    items = [
        {"cis": DIRTY_SPACES_TAIL, "price_kopecks": 200000},
        {"kiz_code": DIRTY_CONCATENATED_4KEY, "price_kopecks": 250000},
    ]
    doc = client._build_withdrawal_document(
        items=items,
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    products = payload["products"]
    assert len(products) == 2
    for p in products:
        assert len(p["cis"]) == 31
        assert p["cis"] == CLEAN_31_KIZ


def test_return_document_guarantees_31_char_ki():
    client = CZClient(inn="7701234567", token="mock-token")
    doc = client._build_return_document(
        kiz_codes=[DIRTY_SPACES_TAIL, DIRTY_GS_DELIMITERS],
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    products = payload.get("products_list") or payload.get("products")
    assert len(products) == 2
    for p in products:
        assert len(p["ki"]) == 31
        assert p["ki"] == CLEAN_31_KIZ


def test_return_document_items_guarantees_31_char_ki():
    client = CZClient(inn="7701234567", token="mock-token")
    items = [
        {"ki": DIRTY_SPACES_TAIL},
        {"kiz_code": DIRTY_CONCATENATED_3KEY},
    ]
    doc = client._build_return_document(
        items=items,
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    products = payload.get("products_list") or payload.get("products")
    assert len(products) == 2
    for p in products:
        assert len(p["ki"]) == 31
        assert p["ki"] == CLEAN_31_KIZ



# ==============================================================================
# 5. Tests for attach_kiz and validate_kiz endpoints
# ==============================================================================

@pytest.mark.asyncio
async def test_attach_kiz_normalizes_dirty_code_before_saving():
    await init_db()
    from app.api.kiz import attach_kiz, validate_kiz
    from app.schemas.order import KIZAttachRequest

    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller M1",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True,
        )
        session.add(seller)

        order_id = random.randint(900000000, 999999999)
        order = Order(
            id=order_id,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            article="dress.spring.2026",
            tech_size="M",
            name="Платье весеннее",
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
        )
        session.add(order)
        await session.commit()

        # Submit dirty raw barcode with spaces and crypto tail
        req = KIZAttachRequest(kiz_code=DIRTY_SPACES_TAIL)

        with patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
            mock_cz.return_value = [{"cisInfo": {"status": "INTRODUCED", "ownerInn": "7700123456"}}]
            res = await attach_kiz(seller_id=seller_id, order_id=order_id, req=req, db=session)

        assert res["success"] is True
        assert res["kiz_code"] == CLEAN_31_KIZ
        assert len(res["kiz_code"]) == 31

        # Check DB model record
        saved_order = await session.get(Order, order_id)
        assert saved_order.kiz_code == CLEAN_31_KIZ
        assert len(saved_order.kiz_code) == 31
        assert saved_order.kiz_status == KizStatus.ATTACHED

        # Validate endpoint
        val_res = await validate_kiz(seller_id=seller_id, order_id=order_id, db=session)
        assert val_res["valid"] is True
        assert val_res["details"]["kiz_code"] == CLEAN_31_KIZ
        assert val_res["details"]["length"] == 31


# ==============================================================================
# 6. Tests for normalize_existing_orders_kiz and Order #5647931541
# ==============================================================================

@pytest.mark.asyncio
async def test_normalize_existing_orders_kiz_and_target_order_5647931541():
    await init_db()

    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Order 5647931541",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True,
        )
        session.add(seller)

        # Create target order #5647931541 with dirty KIZ
        target_order_id = 5647931541
        existing_order = await session.get(Order, target_order_id)
        if existing_order:
            await session.delete(existing_order)

        from sqlalchemy import select
        old_kpis = await session.execute(
            select(KizProductInfo).where(KizProductInfo.order_id == target_order_id)
        )
        for ok in old_kpis.scalars().all():
            await session.delete(ok)
        await session.commit()

        dirty_target_kiz = f"0104630199251318215QTSRh>4sVc+. 91EE12 92{uuid.uuid4().hex[:12]}"
        order_5647931541 = Order(
            id=target_order_id,
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="ONE SIZE",
            name="Капор утепленный",
            kiz_required=True,
            kiz_status=KizStatus.ERROR,
            kiz_code=dirty_target_kiz,  # unnormalized code with space and crypto tail
        )
        session.add(order_5647931541)

        # Also add a random order with concatenated tail
        other_order_id = random.randint(500000000, 599999999)
        dirty_other_kiz = f"0104630199251318215QTSRh>4sVc+.91ffd92{uuid.uuid4().hex[:8]}"
        other_order = Order(
            id=other_order_id,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            article="shirt.white.42",
            tech_size="42",
            name="Рубашка",
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
            kiz_code=dirty_other_kiz,
        )
        session.add(other_order)

        # Add corresponding KizProductInfo
        kpi = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=dirty_target_kiz,
            gtin="04630199251318",
            clean_cis=dirty_target_kiz,
            order_id=target_order_id,
            seller_id=seller.id,
        )

        session.add(kpi)
        await session.commit()


        # Run database normalization routine
        res = await normalize_existing_orders_kiz(db=session, target_order_id=target_order_id)

        assert res["target_found"] is True
        assert res["target_normalized"] is True
        assert res["orders_updated"] >= 2

        # Verify order #5647931541 in DB is normalized to exact 31 chars
        refreshed_target = await session.get(Order, target_order_id)
        assert refreshed_target.kiz_code == CLEAN_31_KIZ
        assert len(refreshed_target.kiz_code) == 31

        # Verify other order is normalized to exact 31 chars
        refreshed_other = await session.get(Order, other_order_id)
        assert refreshed_other.kiz_code == CLEAN_31_KIZ
        assert len(refreshed_other.kiz_code) == 31

        # Verify KizProductInfo clean_cis is normalized
        refreshed_kpi = await session.get(KizProductInfo, kpi.id)
        assert refreshed_kpi.clean_cis == CLEAN_31_KIZ
        assert len(refreshed_kpi.clean_cis) == 31

        # Pass 2: Verify idempotency - already clean target order must report target_normalized=True
        res2 = await normalize_existing_orders_kiz(db=session, target_order_id=target_order_id)
        assert res2["target_found"] is True
        assert res2["target_normalized"] is True
        assert res2["orders_updated"] == 0
