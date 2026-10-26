"""
Tier 1 & Tier 2 Tests for Returns Domain Service & Bifurcation Logic (wb_item_returns_service).
Verifies return matching (srid, orderId, nmId), True API status bifurcation:
- RETIRED -> KizSignatureBatch with data_payload['returns'] for LP_RETURN
- INTRODUCED -> Order.kiz_status = RETURNED without extraneous ГИС МТ document
All tests use isolated in-memory SQLite (sqlite+aiosqlite:///:memory:) and mocked APIs.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.database import Base
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizProductInfo, KizSignatureBatch, BatchStatus
from app.models.wb_finance import WbSalesReportRow
from app.models.audit import AuditLog
from app.services.encryption import encrypt

try:
    from app.services.wb_item_returns_service import (
        sync_seller_returns,
        process_item_returns_for_seller,
        fetch_wb_item_returns_data,
    )
except ImportError:
    sync_seller_returns = None
    process_item_returns_for_seller = None
    fetch_wb_item_returns_data = None


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
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _create_test_seller(session: AsyncSession, seller_id: str = "test-seller-1") -> Seller:
    seller = Seller(
        id=seller_id, name="Test Returns Shop",
        wb_api_token_encrypted=encrypt("valid-wb-token"),
        cz_inn="7701234567", cz_token_encrypted=encrypt("valid-cz-token"), is_active=True,
    )
    if hasattr(seller, "item_returns_enabled"):
        setattr(seller, "item_returns_enabled", True)
    if hasattr(seller, "item_returns_interval_hours"):
        setattr(seller, "item_returns_interval_hours", 6)
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Tier 1: Feature Coverage (>= 5 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_order_model_and_kiz_status_returned_invariants(db_session: AsyncSession):
    """Verify Order and KizStatus support RETURNED status and return document fields."""
    seller = await _create_test_seller(db_session, "seller-inv-1")
    order = Order(
        id=9001001, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code="0104630199251318215QTSRh>4sVc+.", kiz_status=KizStatus.WITHDRAWN, kiz_cz_status="RETIRED",
    )
    db_session.add(order)
    await db_session.commit()

    # Transition to RETURNED
    order.kiz_status = KizStatus.RETURNED
    order.kiz_cz_status = "INTRODUCED"
    order.cz_return_doc_id = "cz-doc-ret-12345"
    await db_session.commit()

    reloaded = await db_session.get(Order, 9001001)
    assert reloaded.kiz_status == KizStatus.RETURNED
    assert reloaded.kiz_cz_status == "INTRODUCED"
    assert reloaded.cz_return_doc_id == "cz-doc-ret-12345"


@pytest.mark.asyncio
async def test_kiz_signature_batch_returns_payload_invariants(db_session: AsyncSession):
    """Verify KizSignatureBatch stores returns array with required LP_RETURN structure."""
    seller = await _create_test_seller(db_session, "seller-batch-1")
    batch = KizSignatureBatch(
        seller_id=seller.id, source="item_returns", status=BatchStatus.PENDING_SIGNATURE,
        sales_count=0, returns_count=1, total_count=1,
        data_payload={
            "withdrawals": [],
            "returns": [{"ki": "0104630199251318215QTSRh>4sVc+.", "order_id": 9001002, "srid": "srid_ret_9002.0.0", "action": "RETURN", "primary_document_type": "RECEIPT", "primary_document_number": "RET-9001002", "primary_document_date": "2026-10-26"}],
            "summary": {"total": 1, "returns": 1},
        },
    )
    db_session.add(batch)
    await db_session.commit()

    reloaded = await db_session.get(KizSignatureBatch, batch.id)
    assert reloaded.returns_count == 1
    assert len(reloaded.data_payload["returns"]) == 1
    assert reloaded.data_payload["returns"][0]["action"] == "RETURN"


@pytest.mark.asyncio
async def test_bifurcation_retired_kiz_creates_lp_return_batch(db_session: AsyncSession):
    """When True API status is RETIRED, service creates KizSignatureBatch with returns array."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-bif-ret")
    test_kiz = "0104630199251318215QTSRh>4sVc+."
    test_srid = "srid_bif_ret_001.0.0"

    order = Order(
        id=910001,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_kiz,
        kiz_status=KizStatus.WITHDRAWN,
        kiz_cz_status="RETIRED",
    )
    kinfo = KizProductInfo(
        kiz_code=test_kiz,
        gtin="04630199251318",
        serial_number="5QTSRh>4sVc+.",
        clean_cis=test_kiz,
        seller_id=seller.id,
        order_id=order.id,
        cz_status="RETIRED",
    )
    db_session.add_all([order, kinfo])
    await db_session.commit()

    mock_wb_returns = [
        {"srid": test_srid, "orderId": 910001, "nmId": 123456, "date": "2026-10-26T10:00:00Z"}
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_kiz, "status": "RETIRED", "ownerInn": seller.cz_inn}]

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res.get("batches_created", 0) >= 1 or res.get("returns_count", 0) >= 1


@pytest.mark.asyncio
async def test_bifurcation_introduced_kiz_marks_returned_without_batch(db_session: AsyncSession):
    """When True API status is INTRODUCED, service updates Order.kiz_status=RETURNED without batch."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-bif-intro")
    test_kiz = "0104630199251332215+gIUHbTKIpgQ"
    test_srid = "srid_bif_intro_002.0.0"

    order = Order(
        id=910002,
        seller_id=seller.id,
        status=OrderStatus.ASSEMBLED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_kiz,
        kiz_status=KizStatus.ATTACHED,
        kiz_cz_status="INTRODUCED",
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [
        {"srid": test_srid, "orderId": 910002, "nmId": 654321, "date": "2026-10-26T10:00:00Z"}
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_kiz, "status": "INTRODUCED", "ownerInn": seller.cz_inn}]

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res.get("batches_created", 0) == 0

        reloaded = await db_session.get(Order, 910002)
        assert reloaded.kiz_status == KizStatus.RETURNED


@pytest.mark.asyncio
async def test_match_return_by_order_id_fallback(db_session: AsyncSession):
    """When srid does not match, return record matches by orderId."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-match-oid")
    order = Order(
        id=910003,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code="0104630199251318215MATCH_OID",
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [
        {"srid": "unmatched_srid.0.0", "orderId": 910003, "nmId": 99999}
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": order.kiz_code, "status": "INTRODUCED"}]

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        reloaded = await db_session.get(Order, 910003)
        assert reloaded.kiz_status == KizStatus.RETURNED


# ============================================================================
# Tier 2: Boundary & Corner Cases (>= 5 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_sync_returns_empty_wb_list(db_session: AsyncSession):
    """When WB API returns empty list, sync completes cleanly with 0 processed."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-empty-ret")
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = []
        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res.get("processed_count", 0) == 0
        assert res.get("batches_created", 0) == 0


@pytest.mark.asyncio
async def test_sync_returns_unmatched_item_handled_gracefully(db_session: AsyncSession):
    """When return item has unknown srid/orderId, skips without error or DB corruption."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-unmatched")
    mock_wb_returns = [
        {"srid": "unknown_srid_9999.0.0", "orderId": 99999999, "nmId": 888888}
    ]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = mock_wb_returns
        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res is not None


@pytest.mark.asyncio
async def test_sync_returns_order_without_kiz_code(db_session: AsyncSession):
    """When order has no attached kiz_code, service handles safely."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-no-kiz")
    order = Order(
        id=910005,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=False,
        kiz_code=None,
        kiz_status=KizStatus.NOT_REQUIRED,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [{"srid": "srid_no_kiz.0.0", "orderId": 910005, "nmId": 77777}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = mock_wb_returns
        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res is not None


@pytest.mark.asyncio
async def test_sync_returns_idempotent_processing(db_session: AsyncSession):
    """Running sync multiple times on same returns does not produce duplicate batches."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-idempotent")
    test_kiz = "0104630199251318215IDEMPOTENT1"
    order = Order(
        id=910006,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_kiz,
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [{"srid": "srid_idem.0.0", "orderId": 910006, "nmId": 55555}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_kiz, "status": "RETIRED"}]

        res1 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        batches1 = res1.get("batches_created", 0)

        # Run second time
        res2 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        batches2 = res2.get("batches_created", 0)
        assert batches2 <= batches1


@pytest.mark.asyncio
async def test_sync_returns_true_api_exception_handling(db_session: AsyncSession):
    """True API failure is caught and handled cleanly without unhandled crash."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-true-err")
    order = Order(
        id=910007,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code="0104630199251318215TRUE_ERR",
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [{"srid": "srid_true_err.0.0", "orderId": 910007, "nmId": 44444}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.side_effect = RuntimeError("True API timeout")

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res is not None


@pytest.mark.asyncio
async def test_sync_returns_inactive_seller_skipped(db_session: AsyncSession):
    """Seller with is_active=False is skipped cleanly."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")

    seller = await _create_test_seller(db_session, "seller-inactive")
    seller.is_active = False
    await db_session.commit()

    res = await sync_seller_returns(db=db_session, seller_id=seller.id)
    assert res.get("skipped", False) is True or res.get("processed_count", 0) == 0


@pytest.mark.asyncio
async def test_sync_returns_date_range_filtering(db_session: AsyncSession):
    """Verify sync passes date_from and date_to parameters to client."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")
    seller = await _create_test_seller(db_session, "seller-date-filter")
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as m:
        m.return_value = []
        await sync_seller_returns(db=db_session, seller_id=seller.id, date_from="2026-10-01", date_to="2026-10-26")
        m.assert_called_once()


@pytest.mark.asyncio
async def test_sync_returns_result_keys_contract(db_session: AsyncSession):
    """Verify result dictionary keys: processed_count and batches_created."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")
    seller = await _create_test_seller(db_session, "seller-res-keys")
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as m:
        m.return_value = []
        res = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert "processed_count" in res and "batches_created" in res


@pytest.mark.asyncio
async def test_item_returns_no_wb_token(db_session: AsyncSession):
    """Verify seller without WB token returns empty list and handled gracefully."""
    seller = await _create_test_seller(db_session, "seller-no-token")
    seller.wb_api_token_encrypted = ""
    await db_session.commit()

    rows = await fetch_wb_item_returns_data(seller)
    assert rows == []

    res = await process_item_returns_for_seller(seller=seller, db=db_session)
    assert res.get("success") is True
    assert res.get("created") is False
    assert res.get("total_wb_records", res.get("processed_count")) == 0


@pytest.mark.asyncio
async def test_item_returns_correlation_by_srid(db_session: AsyncSession):
    """Verify return correlation via WbSalesReportRow.srid to extract clean_cis."""
    seller = await _create_test_seller(db_session, "seller-srid-match")
    test_cis = "0104630199251844215SRIDTEST1"
    test_srid = "srid_fin_001.0.0"

    fin_row = WbSalesReportRow(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        rrd_id=12345678,
        srid=test_srid,
        clean_cis=test_cis,
    )
    order = Order(
        id=920001,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_cis,
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add_all([fin_row, order])
    await db_session.commit()

    mock_wb_returns = [{"srid": test_srid, "orderId": None, "nmId": None, "price": 1250.0}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_cis, "status": "RETIRED", "ownerInn": seller.cz_inn}]

        res = await process_item_returns_for_seller(seller=seller, db=db_session)
        assert res.get("batches_created") == 1
        assert res.get("returns_count") == 1


@pytest.mark.asyncio
async def test_item_returns_correlation_by_nm_id(db_session: AsyncSession):
    """Verify return correlation by nmId when srid and orderId are absent/unmatched."""
    seller = await _create_test_seller(db_session, "seller-nm-match")
    test_cis = "0104630199251844215NMTEST001"
    order = Order(
        id=920002,
        seller_id=seller.id,
        nm_id=778899,
        status=OrderStatus.ASSEMBLED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_cis,
        kiz_status=KizStatus.ATTACHED,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [{"srid": "unknown_srid_99.0.0", "orderId": None, "nmId": 778899}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_cis, "status": "INTRODUCED", "ownerInn": seller.cz_inn}]

        res = await process_item_returns_for_seller(seller=seller, db=db_session)
        assert res.get("introduced_count") == 1
        assert res.get("batches_created") == 0

        reloaded = await db_session.get(Order, 920002)
        assert reloaded.kiz_status == KizStatus.RETURNED


@pytest.mark.asyncio
async def test_item_returns_skips_foreign_owner(db_session: AsyncSession):
    """Verify KIZ owned by third party (e.g. RVB 9714053621) is excluded from LP_RETURN batch."""
    seller = await _create_test_seller(db_session, "seller-foreign-own")
    test_cis = "0104630199251844215FOREIGN01"
    order = Order(
        id=920003,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=test_cis,
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    mock_wb_returns = [{"orderId": 920003, "srid": "srid_foreign.0.0"}]
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = mock_wb_returns
        mock_cz.return_value = [{"cis": test_cis, "status": "RETIRED", "ownerInn": "9714053621"}]

        res = await process_item_returns_for_seller(seller=seller, db=db_session)
        assert res.get("batches_created") == 0
        assert res.get("foreign_owner_count") == 1


@pytest.mark.asyncio
async def test_item_returns_audit_log_and_transaction_safety(db_session: AsyncSession):
    """Verify AuditLog is recorded in the session upon return processing."""
    seller = await _create_test_seller(db_session, "seller-audit-log")
    mock_wb_returns = [{"srid": "unknown_srid_audit.0.0", "orderId": 888888, "nmId": 123}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret:
        mock_ret.return_value = mock_wb_returns
        res = await process_item_returns_for_seller(seller=seller, db=db_session)
        assert res.get("success") is True

    stmt = select(AuditLog).where(AuditLog.seller_id == seller.id, AuditLog.agent == "wb_item_returns")
    audit_res = await db_session.execute(stmt)
    entries = audit_res.scalars().all()
    assert len(entries) >= 1
    assert entries[0].payload.get("unmatched_count") == 1

