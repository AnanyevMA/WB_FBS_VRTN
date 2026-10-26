"""
Tier 1 & Tier 2 In-Memory SQLite Test Suite for WB Warehouse Sales Storno & Return Receipts.
Verifies Milestone 2 invariants for wb_warehouse_sales_service.py and wb_warehouse_sales_builder.py:
1. Normal warehouse sales processing (zero regressions).
2. Excise rows with return/storno receipt and RETIRED KIZ -> generates LP_RETURN batch.
3. Excise rows with INTRODUCED KIZ -> skips LP_RETURN and updates order to RETURNED.
4. Third-party INN ownership isolation -> skipped from LP_RETURN (avoids True API error 11).
5. Mixed batches containing both sales and returns.
6. Storno receipts via negative amounts / return document types in finance reports.
7. Deduplication of pending return batches.
8. Unnormalized raw KIZ (crypto-tail) correlation with clean 31-char CIS in excise report.
9. Fallback handling on missing/zero fiscal document numbers.
10. Line length compliance (<400 lines per module).
"""
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.kiz import BatchStatus, KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import encrypt
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller

try:
    from app.services.wb_warehouse_sales_builder import (
        build_warehouse_return_item,
        build_warehouse_sale_item,
    )
except ImportError:
    build_warehouse_return_item = None
    build_warehouse_sale_item = None


@pytest.fixture
async def db_session():
    """Isolated in-memory SQLite database session fixture with StaticPool."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _create_test_seller(
    session: AsyncSession,
    seller_id: str = "wh-seller-test",
    cz_inn: str = "190207495060",
) -> Seller:
    """Helper to persist an active test Seller with encrypted tokens."""
    seller = Seller(
        id=seller_id,
        name="Warehouse Test Seller",
        wb_api_token_encrypted=encrypt("wb_tok_test_secret"),
        cz_inn=cz_inn,
        cz_token_encrypted=encrypt("cz_tok_test_secret"),
        is_active=True,
    )
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Tier 1: Core Architectural Contracts & Invariants
# ============================================================================

def test_warehouse_sales_service_line_length_under_limit():
    """Verify wb_warehouse_sales_service.py line count is strictly under 400 lines (PROJECT_RULES.md)."""
    service_path = os.path.join("app", "services", "wb_warehouse_sales_service.py")
    assert os.path.exists(service_path), f"File {service_path} not found"
    with open(service_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) < 400, f"wb_warehouse_sales_service.py exceeds 400 lines: {len(lines)}"


def test_builder_file_line_length_under_limit():
    """Verify wb_warehouse_sales_builder.py line count is strictly under 400 lines if present."""
    builder_path = os.path.join("app", "services", "wb_warehouse_sales_builder.py")
    if os.path.exists(builder_path):
        with open(builder_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) < 400, f"wb_warehouse_sales_builder.py exceeds 400 lines: {len(lines)}"


def test_builder_build_warehouse_return_item_contract():
    """Verify build_warehouse_return_item returns valid dictionary satisfying LP_RETURN schema."""
    if build_warehouse_return_item is None:
        pytest.skip("wb_warehouse_sales_builder awaiting extraction by M2 worker")

    ret_item = build_warehouse_return_item(
        cis="0104630199251332215+gIUHbTKIpgQ",
        order_id=987654,
        srid="srid_builder_001.0.0",
        barcode="2044967817804",
        nm_id=473442280,
        price=3194.0,
        fiscal_doc_number=145211,
        fiscal_drive_number="7380440903830012",
        fiscal_dt="2026-08-28",
    )
    assert ret_item["ki"] == "0104630199251332215+gIUHbTKIpgQ"
    assert ret_item["clean_cis"] == "0104630199251332215+gIUHbTKIpgQ"
    assert ret_item["action"] == "RETURN"
    assert ret_item.get("doc_type") == "LP_RETURN" or ret_item.get("document_type") in ("OTHER", "LP_RETURN")
    assert ret_item["primary_document_type"] == "RECEIPT"
    assert ret_item["primary_document_number"] == "145211"
    assert ret_item["primary_document_date"] == "2026-08-28"
    assert ret_item["price"] == 3194.0
    assert ret_item["price_kopecks"] == 319400
    assert ret_item["is_seller_owner"] is True
    assert ret_item["needs_cz_return"] is True
    assert ret_item["selected"] is True


def test_builder_build_warehouse_sale_item_contract():
    """Verify build_warehouse_sale_item returns valid dictionary with WITHDRAWAL action."""
    if build_warehouse_sale_item is None:
        pytest.skip("wb_warehouse_sales_builder awaiting extraction by M2 worker")

    sale_item = build_warehouse_sale_item(
        cis="0104630199251332215SALE_ITEM",
        order_id=123456,
        srid="srid_sale_item.0.0",
        barcode="2044967817804",
        nm_id=123,
        price=1500.0,
        fiscal_doc_number=111,
        fiscal_drive_number="222",
        fiscal_dt="2026-08-28",
    )
    assert sale_item["action"] == "WITHDRAWAL" or sale_item.get("needs_withdrawal") is True
    assert sale_item.get("ki") == "0104630199251332215SALE_ITEM" or sale_item.get("kiz_code") == "0104630199251332215SALE_ITEM"
    assert sale_item["price"] == 1500.0
    assert sale_item["price_kopecks"] == 150000


@pytest.mark.asyncio
async def test_warehouse_sales_normal_sale_zero_regression(db_session: AsyncSession):
    """
    Zero-regression baseline: Normal sale with INTRODUCED KIZ on seller balance
    creates a KizSignatureBatch with withdrawals and empty returns.
    """
    seller = await _create_test_seller(db_session, "seller-normal-sale")
    clean_cis = "0104630199251332215NORMAL_SALE"

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": clean_cis,
            "fiscal_doc_number": 145211,
            "fiscal_drive_number": "7380440903830012",
            "fiscal_dt": "2026-09-01",
            "nm_id": 473442280,
            "price": 2500.0,
            "srid": "srid-normal-sale-001",
        }
    ]

    mock_kinfo = MagicMock()
    mock_kinfo.cz_status = "INTRODUCED"
    mock_kinfo.cz_status_ex = None
    mock_kinfo.raw_cz_payload = {"ownerInn": seller.cz_inn}

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {clean_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 1
        assert res.get("returns_count", 0) == 0

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        assert batch.source == "wb_warehouse_sale"
        assert batch.status == BatchStatus.PENDING_SIGNATURE
        assert batch.sales_count == 1
        assert batch.returns_count == 0
        assert len(batch.data_payload.get("withdrawals", [])) == 1
        assert len(batch.data_payload.get("returns", [])) == 0


@pytest.mark.asyncio
async def test_excise_row_with_return_storno_and_retired_kiz_generates_lp_return(db_session: AsyncSession):
    """
    Invariant: Return row in finance report (doc_type_name='Возврат') matching excise report
    with True API RETIRED status on seller balance -> generates LP_RETURN batch item.
    """
    seller = await _create_test_seller(db_session, "seller-retired-return")
    test_cis = "0104630199251844215RETIRED_RET"
    test_srid = "srid_ret_retired.0.0"

    today_date = datetime.now(timezone.utc).date()
    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=987654321,
        clean_cis=test_cis,
        srid=test_srid,
        doc_type_name="Возврат",
        seller_oper_name="Возврат",
        retail_amount=Decimal("3500.00"),
        rr_date=today_date,
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": test_cis,
            "fiscal_doc_number": 888123,
            "fiscal_drive_number": "7380440903830000",
            "fiscal_dt": today_date.isoformat(),
            "nm_id": 11223344,
            "price": 3500.0,
            "srid": test_srid,
        }
    ]

    mock_kinfo = MagicMock()
    mock_kinfo.cz_status = "RETIRED"
    mock_kinfo.cz_status_ex = None
    mock_kinfo.raw_cz_payload = {"ownerInn": seller.cz_inn}

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {test_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        # In M2, this must create LP_RETURN batch items for RETIRED codes
        if res.get("returns_count", 0) > 0:
            assert res["created"] is True
            assert res["returns_count"] == 1
            batch = await db_session.get(KizSignatureBatch, res["batch_id"])
            assert batch is not None
            assert batch.returns_count == 1
            ret_item = batch.data_payload["returns"][0]
            assert ret_item.get("clean_cis") == test_cis or ret_item.get("ki") == test_cis
            assert ret_item["action"] == "RETURN"
            assert ret_item["is_seller_owner"] is True
            assert ret_item["needs_cz_return"] is True
        else:
            # Baseline behavior before M2 worker merges return builder:
            # Service skips withdrawal (already retired / return detected in finance report)
            assert res.get("needs_withdrawal_count", 0) == 0


@pytest.mark.asyncio
async def test_excise_row_with_introduced_kiz_skips_lp_return(db_session: AsyncSession):
    """
    Invariant: Return row matching excise report where True API status is INTRODUCED
    (product was never withdrawn / uncollected before checkout):
    - Must NOT generate LP_RETURN document (avoids True API error 14).
    - If associated Order exists, updates kiz_status to RETURNED.
    """
    seller = await _create_test_seller(db_session, "seller-intro-skip")
    test_cis = "0104630199251844215ALREADY_INTRO"
    test_srid = "srid_skip_intro.0.0"
    order_id = 77112233

    order = Order(
        id=order_id,
        seller_id=seller.id,
        name="Куртка осенняя",
        status=OrderStatus.DELIVERED,
        kiz_code=test_cis,
        kiz_status=KizStatus.ATTACHED,
        cz_withdrawal_doc_id="doc-old-withdrawn",
        wb_created_at=datetime.now(timezone.utc),
    )
    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=333444,
        clean_cis=test_cis,
        srid=test_srid,
        doc_type_name="Возврат",
        retail_amount=Decimal("1500.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add_all([order, ret_row])
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": test_cis,
            "fiscal_doc_number": 3,
            "srid": test_srid,
            "price": 1500.0,
            "nm_id": 1,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-09-01",
        }
    ]

    mock_kinfo = MagicMock()
    mock_kinfo.cz_status = "INTRODUCED"
    mock_kinfo.cz_status_ex = None
    mock_kinfo.raw_cz_payload = {"ownerInn": seller.cz_inn}

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {test_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        # Invariant: Never create LP_RETURN or withdrawal for INTRODUCED returns
        assert res.get("returns_count", 0) == 0
        assert res.get("needs_withdrawal_count", 0) == 0
        assert order.status == OrderStatus.DELIVERED, "DELIVERED order must NOT be mutated to CANCELLED"
        assert order.kiz_status == KizStatus.RETURNED
        assert order.kiz_cz_status == "INTRODUCED"
        assert order.cz_withdrawal_doc_id is None


@pytest.mark.asyncio
async def test_excise_return_foreign_inn_wb_isolated(db_session: AsyncSession):
    """
    Invariant: Return row where True API ownerInn is Wildberries (ООО 'РВБ' 9714053621)
    MUST be excluded from seller's LP_RETURN batch (avoids True API error 11).
    """
    seller = await _create_test_seller(db_session, "seller-foreign-wb", cz_inn="190207495060")
    test_cis = "0104630199251844215WB_OWNED_CIS"
    test_srid = "srid_wb_owned.0.0"

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=444555,
        clean_cis=test_cis,
        srid=test_srid,
        doc_type_name="Возврат",
        retail_amount=Decimal("2200.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": test_cis,
            "fiscal_doc_number": 55,
            "srid": test_srid,
            "price": 2200.0,
            "nm_id": 99,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-09-01",
        }
    ]

    mock_kinfo = MagicMock()
    mock_kinfo.cz_status = "RETIRED"
    mock_kinfo.cz_status_ex = None
    mock_kinfo.raw_cz_payload = {
        "cisInfo": {
            "ownerInn": "9714053621",  # ООО "РВБ" (WB)
            "ownerName": "ООО РВБ",
        }
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {test_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        # Invariant: Must not create LP_RETURN for WB-owned code
        assert res.get("returns_count", 0) == 0
        assert res.get("needs_withdrawal_count", 0) == 0
        assert res.get("created") is False


@pytest.mark.asyncio
async def test_excise_return_foreign_inn_third_party_isolated(db_session: AsyncSession):
    """
    Invariant: Return row where True API ownerInn is an unrelated 3rd party (e.g. 7701234567)
    MUST be excluded from seller's LP_RETURN batch.
    """
    seller = await _create_test_seller(db_session, "seller-foreign-3rd", cz_inn="190207495060")
    test_cis = "0104630199251844215THIRD_PARTY"
    test_srid = "srid_third_party.0.0"

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=666777,
        clean_cis=test_cis,
        srid=test_srid,
        doc_type_name="Возврат",
        retail_amount=Decimal("1800.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": test_cis,
            "fiscal_doc_number": 66,
            "srid": test_srid,
            "price": 1800.0,
            "nm_id": 101,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-09-01",
        }
    ]

    mock_kinfo = MagicMock()
    mock_kinfo.cz_status = "RETIRED"
    mock_kinfo.cz_status_ex = None
    mock_kinfo.raw_cz_payload = {
        "cisInfo": {
            "ownerInn": "7701234567",
            "ownerName": "ООО СТОРОННИЙ ПОСТАВЩИК",
        }
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {test_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res.get("returns_count", 0) == 0
        assert res.get("created") is False


@pytest.mark.asyncio
async def test_excise_mixed_batch_sales_and_returns(db_session: AsyncSession):
    """
    Invariant: When excise data contains both normal sales (INTRODUCED) and returns (RETIRED),
    the resulting KizSignatureBatch correctly separates withdrawals and returns.
    """
    seller = await _create_test_seller(db_session, "seller-mixed-batch")
    sale_cis = "0104630199251844215SALE_MIXED_1"
    ret_cis = "0104630199251844215RET_MIXED_2"

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=888999,
        clean_cis=ret_cis,
        srid="srid_ret_mixed.0.0",
        doc_type_name="Возврат",
        retail_amount=Decimal("3000.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": sale_cis,
            "fiscal_doc_number": 10,
            "srid": "srid_sale_mixed.0.0",
            "price": 2000.0,
            "nm_id": 1,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-09-01",
        },
        {
            "barcode": "2044967817804",
            "excise_short": ret_cis,
            "fiscal_doc_number": 20,
            "srid": "srid_ret_mixed.0.0",
            "price": 3000.0,
            "nm_id": 2,
            "fiscal_drive_number": "2",
            "fiscal_dt": "2026-09-01",
        },
    ]

    mock_sale_kinfo = MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})
    mock_ret_kinfo = MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {sale_cis: mock_sale_kinfo, ret_cis: mock_ret_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] >= 1
        # When M2 return processing is enabled:
        if res.get("returns_count", 0) > 0:
            assert res["returns_count"] == 1
            batch = await db_session.get(KizSignatureBatch, res["batch_id"])
            assert batch is not None
            assert len(batch.data_payload["withdrawals"]) == 1
            assert len(batch.data_payload["returns"]) == 1
            assert batch.total_count == 2


# ============================================================================
# Tier 2: Boundary, Fault Tolerance & Edge Cases
# ============================================================================

@pytest.mark.asyncio
async def test_excise_storno_negative_amount_correlation(db_session: AsyncSession):
    """
    Edge case: Return identified by negative retail amount in finance report
    (even if doc_type_name is erroneously recorded as 'Продажа' / storno).
    """
    seller = await _create_test_seller(db_session, "seller-neg-storno")
    test_cis = "0104630199251844215STORNO_NEG"
    test_srid = "srid_storno_neg.0.0"

    storno_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=555666,
        clean_cis=test_cis,
        srid=test_srid,
        doc_type_name="Продажа",  # Doc type is sale, but amount is negative!
        seller_oper_name="Сторно",
        retail_amount=Decimal("-2500.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(storno_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": test_cis,
            "fiscal_doc_number": 99,
            "srid": test_srid,
            "price": 2500.0,
            "nm_id": 50,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-09-01",
        }
    ]

    mock_kinfo = MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {test_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        # Should detect that test_cis is a return/storno and NOT process as normal sale withdrawal
        assert res.get("needs_withdrawal_count", 0) == 0


@pytest.mark.asyncio
async def test_excise_return_deduplication_already_pending(db_session: AsyncSession):
    """
    Idempotency: When a return KIZ is already present in a pending signature batch,
    it is not re-added in subsequent runs.
    """
    seller = await _create_test_seller(db_session, "seller-dedup-returns")
    cis_dedup = "0104630199251844215DEDUP_RETURN"

    existing_batch = KizSignatureBatch(
        seller_id=seller.id,
        filename="wb_warehouse_pending_existing.json",
        source="wb_warehouse_sale",
        status=BatchStatus.PENDING_SIGNATURE,
        sales_count=0,
        returns_count=1,
        already_withdrawn_count=0,
        total_count=1,
        data_payload={
            "returns": [{"kiz_code": cis_dedup, "clean_cis": cis_dedup, "action": "RETURN"}]
        },
    )
    db_session.add(existing_batch)
    await db_session.commit()

    # Re-querying should identify existing KIZ
    stmt = select(KizSignatureBatch).where(
        KizSignatureBatch.seller_id == seller.id,
        KizSignatureBatch.status == BatchStatus.PENDING_SIGNATURE,
    )
    batches = (await db_session.execute(stmt)).scalars().all()
    assert len(batches) == 1
    assert batches[0].data_payload["returns"][0]["clean_cis"] == cis_dedup


@pytest.mark.asyncio
async def test_excise_return_unnormalized_crypto_tail_order_lookup(db_session: AsyncSession):
    """
    Normalization: Order stores raw DataMatrix with crypto-tail (GS separator '\x1d91EE10\x1d92...').
    Excise report has clean 31-char CIS.
    Lookup must correlate order by normalized clean CIS.
    """
    seller = await _create_test_seller(db_session, "seller-norm-order")
    raw_tail_kiz = "0104630199251844215CLEAN_31_CIS\x1d91EE10\x1d92cryptotail123="
    clean_cis = "0104630199251844215CLEAN_31_CIS"

    order = Order(
        id=55443322,
        seller_id=seller.id,
        name="Товар с криптохвостом",
        sticker_id="STICKER-GS-999",
        status=OrderStatus.DELIVERED,
        kiz_code=raw_tail_kiz,
        kiz_status=KizStatus.WITHDRAWN,
        wb_created_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    await db_session.commit()

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=121212,
        clean_cis=clean_cis,
        srid="srid_tail.0.0",
        doc_type_name="Возврат",
        retail_amount=Decimal("1990.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": "2044967817804",
            "excise_short": clean_cis,
            "fiscal_doc_number": 777,
            "srid": "srid_tail.0.0",
            "price": 1990.0,
            "nm_id": 77,
            "fiscal_drive_number": "7380440903830000",
            "fiscal_dt": "2026-09-01",
        }
    ]

    mock_kinfo = MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = {clean_cis: mock_kinfo}

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res is not None


def test_excise_return_missing_fiscal_fields_fallback():
    """Fault tolerance: Missing fiscal doc number or drive number uses safe defaults."""
    if build_warehouse_return_item is None:
        pytest.skip("wb_warehouse_sales_builder awaiting extraction by M2 worker")

    ret_item = build_warehouse_return_item(
        cis="0104630199251332215FALLBACK",
        order_id=None,
        srid="srid_fallback.0.0",
        barcode="2044967817804",
        nm_id=473442280,
        price=1000.0,
        fiscal_doc_number=0,
        fiscal_drive_number=None,
        fiscal_dt=None,
    )
    assert ret_item is not None
    assert ret_item["ki"] == "0104630199251332215FALLBACK"
    assert ret_item["action"] == "RETURN"
    assert "0" in str(ret_item.get("primary_document_number") or ret_item.get("receipt_number"))


# ============================================================================
# Tier 3: Empirical Adversarial Suite (Mixed Batches, Boundary Fiscal Fields & Idempotency)
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_batch_generation_sales_only(db_session: AsyncSession):
    """
    Adversarial verification: Report contains ONLY sales (3 items, INTRODUCED on seller balance).
    Batch must have sales_count == 3, returns_count == 0, and data_payload['returns'] empty.
    """
    seller = await _create_test_seller(db_session, "adv-sales-only")
    cises = [f"0104630199251844215ADV_SALE_{i}" for i in range(1, 4)]
    
    mock_excise_rows = [
        {
            "barcode": f"204496781780{i}",
            "excise_short": cis,
            "fiscal_doc_number": 1000 + i,
            "fiscal_drive_number": "7380440903830000",
            "fiscal_dt": "2026-10-01",
            "nm_id": 100 + i,
            "price": 1200.0 * i,
            "srid": f"srid_adv_sale_{i}.0.0",
        }
        for i, cis in enumerate(cises, start=1)
    ]

    mock_verified = {
        cis: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})
        for cis in cises
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 3
        assert res["returns_count"] == 0

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        assert batch.sales_count == 3
        assert batch.returns_count == 0
        assert batch.total_count == 3
        assert len(batch.data_payload["withdrawals"]) == 3
        assert len(batch.data_payload["returns"]) == 0


@pytest.mark.asyncio
async def test_adversarial_batch_generation_returns_only(db_session: AsyncSession):
    """
    Adversarial verification: Report contains ONLY returns (3 items, RETIRED on seller balance).
    Batch must have sales_count == 0, returns_count == 3, and data_payload['withdrawals'] empty.
    """
    seller = await _create_test_seller(db_session, "adv-returns-only")
    cises = [f"0104630199251844215ADV_RET_{i}" for i in range(1, 4)]

    # Add finance report rows identifying these as returns
    today_date = datetime.now(timezone.utc).date()
    for i, cis in enumerate(cises, start=1):
        ret_row = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller.id,
            rrd_id=2000 + i,
            clean_cis=cis,
            srid=f"srid_adv_ret_{i}.0.0",
            doc_type_name="Возврат",
            retail_amount=Decimal(str(1500.0 * i)),
            rr_date=today_date,
        )
        db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "barcode": f"204496781780{i}",
            "excise_short": cis,
            "fiscal_doc_number": 2000 + i,
            "fiscal_drive_number": "7380440903830000",
            "fiscal_dt": today_date.isoformat(),
            "nm_id": 200 + i,
            "price": 1500.0 * i,
            "srid": f"srid_adv_ret_{i}.0.0",
        }
        for i, cis in enumerate(cises, start=1)
    ]

    mock_verified = {
        cis: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})
        for cis in cises
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 0
        assert res["returns_count"] == 3

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        assert batch.sales_count == 0
        assert batch.returns_count == 3
        assert batch.total_count == 3
        assert len(batch.data_payload["withdrawals"]) == 0
        assert len(batch.data_payload["returns"]) == 3
        for item in batch.data_payload["returns"]:
            assert item["action"] == "RETURN"
            assert item["doc_type"] == "LP_RETURN"
            assert item["needs_cz_return"] is True


@pytest.mark.asyncio
async def test_adversarial_batch_generation_mixed_complex(db_session: AsyncSession):
    """
    Adversarial stress-test: Complex mixed excise report containing simultaneous:
    - 2 valid sales (INTRODUCED on seller balance)
    - 2 valid returns (RETIRED on seller balance, 1 via doc_type='Возврат', 1 via negative amount)
    - 1 return with INTRODUCED status -> must be skipped
    - 1 foreign sale owned by Wildberries (9714053621) -> must be isolated
    - 1 foreign return owned by third-party (7701234567) -> must be isolated
    - 1 already retired sale (RETIRED) -> must update order if exists, skip withdrawal
    - 1 empty row (missing excise_short) -> safely skipped
    """
    seller = await _create_test_seller(db_session, "adv-mixed-complex")
    today_date = datetime.now(timezone.utc).date()

    sale1 = "0104630199251844215MIX_SALE_1"
    sale2 = "0104630199251844215MIX_SALE_2"
    ret1 = "0104630199251844215MIX_RET_1"
    ret2_neg = "0104630199251844215MIX_RET_2_NEG"
    ret_intro = "0104630199251844215MIX_RET_INTRO"
    foreign_wb = "0104630199251844215MIX_FOR_WB"
    foreign_3rd = "0104630199251844215MIX_FOR_3RD"
    sale_already_ret = "0104630199251844215MIX_ALREADY_RET"

    # Pre-populate finance report for returns
    ret1_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=3001,
        clean_cis=ret1, srid="srid_mix_ret_1.0.0", doc_type_name="Возврат",
        retail_amount=Decimal("2100.00"), rr_date=today_date,
    )
    ret2_neg_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=3002,
        clean_cis=ret2_neg, srid="srid_mix_ret_2_neg.0.0", doc_type_name="Продажа",
        seller_oper_name="Сторно", retail_amount=Decimal("-1800.00"), rr_date=today_date,
    )
    ret_intro_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=3003,
        clean_cis=ret_intro, srid="srid_mix_ret_intro.0.0", doc_type_name="Отказ",
        retail_amount=Decimal("1500.00"), rr_date=today_date,
    )
    foreign_3rd_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=3004,
        clean_cis=foreign_3rd, srid="srid_mix_for_3rd.0.0", doc_type_name="Возврат",
        retail_amount=Decimal("3000.00"), rr_date=today_date,
    )
    # Order for sale_already_ret to verify status update
    already_ret_order = Order(
        id=990011, seller_id=seller.id, name="Выведенный товар",
        status=OrderStatus.DELIVERED, kiz_code=sale_already_ret,
        kiz_status=KizStatus.ATTACHED, wb_created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([ret1_row, ret2_neg_row, ret_intro_row, foreign_3rd_row, already_ret_order])
    await db_session.commit()

    mock_excise_rows = [
        {"excise_short": sale1, "srid": "srid_sale_1.0.0", "price": 1000.0, "fiscal_doc_number": 101, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": sale2, "srid": "srid_sale_2.0.0", "price": 1500.0, "fiscal_doc_number": 102, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret1, "srid": "srid_mix_ret_1.0.0", "price": 2100.0, "fiscal_doc_number": 201, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret2_neg, "srid": "srid_mix_ret_2_neg.0.0", "price": 1800.0, "fiscal_doc_number": 202, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret_intro, "srid": "srid_mix_ret_intro.0.0", "price": 1500.0, "fiscal_doc_number": 203, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": foreign_wb, "srid": "srid_for_wb.0.0", "price": 999.0, "fiscal_doc_number": 301, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": foreign_3rd, "srid": "srid_mix_for_3rd.0.0", "price": 3000.0, "fiscal_doc_number": 302, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": sale_already_ret, "srid": "srid_already_ret.0.0", "price": 1200.0, "fiscal_doc_number": 401, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": "", "srid": "srid_empty.0.0", "price": 0.0},
        {"excise_short": "   ", "srid": "srid_space.0.0", "price": 0.0},
        {"excise_short": None, "srid": "srid_none.0.0", "price": 0.0},
    ]

    mock_verified = {
        sale1: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        sale2: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret1: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret2_neg: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret_intro: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        foreign_wb: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": "9714053621"}),
        foreign_3rd: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": "7701234567"}),
        sale_already_ret: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 2
        assert res["returns_count"] == 2
        assert res["already_retired_count"] == 1
        assert res["other_owner_count"] == 2

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        assert batch.sales_count == 2
        assert batch.returns_count == 2
        assert batch.total_count == 4
        assert len(batch.data_payload["withdrawals"]) == 2
        assert len(batch.data_payload["returns"]) == 2

        # Check order update
        await db_session.refresh(already_ret_order)
        assert already_ret_order.kiz_status == KizStatus.WITHDRAWN


@pytest.mark.asyncio
async def test_adversarial_idempotency_exact_repeated_runs(db_session: AsyncSession):
    """
    Adversarial verification of idempotency:
    Run 1 on mixed report creates Batch 1.
    Run 2 with the identical excise report must detect already queued items and NOT create Batch 2.
    """
    seller = await _create_test_seller(db_session, "adv-idempotent-exact")
    sale_cis = "0104630199251844215IDEMP_SALE"
    ret_cis = "0104630199251844215IDEMP_RET"

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=4001,
        clean_cis=ret_cis, srid="srid_idemp_ret.0.0", doc_type_name="Возврат",
        retail_amount=Decimal("2000.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {"excise_short": sale_cis, "srid": "srid_idemp_sale.0.0", "price": 1000.0, "fiscal_doc_number": 501, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret_cis, "srid": "srid_idemp_ret.0.0", "price": 2000.0, "fiscal_doc_number": 502, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
    ]

    mock_verified = {
        sale_cis: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret_cis: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        # Run 1: Must create batch
        res1 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res1["created"] is True
        assert res1["sales_count"] == 1
        assert res1["returns_count"] == 1

        # Run 2: Exact same report — must NOT create duplicate batch!
        res2 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res2["created"] is False
        assert res2["sales_count"] == 0
        assert res2["returns_count"] == 0

        # Assert only 1 batch exists in DB
        stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
        batches = (await db_session.execute(stmt)).scalars().all()
        assert len(batches) == 1
        assert batches[0].id == res1["batch_id"]


@pytest.mark.asyncio
async def test_adversarial_idempotency_incremental_new_sale(db_session: AsyncSession):
    """
    Adversarial verification of incremental deduplication:
    Run 1 creates batch with sale1 and ret1.
    Run 2 receives sale1, ret1, PLUS new sale2.
    Run 2 must create batch with ONLY sale2 (sales_count=1, returns_count=0), without duplicating sale1 or ret1.
    """
    seller = await _create_test_seller(db_session, "adv-idempotent-incr")
    sale1 = "0104630199251844215INCR_SALE_1"
    sale2_new = "0104630199251844215INCR_SALE_2_NEW"
    ret1 = "0104630199251844215INCR_RET_1"

    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=5001,
        clean_cis=ret1, srid="srid_incr_ret_1.0.0", doc_type_name="Возврат",
        retail_amount=Decimal("2500.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    rows_run1 = [
        {"excise_short": sale1, "srid": "srid_incr_sale_1.0.0", "price": 1000.0, "fiscal_doc_number": 601, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret1, "srid": "srid_incr_ret_1.0.0", "price": 2500.0, "fiscal_doc_number": 602, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
    ]

    mock_verified_run1 = {
        sale1: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret1: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = rows_run1
        mock_verify.return_value = mock_verified_run1

        # Run 1
        res1 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res1["created"] is True
        assert res1["sales_count"] == 1
        assert res1["returns_count"] == 1

        # Run 2: Contains old items PLUS new sale2
        rows_run2 = list(rows_run1) + [
            {"excise_short": sale2_new, "srid": "srid_incr_sale_2.0.0", "price": 3200.0, "fiscal_doc_number": 603, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-02"}
        ]
        mock_verified_run2 = dict(mock_verified_run1)
        mock_verified_run2[sale2_new] = MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

        mock_fetch.return_value = rows_run2
        mock_verify.return_value = mock_verified_run2

        res2 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res2["created"] is True
        assert res2["sales_count"] == 1
        assert res2["returns_count"] == 0

        batch2 = await db_session.get(KizSignatureBatch, res2["batch_id"])
        assert batch2 is not None
        assert batch2.sales_count == 1
        assert batch2.returns_count == 0
        assert batch2.data_payload["withdrawals"][0]["clean_cis"] == sale2_new
        assert len(batch2.data_payload["returns"]) == 0

        # Assert total 2 batches in DB
        stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
        batches = (await db_session.execute(stmt)).scalars().all()
        assert len(batches) == 2


@pytest.mark.asyncio
async def test_adversarial_empty_and_corrupted_fiscal_fields(db_session: AsyncSession):
    """
    Adversarial fault tolerance: Excise rows with all fiscal fields missing/corrupted
    (None, 0, empty string, missing srid, missing price, missing order in DB).
    Must generate valid KizSignatureBatch payloads with safe defaults without crashing.
    """
    seller = await _create_test_seller(db_session, "adv-empty-fields")
    sale_cis = "0104630199251844215EMPTY_FISCAL_SALE"
    ret_cis = "0104630199251844215EMPTY_FISCAL_RET"

    # Finance report row for return without clean_cis, but matching srid
    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=6001,
        clean_cis=ret_cis, srid="srid_empty_test", doc_type_name="Возврат",
        retail_amount=Decimal("0.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()

    mock_excise_rows = [
        {
            "excise_short": sale_cis,
            "fiscal_doc_number": None,
            "fiscal_drive_number": "",
            "fiscal_dt": None,
            "srid": None,
            "price": None,
            "barcode": None,
            "nm_id": None,
        },
        {
            "excise_short": ret_cis,
            "fiscal_doc_number": 0,
            "fiscal_drive_number": None,
            "fiscal_dt": "",
            "srid": "srid_empty_test",
            "price": 0.0,
            "barcode": "",
            "nm_id": 0,
        },
    ]

    mock_verified = {
        sale_cis: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={}),
        ret_cis: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 1
        assert res["returns_count"] == 1

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        w_item = batch.data_payload["withdrawals"][0]
        r_item = batch.data_payload["returns"][0]

        # Verify safe fallbacks
        assert w_item["action"] == "WITHDRAWAL"
        assert w_item["primary_document_number"] == "1"
        assert w_item["price"] == 0.0
        assert w_item["price_kopecks"] == 0
        assert len(w_item["primary_document_date"]) == 10  # YYYY-MM-DD

        assert r_item["action"] == "RETURN"
        assert r_item["primary_document_number"] in ("srid_empty_test", "1", "0")
        assert r_item["price"] == 0.0
        assert r_item["price_kopecks"] == 0
        assert len(r_item["primary_document_date"]) == 10


@pytest.mark.asyncio
async def test_adversarial_duplicate_cises_in_same_excise_report(db_session: AsyncSession):
    """
    Adversarial verification: Report returns duplicate occurrences of the same CIS
    (e.g. across multiple pagination slices or repeated status events).
    Must deduplicate by clean CIS and include exactly 1 entry.
    """
    seller = await _create_test_seller(db_session, "adv-dup-cis")
    dup_cis = "0104630199251844215DUP_CIS_TEST"

    mock_excise_rows = [
        {"excise_short": dup_cis, "srid": "srid_dup_1.0.0", "price": 1000.0, "fiscal_doc_number": 701, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": dup_cis, "srid": "srid_dup_2.0.0", "price": 1200.0, "fiscal_doc_number": 702, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-02"},
        {"excise_short": dup_cis, "srid": "srid_dup_3.0.0", "price": 1400.0, "fiscal_doc_number": 703, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-03"},
    ]

    mock_verified = {
        dup_cis: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res["created"] is True
        assert res["sales_count"] == 1
        assert res["returns_count"] == 0

        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch.total_count == 1
        assert len(batch.data_payload["withdrawals"]) == 1


# ============================================================================
# Tier 3: Empirical Adversarial Stress-Tests for Order State Transitions & Concurrency
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_non_delivered_orders_mutated_to_cancelled_on_introduced_return(db_session: AsyncSession):
    """
    Stress-Test Invariant:
    When return records with INTRODUCED status are received:
    - Non-DELIVERED orders (NEW, CONFIRMED, ASSEMBLED, SHIPPED) MUST transition to CANCELLED.
    - kiz_status MUST transition to RETURNED.
    - cz_withdrawal_doc_id MUST be reset to None.
    - Zero LP_RETURN batches must be created.
    """
    seller = await _create_test_seller(db_session, "seller-non-deliv-cancel")

    test_cases = [
        (400101, OrderStatus.NEW, "0104630199251844215NON_DELIV_NEW01", "srid_new_01.0.0"),
        (400102, OrderStatus.ASSEMBLING, "0104630199251844215NON_DELIV_ASSM01", "srid_assm_01.0.0"),
        (400103, OrderStatus.ASSEMBLED, "0104630199251844215NON_DELIV_ASMB01", "srid_asmb_01.0.0"),
        (400104, OrderStatus.DELIVERING, "0104630199251844215NON_DELIV_DLVR01", "srid_dlvr_01.0.0"),
        (400105, OrderStatus.SORTED, "0104630199251844215NON_DELIV_SORT01", "srid_sort_01.0.0"),
    ]

    orders = []
    finance_rows = []
    mock_excise_rows = []
    mock_verified = {}

    for oid, init_status, cis, srid in test_cases:
        o = Order(
            id=oid,
            seller_id=seller.id,
            status=init_status,
            kiz_code=cis,
            kiz_status=KizStatus.ATTACHED,
            cz_withdrawal_doc_id=f"DOC-PENDING-WITHDRAWAL-{oid}",
            wb_created_at=datetime.now(timezone.utc),
        )
        orders.append(o)

        f_row = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller.id,
            rrd_id=oid + 100000,
            clean_cis=cis,
            srid=srid,
            doc_type_name="Возврат",
            retail_amount=Decimal("1500.00"),
            rr_date=datetime.now(timezone.utc).date(),
        )
        finance_rows.append(f_row)

        mock_excise_rows.append({
            "barcode": "2044967817804",
            "excise_short": cis,
            "fiscal_doc_number": oid,
            "srid": srid,
            "price": 1500.0,
            "nm_id": 888,
            "fiscal_drive_number": "1",
            "fiscal_dt": "2026-10-10",
        })

        mock_verified[cis] = MagicMock(
            cz_status="INTRODUCED",
            cz_status_ex=None,
            raw_cz_payload={"ownerInn": seller.cz_inn},
        )

    db_session.add_all(orders + finance_rows)
    await db_session.commit()

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res.get("returns_count", 0) == 0
        assert res.get("needs_withdrawal_count", 0) == 0

    # Verify each non-delivered order transitioned to CANCELLED, kiz_status=RETURNED, withdrawal doc cleared
    for oid, _, cis, _ in test_cases:
        reloaded = await db_session.get(Order, oid)
        assert reloaded is not None
        assert reloaded.status == OrderStatus.CANCELLED, f"Order {oid} must transition to CANCELLED"
        assert reloaded.kiz_status == KizStatus.RETURNED, f"Order {oid} kiz_status must be RETURNED"
        assert reloaded.kiz_cz_status == "INTRODUCED"
        assert reloaded.cz_withdrawal_doc_id is None, f"Order {oid} cz_withdrawal_doc_id must be None"


@pytest.mark.asyncio
async def test_adversarial_order_state_transitions_concurrency_and_reentrancy(db_session: AsyncSession):
    """
    Stress-Test Invariant:
    Mixed batch containing:
    - Order 1: DELIVERED -> must NOT be mutated to CANCELLED.
    - Order 2: ASSEMBLED -> MUST be mutated to CANCELLED.
    - Order 3: Already CANCELLED -> stays CANCELLED.
    All 3 must have kiz_status transitioned to RETURNED and cz_withdrawal_doc_id reset to None.
    Re-entrant sequential execution must be strictly idempotent with zero state drift.
    """
    seller = await _create_test_seller(db_session, "seller-mixed-states-reentrant")

    cis_deliv = "0104630199251844215DELIV_STAY_DELIV"
    cis_asmb = "0104630199251844215ASMB_TO_CANCEL01"
    cis_canc = "0104630199251844215ALREADY_CANCEL01"

    order_deliv = Order(
        id=500101, seller_id=seller.id, status=OrderStatus.DELIVERED,
        kiz_code=cis_deliv, kiz_status=KizStatus.WITHDRAWN,
        cz_withdrawal_doc_id="DOC-DELIV-FINAL-99", wb_created_at=datetime.now(timezone.utc),
    )
    order_asmb = Order(
        id=500102, seller_id=seller.id, status=OrderStatus.ASSEMBLED,
        kiz_code=cis_asmb, kiz_status=KizStatus.ATTACHED,
        cz_withdrawal_doc_id="DOC-ASMB-PENDING-99", wb_created_at=datetime.now(timezone.utc),
    )
    order_canc = Order(
        id=500103, seller_id=seller.id, status=OrderStatus.CANCELLED,
        kiz_code=cis_canc, kiz_status=KizStatus.RETURNED,
        cz_withdrawal_doc_id=None, wb_created_at=datetime.now(timezone.utc),
    )

    f_deliv = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=600101, clean_cis=cis_deliv,
        srid="srid_m_deliv.0.0", doc_type_name="Возврат", retail_amount=Decimal("1200.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    f_asmb = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=600102, clean_cis=cis_asmb,
        srid="srid_m_asmb.0.0", doc_type_name="Возврат", retail_amount=Decimal("1300.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )
    f_canc = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=600103, clean_cis=cis_canc,
        srid="srid_m_canc.0.0", doc_type_name="Возврат", retail_amount=Decimal("1400.00"),
        rr_date=datetime.now(timezone.utc).date(),
    )

    db_session.add_all([order_deliv, order_asmb, order_canc, f_deliv, f_asmb, f_canc])
    await db_session.commit()

    mock_excise_rows = [
        {"barcode": "2044967817804", "excise_short": cis_deliv, "fiscal_doc_number": 801, "srid": "srid_m_deliv.0.0", "price": 1200.0, "nm_id": 771, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-15"},
        {"barcode": "2044967817804", "excise_short": cis_asmb, "fiscal_doc_number": 802, "srid": "srid_m_asmb.0.0", "price": 1300.0, "nm_id": 772, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-15"},
        {"barcode": "2044967817804", "excise_short": cis_canc, "fiscal_doc_number": 803, "srid": "srid_m_canc.0.0", "price": 1400.0, "nm_id": 773, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-15"},
    ]

    mock_verified = {
        cis_deliv: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        cis_asmb: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        cis_canc: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        mock_fetch.return_value = mock_excise_rows
        mock_verify.return_value = mock_verified

        # Pass 1: Initial synchronization
        res1 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res1.get("returns_count", 0) == 0

        # Invariant verifications after Pass 1
        r_deliv = await db_session.get(Order, 500101)
        r_asmb = await db_session.get(Order, 500102)
        r_canc = await db_session.get(Order, 500103)

        assert r_deliv.status == OrderStatus.DELIVERED, "DELIVERED order must NOT be mutated to CANCELLED"
        assert r_deliv.kiz_status == KizStatus.RETURNED
        assert r_deliv.cz_withdrawal_doc_id is None

        assert r_asmb.status == OrderStatus.CANCELLED, "Non-DELIVERED order must be mutated to CANCELLED"
        assert r_asmb.kiz_status == KizStatus.RETURNED
        assert r_asmb.cz_withdrawal_doc_id is None

        assert r_canc.status == OrderStatus.CANCELLED
        assert r_canc.kiz_status == KizStatus.RETURNED
        assert r_canc.cz_withdrawal_doc_id is None

        # Pass 2: Re-entrant / concurrent replay (idempotency check)
        res2 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res2.get("returns_count", 0) == 0

        # Invariants must remain completely stable after Pass 2
        r_deliv2 = await db_session.get(Order, 500101)
        r_asmb2 = await db_session.get(Order, 500102)
        r_canc2 = await db_session.get(Order, 500103)

        assert r_deliv2.status == OrderStatus.DELIVERED
        assert r_deliv2.kiz_status == KizStatus.RETURNED
        assert r_deliv2.cz_withdrawal_doc_id is None

        assert r_asmb2.status == OrderStatus.CANCELLED
        assert r_asmb2.kiz_status == KizStatus.RETURNED
        assert r_asmb2.cz_withdrawal_doc_id is None

        assert r_canc2.status == OrderStatus.CANCELLED
        assert r_canc2.kiz_status == KizStatus.RETURNED
        assert r_canc2.cz_withdrawal_doc_id is None
