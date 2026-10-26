"""
Empirical Challenger Test Suite for WB Warehouse Sales & Storno Returns (M2 Iteration 2).
Stress tests edge cases for:
1. Negative price return records
2. Storno records with empty/malformed CIS
3. Batch creation idempotency (double-processing warehouse sales)
4. Foreign INN and non-excise row skipping
Strict line limit: < 400 lines (PROJECT_RULES.md).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.kiz import BatchStatus, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import encrypt
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller


@pytest.fixture
async def db_session():
    """Isolated in-memory SQLite database session fixture."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _create_test_seller(session: AsyncSession, seller_id: str, cz_inn: str = "190207495060") -> Seller:
    seller = Seller(
        id=seller_id,
        name=f"Seller {seller_id}",
        wb_api_token_encrypted=encrypt("wb_test_token"),
        cz_inn=cz_inn,
        cz_token_encrypted=encrypt("cz_test_token"),
        is_active=True,
    )
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Suite 1: Negative Price Return Records
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_negative_price_in_excise_row_triggers_return_without_fin_report(db_session: AsyncSession):
    """Verify negative price in excise row identifies return even with NO finance report."""
    seller = await _create_test_seller(db_session, "s-neg-price-no-fin")
    cis = "0104630199251844215NEG_PRICE_PURE"
    excise_rows = [{
        "excise_short": cis, "srid": "srid_neg_pure.0.0", "price": -1750.50,
        "fiscal_doc_number": 901, "fiscal_drive_number": "111", "fiscal_dt": "2026-10-01",
    }]
    mock_kinfo = MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        m_fetch.return_value = excise_rows
        m_ver.return_value = {cis: mock_kinfo}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is True
        assert res["returns_count"] == 1
        assert res["sales_count"] == 0
        batch = await db_session.get(KizSignatureBatch, res["batch_id"])
        assert batch is not None
        assert len(batch.data_payload["returns"]) == 1
        item = batch.data_payload["returns"][0]
        assert item["action"] == "RETURN"
        assert item["doc_type"] == "LP_RETURN"


@pytest.mark.asyncio
async def test_adversarial_negative_price_introduced_kiz_skips_and_cancels_order(db_session: AsyncSession):
    """Verify negative price with INTRODUCED status cancels in-flight order and skips batch."""
    seller = await _create_test_seller(db_session, "s-neg-intro-cancel")
    cis = "0104630199251844215NEG_INTRO_CANCEL"
    order = Order(
        id=77011, seller_id=seller.id, name="In-flight Order",
        status=OrderStatus.DELIVERING, kiz_code=cis, kiz_status=KizStatus.ATTACHED,
        cz_withdrawal_doc_id="doc_prior_active", wb_created_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    await db_session.commit()

    excise_rows = [{"excise_short": cis, "price": -2100.0, "srid": "srid_cancel.0.0"}]
    mock_kinfo = MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = {cis: mock_kinfo}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["returns_count"] == 0
        await db_session.refresh(order)
        assert order.status == OrderStatus.CANCELLED
        assert order.kiz_status == KizStatus.RETURNED
        assert order.cz_withdrawal_doc_id is None


@pytest.mark.asyncio
async def test_adversarial_negative_price_introduced_preserves_delivered_status(db_session: AsyncSession):
    """Verify negative price INTRODUCED return does not downgrade DELIVERED order status."""
    seller = await _create_test_seller(db_session, "s-neg-intro-deliv")
    cis = "0104630199251844215NEG_INTRO_DELIV"
    order = Order(
        id=77022, seller_id=seller.id, name="Delivered Order",
        status=OrderStatus.DELIVERED, kiz_code=cis, kiz_status=KizStatus.WITHDRAWN,
        cz_withdrawal_doc_id="doc_completed_123", wb_created_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    await db_session.commit()

    excise_rows = [{"excise_short": cis, "price": -999.0, "srid": "srid_deliv.0.0"}]
    mock_kinfo = MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn})

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = {cis: mock_kinfo}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        await db_session.refresh(order)
        assert order.status == OrderStatus.DELIVERED
        assert order.kiz_status == KizStatus.RETURNED
        assert order.cz_withdrawal_doc_id is None


# ============================================================================
# Suite 2: Storno Records with Empty/Malformed CIS
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_storno_with_empty_and_whitespace_cises(db_session: AsyncSession):
    """Verify storno rows with None, empty string, or whitespace CIS are safely skipped."""
    seller = await _create_test_seller(db_session, "s-storno-empty-cis")
    fin_storno = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=9011,
        clean_cis="", srid="srid_storno_empty", doc_type_name="Сторно",
        retail_amount=Decimal("-1500.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(fin_storno)
    await db_session.commit()

    excise_rows = [
        {"excise_short": None, "srid": "srid_storno_empty", "price": -1500.0},
        {"excise_short": "", "srid": "srid_storno_empty", "price": -1500.0},
        {"excise_short": "   ", "srid": "srid_storno_empty", "price": -1500.0},
        {"excise_short": "\t\n", "srid": "srid_storno_empty", "price": -1500.0},
    ]

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = {}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["returns_count"] == 0
        assert res["sales_count"] == 0
        assert res["total_wb_records"] == 4


@pytest.mark.asyncio
async def test_adversarial_storno_with_malformed_cis_strings(db_session: AsyncSession):
    """Verify storno with corrupted/unverified CIS safely skips LP_RETURN batch."""
    seller = await _create_test_seller(db_session, "s-storno-malformed-cis")
    malformed_cis = "CORRUPTED_CIS_NOT_A_BARCODE"
    fin_storno = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=9012,
        clean_cis=malformed_cis, srid="srid_malformed", doc_type_name="Возврат",
        retail_amount=Decimal("1200.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(fin_storno)
    await db_session.commit()

    excise_rows = [{"excise_short": malformed_cis, "srid": "srid_malformed", "price": 1200.0}]

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        # True API returns None for corrupted CIS
        m_ver.return_value = {malformed_cis: None}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["returns_count"] == 0


# ============================================================================
# Suite 3: Batch Creation Idempotency (Double-Processing)
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_double_processing_idempotency_triple_run(db_session: AsyncSession):
    """Verify running 3 times consecutively with identical data creates exactly 1 batch."""
    seller = await _create_test_seller(db_session, "s-triple-idemp")
    sale_cis = "0104630199251844215IDEMP_S"
    ret_cis = "0104630199251844215IDEMP_R"

    fin_ret = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=9013,
        clean_cis=ret_cis, srid="srid_idemp_ret", doc_type_name="Возврат",
        retail_amount=Decimal("1800.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(fin_ret)
    await db_session.commit()

    excise_rows = [
        {"excise_short": sale_cis, "srid": "srid_idemp_s", "price": 1200.0, "fiscal_doc_number": 1, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
        {"excise_short": ret_cis, "srid": "srid_idemp_ret", "price": 1800.0, "fiscal_doc_number": 2, "fiscal_drive_number": "1", "fiscal_dt": "2026-10-01"},
    ]
    mock_ver = {
        sale_cis: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        ret_cis: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver, \
         patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock):
        m_fetch.return_value = excise_rows
        m_ver.return_value = mock_ver

        res1 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res1["created"] is True
        assert res1["sales_count"] == 1
        assert res1["returns_count"] == 1

        res2 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res2["created"] is False
        assert res2["sales_count"] == 0
        assert res2["returns_count"] == 0

        res3 = await process_warehouse_sales_for_seller(seller, db_session, days=30)
        assert res3["created"] is False

        stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
        batches = (await db_session.execute(stmt)).scalars().all()
        assert len(batches) == 1


@pytest.mark.asyncio
async def test_adversarial_idempotency_across_multiple_pending_batches(db_session: AsyncSession):
    """Verify deduplication detects items queued across multiple separate pending batches."""
    seller = await _create_test_seller(db_session, "s-multi-pending-batches")
    cis1 = "0104630199251844215MULTI_P1"
    cis2 = "0104630199251844215MULTI_P2"

    batch1 = KizSignatureBatch(
        seller_id=seller.id, filename="b1.json", source="wb_warehouse_sale",
        status=BatchStatus.PENDING_SIGNATURE, sales_count=1, total_count=1,
        data_payload={"withdrawals": [{"clean_cis": cis1, "action": "WITHDRAWAL"}], "returns": []},
    )
    batch2 = KizSignatureBatch(
        seller_id=seller.id, filename="b2.json", source="wb_warehouse_sale",
        status=BatchStatus.PENDING_SIGNATURE, returns_count=1, total_count=1,
        data_payload={"withdrawals": [], "returns": [{"clean_cis": cis2, "action": "RETURN"}]},
    )
    db_session.add_all([batch1, batch2])
    await db_session.commit()

    fin_ret = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=9014,
        clean_cis=cis2, srid="srid_m2", doc_type_name="Возврат",
        retail_amount=Decimal("1500.00"), rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(fin_ret)
    await db_session.commit()

    excise_rows = [
        {"excise_short": cis1, "srid": "srid_m1", "price": 1000.0},
        {"excise_short": cis2, "srid": "srid_m2", "price": 1500.0},
    ]
    mock_ver = {
        cis1: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
        cis2: MagicMock(cz_status="RETIRED", cz_status_ex=None, raw_cz_payload={"ownerInn": seller.cz_inn}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = mock_ver
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["sales_count"] == 0
        assert res["returns_count"] == 0


# ============================================================================
# Suite 4: Foreign INN and Non-Excise Row Skipping
# ============================================================================

@pytest.mark.asyncio
async def test_adversarial_foreign_inn_variations_skipped(db_session: AsyncSession):
    """Verify all variations of foreign INN ownership are isolated."""
    seller = await _create_test_seller(db_session, "s-foreign-variations", cz_inn="190207495060")
    cises = [f"0104630199251844215FOREIGN_{i}" for i in range(5)]

    excise_rows = [
        {"excise_short": cises[0], "srid": "srid_f0", "price": 1000.0},
        {"excise_short": cises[1], "srid": "srid_f1", "price": 1100.0},
        {"excise_short": cises[2], "srid": "srid_f2", "price": 1200.0},
        {"excise_short": cises[3], "srid": "srid_f3", "price": 1300.0},
        {"excise_short": cises[4], "srid": "srid_f4", "price": 1400.0},
    ]

    mock_ver = {
        cises[0]: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": "9714053621"}),
        cises[1]: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": "7721546864"}),
        cises[2]: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"ownerInn": " 7701234567 "}),
        cises[3]: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"owner_inn": "7701234567"}),
        cises[4]: MagicMock(cz_status="INTRODUCED", cz_status_ex=None, raw_cz_payload={"cisInfo": {"ownerInn": "7701234567"}}),
    }

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = mock_ver
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["other_owner_count"] == 5
        assert res["sales_count"] == 0
        assert res["returns_count"] == 0


@pytest.mark.asyncio
async def test_adversarial_non_excise_rows_ignored(db_session: AsyncSession):
    """Verify non-excise rows without valid excise_short are ignored without errors."""
    seller = await _create_test_seller(db_session, "s-non-excise-skip")
    excise_rows = [
        {"barcode": "2044967817804", "price": 1500.0, "srid": "srid_no_excise_1"},
        {"barcode": "2044967817804", "excise_short": None, "price": 1200.0},
        {"barcode": "2044967817804", "excise_short": "", "price": 1300.0},
        {},
    ]

    with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as m_fetch, \
         patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as m_ver:
        m_fetch.return_value = excise_rows
        m_ver.return_value = {}
        res = await process_warehouse_sales_for_seller(seller, db_session, days=30)

        assert res["created"] is False
        assert res["total_wb_records"] == 4
        assert res["sales_count"] == 0
        assert res["returns_count"] == 0
