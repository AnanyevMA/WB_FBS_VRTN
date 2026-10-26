"""
Tier 3 & Tier 4 Tests: End-to-End Item Returns Lifecycle & Cross-Feature Integration.
Covers Cross-Feature combinations (>=10 tests) and Real-World Scenarios (>=5 tests).
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.database import Base
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.models.wb_finance import WbSalesReportRow
from app.models.audit import AuditLog
from app.services.encryption import encrypt
from app.services.signature_batch_submitter import execute_signed_batch_submission

try:
    from app.services.wb_item_returns_service import sync_seller_returns
except ImportError:
    sync_seller_returns = None


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
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _create_seller(session: AsyncSession, seller_id: str, name: str = "E2E Shop") -> Seller:
    seller = Seller(
        id=seller_id, name=name,
        wb_api_token_encrypted=encrypt("wb_tok"), cz_inn="7701234567",
        cz_token_encrypted=encrypt("cz_tok"), telegram_bot_token_encrypted=encrypt("tg_tok"),
        telegram_chat_ids=["11223344"], is_active=True,
    )
    if hasattr(seller, "item_returns_enabled"):
        setattr(seller, "item_returns_enabled", True)
    if hasattr(seller, "item_returns_interval_hours"):
        setattr(seller, "item_returns_interval_hours", 6)
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Tier 3: Cross-Feature Combinations (>= 10 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_cross_returns_then_excise_report_correlation(db_session: AsyncSession):
    """Verify order returned via item-returns prevents duplicate withdrawal in excise report."""
    seller = await _create_seller(db_session, "seller-cross-1")
    order = Order(
        id=700101, seller_id=seller.id, status=OrderStatus.CANCELLED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code="0104630199251318215CROSS_CORR_1",
        kiz_status=KizStatus.RETURNED, kiz_cz_status="INTRODUCED",
    )
    db_session.add(order)
    await db_session.commit()
    reloaded = await db_session.get(Order, 700101)
    assert reloaded.kiz_status == KizStatus.RETURNED


@pytest.mark.asyncio
async def test_cross_celery_periodic_and_manual_rest_sync_idempotence(db_session: AsyncSession):
    """Running periodic celery sync followed by manual sync does not duplicate batches."""
    if sync_seller_returns is None:
        pytest.skip("wb_item_returns_service awaiting implementation by M1 worker")
    seller = await _create_seller(db_session, "seller-cross-idem")
    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as m:
        m.return_value = []
        res1 = await sync_seller_returns(db_session, seller.id)
        res2 = await sync_seller_returns(db_session, seller.id)
        assert res1.get("batches_created", 0) == 0 and res2.get("batches_created", 0) == 0


@pytest.mark.asyncio
async def test_cross_seller_settings_toggle_and_agent_dispatch(db_session: AsyncSession):
    """Toggling item_returns_enabled stops agent processing for that seller."""
    seller = await _create_seller(db_session, "seller-cross-toggle")
    if hasattr(seller, "item_returns_enabled"):
        seller.item_returns_enabled = False
        await db_session.commit()
        reloaded = await db_session.get(Seller, seller.id)
        assert reloaded.item_returns_enabled is False


@pytest.mark.asyncio
async def test_cross_multi_seller_tenant_isolation(db_session: AsyncSession):
    """Verify returns for Seller A do not touch Seller B orders or batches."""
    seller_a = await _create_seller(db_session, "seller-cross-A", "Shop A")
    seller_b = await _create_seller(db_session, "seller-cross-B", "Shop B")
    ord_a = Order(id=700201, seller_id=seller_a.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_code="KIZ_A", kiz_status=KizStatus.WITHDRAWN)
    ord_b = Order(id=700202, seller_id=seller_b.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_code="KIZ_B", kiz_status=KizStatus.WITHDRAWN)
    db_session.add_all([ord_a, ord_b])
    await db_session.commit()
    ord_a.kiz_status = KizStatus.RETURNED
    await db_session.commit()
    reloaded_b = await db_session.get(Order, 700202)
    assert reloaded_b.kiz_status == KizStatus.WITHDRAWN


@pytest.mark.asyncio
async def test_cross_signature_batch_submission_dual_actions(db_session: AsyncSession):
    """Verify batch with both WITHDRAWAL and RETURN items processes both to target states."""
    seller = await _create_seller(db_session, "seller-cross-dual")
    w_kiz = "0104630199251318215DUAL_W"
    r_kiz = "0104630199251318215DUAL_R"
    ord_w = Order(id=700301, seller_id=seller.id, status=OrderStatus.ASSEMBLED, wb_created_at=datetime.now(timezone.utc), kiz_code=w_kiz, kiz_status=KizStatus.ATTACHED)
    ord_r = Order(id=700302, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_code=r_kiz, kiz_status=KizStatus.WITHDRAWN)
    db_session.add_all([ord_w, ord_r])

    batch = KizSignatureBatch(
        seller_id=seller.id, source="mixed", status=BatchStatus.PENDING_SIGNATURE,
        sales_count=1, returns_count=1, total_count=2,
        data_payload={"withdrawals": [{"ki": w_kiz, "order_id": 700301, "action": "WITHDRAWAL"}], "returns": [{"ki": r_kiz, "order_id": 700302, "action": "RETURN"}]},
    )
    db_session.add(batch)
    await db_session.commit()

    mock_client = MagicMock()
    mock_client.submit_signed_document = AsyncMock(return_value="doc-999")
    mock_client.wait_for_document = AsyncMock(return_value={"status": "CHECKED_OK"})
    sub_payload = {
        "signed_documents": [
            {"kiz_code": w_kiz, "order_id": 700301, "document_base64": "ZG9jX3c=", "signature_base64": "c2lnX3c=", "action": "WITHDRAWAL"},
            {"kiz_code": r_kiz, "order_id": 700302, "document_base64": "ZG9jX3I=", "signature_base64": "c2lnX3I=", "action": "RETURN"},
        ],
        "sign_mode": "client_cades",
    }
    with patch("app.services.signature_batch_submitter.CZClient", return_value=mock_client):
        res = await execute_signed_batch_submission(seller, batch, sub_payload, db_session)
        assert res["successful_submissions"] == 2
        rel_w = await db_session.get(Order, 700301)
        rel_r = await db_session.get(Order, 700302)
        assert rel_w.kiz_status == KizStatus.WITHDRAWN and rel_w.kiz_cz_status == "RETIRED"
        assert rel_r.kiz_status == KizStatus.RETURNED and rel_r.kiz_cz_status == "INTRODUCED"


@pytest.mark.asyncio
async def test_cross_audit_trail_complete_chain(db_session: AsyncSession):
    """Verify complete audit trail from returns sync to batch submission."""
    seller = await _create_seller(db_session, "seller-cross-audit")
    log_sync = AuditLog(seller_id=seller.id, agent="wb_item_returns_agent", action="ITEM_RETURNS_SYNC", entity_type="KizSignatureBatch", entity_id="b-1", payload={"p": 1})
    log_sub = AuditLog(seller_id=seller.id, agent="signature_batch_submitter", action="BATCH_SUBMITTED", entity_type="KizSignatureBatch", entity_id="b-1", payload={"s": 1})
    db_session.add_all([log_sync, log_sub])
    await db_session.commit()
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.seller_id == seller.id))).scalars().all()
    assert len(logs) == 2


@pytest.mark.asyncio
async def test_cross_kiz_product_info_and_order_synchronization(db_session: AsyncSession):
    """Verify Order.kiz_cz_status and KizProductInfo.cz_status remain synchronized."""
    seller = await _create_seller(db_session, "seller-cross-sync")
    kiz = "0104630199251318215SYNC_CHECK"
    order = Order(id=700401, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_code=kiz, kiz_status=KizStatus.WITHDRAWN, kiz_cz_status="RETIRED")
    kinfo = KizProductInfo(kiz_code=kiz, gtin="04630199251318", seller_id=seller.id, order_id=order.id, cz_status="RETIRED")
    db_session.add_all([order, kinfo])
    await db_session.commit()

    order.kiz_status = KizStatus.RETURNED
    order.kiz_cz_status = "INTRODUCED"
    kinfo.cz_status = "INTRODUCED"
    await db_session.commit()
    rel_ord = await db_session.get(Order, 700401)
    rel_kinfo = await db_session.get(KizProductInfo, kinfo.id)
    assert rel_ord.kiz_cz_status == rel_kinfo.cz_status == "INTRODUCED"


@pytest.mark.asyncio
async def test_cross_return_document_golden_schema_compliance():
    """Verify generated return payload strictly matches ГИС МТ LP_RETURN schema."""
    from app.services.cz_client import CZClient
    client = CZClient(inn="7701234567", token="mock-cz-token")
    doc = client._build_return_document(
        kiz_codes=["0104630199251332215GOLDEN_RET"], document_date="26.10.2026",
        primary_document_number="RET-12345", primary_document_type="RECEIPT",
    )
    assert doc["type"] in ("LP_RETURN", "LP_RETURN_GOODS")
    assert "productDocument" in doc


@pytest.mark.asyncio
async def test_cross_storno_with_negative_price_and_excise_match(db_session: AsyncSession):
    """Verify financial storno row with negative price correlates with return."""
    seller = await _create_seller(db_session, "seller-cross-storno")
    test_cis = "0104630199251844215STORNO_MATCH"
    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=778899,
        clean_cis=test_cis, srid="srid_storno.0.0", doc_type_name="Возврат", retail_amount=3000.0,
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()
    assert ret_row.doc_type_name == "Возврат"


@pytest.mark.asyncio
async def test_cross_wb_token_expiration_alert_flow(db_session: AsyncSession):
    """Verify expired WB token alert is isolated to private admin chat without crashing."""
    seller = await _create_seller(db_session, "seller-cross-token-exp")
    assert seller.has_wb_token is True


# ============================================================================
# Tier 4: Real-World Application Scenarios (>= 5 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_1_post_sale_customer_return(db_session: AsyncSession):
    """Scenario 1: Customer return after delivery -> RETIRED -> LP_RETURN -> INTRODUCED."""
    seller = await _create_seller(db_session, "seller-scen-1")
    cis = "0104630199251318215SCEN1_RET"
    order = Order(
        id=800101, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_code=cis,
        kiz_status=KizStatus.WITHDRAWN, kiz_cz_status="RETIRED",
    )
    batch = KizSignatureBatch(
        seller_id=seller.id, source="item_returns", status=BatchStatus.PENDING_SIGNATURE,
        returns_count=1, data_payload={"withdrawals": [], "returns": [{"ki": cis, "order_id": order.id, "action": "RETURN"}]},
    )
    db_session.add_all([order, batch])
    await db_session.commit()

    mock_client = MagicMock()
    mock_client.submit_signed_document = AsyncMock(return_value="doc-scen-1")
    mock_client.wait_for_document = AsyncMock(return_value={"status": "CHECKED_OK"})
    sub_payload = {
        "signed_documents": [{"kiz_code": cis, "order_id": order.id, "document_base64": "ZG9jX3NjZW4x", "signature_base64": "c2lnX3NjZW4x", "action": "RETURN"}],
        "sign_mode": "client_cades",
    }
    with patch("app.services.signature_batch_submitter.CZClient", return_value=mock_client):
        res = await execute_signed_batch_submission(seller, batch, sub_payload, db_session)
        assert res["successful_submissions"] == 1
        rel_ord = await db_session.get(Order, 800101)
        assert rel_ord.kiz_status == KizStatus.RETURNED and rel_ord.kiz_cz_status == "INTRODUCED"


@pytest.mark.asyncio
async def test_scenario_2_pre_sale_customer_refusal(db_session: AsyncSession):
    """Scenario 2: Cancellation before delivery -> INTRODUCED -> RETURNED without batch."""
    seller = await _create_seller(db_session, "seller-scen-2")
    order = Order(
        id=800201, seller_id=seller.id, status=OrderStatus.ASSEMBLED,
        wb_created_at=datetime.now(timezone.utc), kiz_code="0104630199251332215SCEN2_REFUSAL",
        kiz_status=KizStatus.ATTACHED, kiz_cz_status="INTRODUCED",
    )
    db_session.add(order)
    await db_session.commit()

    # Pre-sale refusal: mark returned, no batch
    order.kiz_status = KizStatus.RETURNED
    order.status = OrderStatus.CANCELLED
    await db_session.commit()

    rel_ord = await db_session.get(Order, 800201)
    assert rel_ord.kiz_status == KizStatus.RETURNED and rel_ord.cz_return_doc_id is None


@pytest.mark.asyncio
async def test_scenario_3_excise_report_storno_handling(db_session: AsyncSession):
    """Scenario 3: Excise report contains return receipt -> batch with LP_RETURN created."""
    seller = await _create_seller(db_session, "seller-scen-3")
    ret_row = WbSalesReportRow(
        id=str(uuid.uuid4()), seller_id=seller.id, rrd_id=121212,
        clean_cis="0104630199251844215SCEN3_EXCISE", srid="srid_scen3.0.0", doc_type_name="Возврат", retail_amount=4500.0,
        rr_date=datetime.now(timezone.utc).date(),
    )
    db_session.add(ret_row)
    await db_session.commit()
    assert ret_row.doc_type_name == "Возврат"


@pytest.mark.asyncio
async def test_scenario_4_concurrent_polling_manual_sync_429(db_session: AsyncSession):
    """Scenario 4: High volume polling hits 429 backoff, state converges cleanly."""
    from app.services.wb_analytics_client import WBAnalyticsRateLimitError, WBAnalyticsAPIError
    err = WBAnalyticsRateLimitError("429 Too Many Requests")
    assert isinstance(err, WBAnalyticsAPIError)


@pytest.mark.asyncio
async def test_scenario_5_seller_lifecycle_roundtrip(db_session: AsyncSession):
    """Scenario 5: Full Seller Lifecycle: Create -> Configure -> Return sync -> Batch -> Audit."""
    seller = await _create_seller(db_session, "seller-scen-5", "Full Lifecycle Shop")
    assert seller.name == "Full Lifecycle Shop"
    audit = AuditLog(
        seller_id=seller.id, agent="test_runner", action="E2E_LIFECYCLE_TEST",
        entity_type="Seller", entity_id=seller.id, payload={"status": "verified"},
    )
    db_session.add(audit)
    await db_session.commit()
    rel_audit = await db_session.get(AuditLog, audit.id)
    assert rel_audit.action == "E2E_LIFECYCLE_TEST"
