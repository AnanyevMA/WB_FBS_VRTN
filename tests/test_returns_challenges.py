"""
Empirical Adversarial Challenge Test Suite for Milestone 1:
Target: app/services/wb_item_returns_service.py (WB Item Returns & Invariants)

Challenges Covered:
- Challenge B1: Third-Party INN Poisoning (RVB 9714053621 vs Seller INN)
- Challenge B2: Pre-Sale Cancellation Injection (INTRODUCED KIZ -> Cancel withdrawal, RETURNED, No batch)
- Challenge B3: Dirty / Crypto-Tailed KIZ (AIM prefix, GS delimiters, 91/92 tails, serial 91/92 traps)
- Challenge B4: Concurrent Re-entrant Sync (Sequential & Concurrent idempotency, zero duplicates)
"""
import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.audit import AuditLog
from app.models.kiz import BatchStatus, KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import encrypt
from app.services.kiz_service import normalize_kiz_light_industry, parse_kiz_code
from app.services.wb_item_returns_service import (
    get_already_queued_return_cises,
    process_item_returns_for_seller,
    sync_seller_returns,
)


@pytest.fixture
async def shared_db_factory():
    """Create in-memory SQLite engine with StaticPool and return session maker."""
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

    yield session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(shared_db_factory):
    """Single session fixture for individual tests."""
    async with shared_db_factory() as session:
        yield session


async def _create_seller(
    session: AsyncSession,
    seller_id: str = "seller-challenger-1",
    cz_inn: str = "7700000000",
) -> Seller:
    """Helper to create seller with valid encrypted tokens."""
    seller = Seller(
        id=seller_id,
        name="Challenger Test Store",
        wb_api_token_encrypted=encrypt("valid-wb-token"),
        cz_inn=cz_inn,
        cz_token_encrypted=encrypt("valid-cz-token"),
        is_active=True,
    )
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Challenge B1: Third-Party INN Poisoning (RVB 9714053621 vs Seller INN)
# ============================================================================

@pytest.mark.asyncio
async def test_challenge_b1_rvb_third_party_inn_poisoning_prevented(db_session: AsyncSession):
    """
    Challenge B1.1: Return record has KIZ owned by RVB (9714053621) while seller is 7700000000.
    Verify:
    - NO LP_RETURN batch is created (batches_created == 0, batch_id is None).
    - foreign_owner_count == 1 in return result and in AuditLog.
    - Zero KizSignatureBatch rows exist in database.
    """
    seller = await _create_seller(db_session, seller_id="seller-b1-1", cz_inn="7700000000")
    kiz_code = "0104630199251318215QTSRh>4sVc+."
    srid_val = "srid_b1_rvb_001.0.0"

    order = Order(
        id=700101,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=kiz_code,
        kiz_status=KizStatus.WITHDRAWN,
        kiz_cz_status="RETIRED",
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [
        {"srid": srid_val, "orderId": 700101, "nmId": 88001, "price": 1800.0, "returnReason": "Брак"}
    ]

    # True API indicates KIZ is RETIRED but owned by Wildberries/RVB (9714053621)
    rvb_inn = "9714053621"
    cz_response = [
        {
            "cis": kiz_code,
            "status": "RETIRED",
            "ownerInn": rvb_inn,
            "ownerName": "ООО РВБ",
        }
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    # Assertions for Challenge B1
    assert res["success"] is True
    assert res["batches_created"] == 0, "No LP_RETURN batch must be created for foreign-owned KIZ"
    assert res["batch_id"] is None
    assert res["returns_count"] == 0
    assert res["foreign_owner_count"] == 1, "foreign_owner_count must be 1"

    # Verify database state: NO batches created
    stmt_b = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
    batches = (await db_session.execute(stmt_b)).scalars().all()
    assert len(batches) == 0, f"Expected 0 batches in DB, found {len(batches)}"

    # Verify AuditLog recorded foreign owner detection
    stmt_a = select(AuditLog).where(
        AuditLog.seller_id == seller.id,
        AuditLog.agent == "wb_item_returns",
    )
    logs = (await db_session.execute(stmt_a)).scalars().all()
    assert len(logs) >= 1
    assert logs[-1].payload.get("foreign_owner_count") == 1
    assert logs[-1].payload.get("returns_needing_cz") == 0


@pytest.mark.asyncio
async def test_challenge_b1_mixed_rvb_and_seller_kiz_filters_poisoned_item(db_session: AsyncSession):
    """
    Challenge B1.2: Two return records:
    - Item 1: Owned by RVB (9714053621) -> MUST be rejected from batch.
    - Item 2: Owned by Seller (7700000000) -> MUST be included in LP_RETURN batch.
    Verify batch payload contains ONLY seller's KIZ.
    """
    seller = await _create_seller(db_session, seller_id="seller-b1-2", cz_inn="7700000000")
    kiz_rvb = "0104630199251318215RVB_POISON01"
    kiz_own = "0104630199251318215OWN_LEGIT001"

    order_rvb = Order(
        id=700201, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=kiz_rvb, kiz_status=KizStatus.WITHDRAWN,
    )
    order_own = Order(
        id=700202, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=kiz_own, kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add_all([order_rvb, order_own])
    await db_session.commit()

    wb_returns = [
        {"orderId": 700201, "nmId": 88002, "price": 1000.0, "srid": "srid.rvb.0.0"},
        {"orderId": 700202, "nmId": 88003, "price": 2000.0, "srid": "srid.own.0.0"},
    ]

    cz_response = [
        {"cis": kiz_rvb, "status": "RETIRED", "ownerInn": "9714053621"},
        {"cis": kiz_own, "status": "RETIRED", "ownerInn": "7700000000"},
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["success"] is True
    assert res["batches_created"] == 1
    assert res["returns_count"] == 1
    assert res["foreign_owner_count"] == 1

    batch = await db_session.get(KizSignatureBatch, res["batch_id"])
    assert batch is not None
    assert batch.returns_count == 1
    items = batch.data_payload["returns"]
    assert len(items) == 1
    assert items[0]["clean_cis"] == kiz_own
    assert items[0]["kiz_code"] == kiz_own
    # Poisoned RVB code is nowhere in batch returns
    assert all(i["clean_cis"] != kiz_rvb for i in items)


@pytest.mark.asyncio
async def test_challenge_b1_nested_cisinfo_and_integer_inn_coercion(db_session: AsyncSession):
    """
    Challenge B1.3: True API returns ownerInn as integer 9714053621 or nested inside cisInfo.
    Verify coercion detects foreign ownership correctly.
    """
    seller = await _create_seller(db_session, seller_id="seller-b1-3", cz_inn="7700000000")
    kiz_code = "0104630199251318215NESTED_INN01"

    order = Order(
        id=700301, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=kiz_code, kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [{"orderId": 700301, "nmId": 88004}]
    # Nested cisInfo with integer ownerInn
    cz_response = [
        {
            "cisInfo": {
                "cis": kiz_code,
                "status": "RETIRED",
                "ownerInn": 9714053621,  # int type
            }
        }
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["batches_created"] == 0
    assert res["foreign_owner_count"] == 1


# ============================================================================
# Challenge B2: Pre-Sale Cancellation Injection (INTRODUCED KIZ Invariants)
# ============================================================================

@pytest.mark.asyncio
async def test_challenge_b2_pre_sale_cancellation_cancels_withdrawal_and_sets_returned(db_session: AsyncSession):
    """
    Challenge B2.1: Pre-sale cancellation where KIZ is in INTRODUCED status.
    Verify:
    - Planned withdrawal is cancelled: Order.cz_withdrawal_doc_id is reset to None.
    - Order.kiz_status transitions to KizStatus.RETURNED.
    - Order.kiz_cz_status transitions to "INTRODUCED".
    - Order.status transitions to OrderStatus.CANCELLED (if not DELIVERED).
    - Linked KizProductInfo is updated to "INTRODUCED".
    - NO LP_RETURN batch is generated (batches_created == 0, batch_id is None).
    """
    seller = await _create_seller(db_session, seller_id="seller-b2-1")
    kiz_code = "0104630199251332215+gIUHbTKIpgQ"

    order = Order(
        id=701001,
        seller_id=seller.id,
        status=OrderStatus.ASSEMBLED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=kiz_code,
        kiz_status=KizStatus.ATTACHED,
        kiz_cz_status="INTRODUCED",
        cz_withdrawal_doc_id="DOC-PLANNED-WITHDRAWAL-777",  # Planned withdrawal staged
    )
    kinfo = KizProductInfo(
        kiz_code=kiz_code,
        gtin="04630199251332",
        serial_number="+gIUHbTKIpgQ",
        clean_cis=kiz_code,
        seller_id=seller.id,
        order_id=order.id,
        cz_status="ATTACHED",
    )
    db_session.add_all([order, kinfo])
    await db_session.commit()

    wb_returns = [{"orderId": 701001, "nmId": 7701, "returnReason": "Отказ клиента до получения"}]
    cz_response = [{"cis": kiz_code, "status": "INTRODUCED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    # Verify returns service result
    assert res["success"] is True
    assert res["batches_created"] == 0, "No LP_RETURN batch must be created for INTRODUCED KIZ"
    assert res["batch_id"] is None
    assert res["returns_count"] == 0
    assert res["introduced_count"] == 1

    # Verify Order state invariants
    reloaded_order = await db_session.get(Order, 701001)
    assert reloaded_order is not None
    assert reloaded_order.cz_withdrawal_doc_id is None, "Planned withdrawal must be cancelled (cz_withdrawal_doc_id = None)"
    assert reloaded_order.kiz_status == KizStatus.RETURNED, "Order.kiz_status must be RETURNED"
    assert reloaded_order.kiz_cz_status == "INTRODUCED"
    assert reloaded_order.status == OrderStatus.CANCELLED, "Pre-sale cancellation must transition order to CANCELLED"

    # Verify KizProductInfo state invariants
    stmt_k = select(KizProductInfo).where(KizProductInfo.clean_cis == kiz_code)
    reloaded_kinfo = (await db_session.execute(stmt_k)).scalar_one_or_none()
    assert reloaded_kinfo is not None
    assert reloaded_kinfo.cz_status == "INTRODUCED"
    assert reloaded_kinfo.is_valid is True
    assert "отказ до выкупа" in (reloaded_kinfo.validation_message or "").lower()

    # Verify zero batches in DB
    stmt_b = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
    batches = (await db_session.execute(stmt_b)).scalars().all()
    assert len(batches) == 0


@pytest.mark.asyncio
async def test_challenge_b2_delivered_order_retains_delivered_status_on_introduced_kiz(db_session: AsyncSession):
    """
    Challenge B2.2: If order was already in DELIVERED status when INTRODUCED return occurs,
    it must NOT transition to CANCELLED (DELIVERED status is immutable),
    while kiz_status becomes RETURNED and withdrawal is cleared.
    """
    seller = await _create_seller(db_session, seller_id="seller-b2-2")
    kiz_code = "0104630199251332215+DELIV_INTROQ"

    order = Order(
        id=701002,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,  # Already delivered
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=kiz_code,
        kiz_status=KizStatus.WITHDRAWN,
        cz_withdrawal_doc_id="DOC-WITHDRAWAL-FINAL",
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [{"orderId": 701002, "nmId": 7702}]
    cz_response = [{"cis": kiz_code, "status": "INTRODUCED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["batches_created"] == 0
    reloaded_order = await db_session.get(Order, 701002)
    assert reloaded_order.status == OrderStatus.DELIVERED, "DELIVERED status must NOT be overridden to CANCELLED"
    assert reloaded_order.kiz_status == KizStatus.RETURNED
    assert reloaded_order.cz_withdrawal_doc_id is None


# ============================================================================
# Challenge B3: Dirty / Crypto-Tailed KIZ Sanitization
# ============================================================================

@pytest.mark.asyncio
async def test_challenge_b3_dirty_aim_prefix_and_gs_crypto_tails_cleaned(db_session: AsyncSession):
    """
    Challenge B3.1: Return record contains dirty KIZ with:
    - AIM prefix: ']d2'
    - FNC1 / GS separators: '\\x1d'
    - Crypto tail: '\\x1d91EE06\\x1d92AQ==...'
    Verify clean 31-character extraction and payload purity.
    """
    seller = await _create_seller(db_session, seller_id="seller-b3-1")
    expected_clean_cis = "0104630199251318215QTSRh>4sVc+."
    assert len(expected_clean_cis) == 31

    # Dirty KIZ as scanned from 2D barcode
    dirty_kiz = f"]d2{expected_clean_cis}\x1d91EE06\x1d92AQ==/xyz123=="

    # Order has dirty KIZ in DB
    order = Order(
        id=702001,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=dirty_kiz,
        kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    # WB returns record passes direct dirty KIZ in kiz_code
    wb_returns = [
        {
            "orderId": 702001,
            "nmId": 99001,
            "kiz_code": dirty_kiz,
            "price": 2500.0,
        }
    ]

    cz_response = [{"cis": expected_clean_cis, "status": "RETIRED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["success"] is True
    assert res["batches_created"] == 1
    assert res["returns_count"] == 1

    batch = await db_session.get(KizSignatureBatch, res["batch_id"])
    assert batch is not None
    item = batch.data_payload["returns"][0]

    # Verify clean 31-character CIS contract
    assert item["clean_cis"] == expected_clean_cis
    assert item["kiz_code"] == expected_clean_cis
    assert item["ki"] == expected_clean_cis
    assert len(item["clean_cis"]) == 31
    assert item["clean_cis"].startswith("01")
    assert item["clean_cis"][16:18] == "21"
    assert "\x1d" not in item["clean_cis"]
    assert "]d2" not in item["clean_cis"]
    assert "91EE06" not in item["clean_cis"]


@pytest.mark.asyncio
async def test_challenge_b3_space_separated_and_parentheses_delimiters(db_session: AsyncSession):
    """
    Challenge B3.2: Return record contains parentheses GS1 delimiters and space-separated crypto-tails:
    - '(01)04630199251318(21)5QTSRh>4sVc+. 91EE06 92tail...'
    Verify clean 31-character extraction.
    """
    seller = await _create_seller(db_session, seller_id="seller-b3-2")
    expected_clean_cis = "0104630199251318215QTSRh>4sVc+."

    dirty_input = "(01)04630199251318(21)5QTSRh>4sVc+. 91EE06 92crypto_tail_here=="

    # Order exists with matching clean CIS
    order = Order(
        id=702002, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=expected_clean_cis, kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [
        {"orderId": 702002, "nmId": 99002, "cis": dirty_input, "price": 1200.0}
    ]

    cz_response = [{"cis": expected_clean_cis, "status": "RETIRED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["batches_created"] == 1
    batch = await db_session.get(KizSignatureBatch, res["batch_id"])
    item = batch.data_payload["returns"][0]
    assert item["clean_cis"] == expected_clean_cis
    assert len(item["clean_cis"]) == 31


@pytest.mark.asyncio
async def test_challenge_b3_serial_number_crypto_trap_not_truncated(db_session: AsyncSession):
    """
    Challenge B3.3: Serial number contains '91' and '92' substrings within its 13 characters:
    '010463019925131821591TRh>4s92+.'
    Verify normalization does NOT prematurely truncate at the 91/92 digits in the serial.
    """
    seller = await _create_seller(db_session, seller_id="seller-b3-3")
    trap_cis = "010463019925131821591TRh>4s92+."
    assert len(trap_cis) == 31
    assert "91" in trap_cis[18:]
    assert "92" in trap_cis[18:]

    order = Order(
        id=702003, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=trap_cis, kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [{"orderId": 702003, "nmId": 99003, "excise_short": trap_cis, "price": 3100.0}]
    cz_response = [{"cis": trap_cis, "status": "RETIRED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    assert res["batches_created"] == 1
    batch = await db_session.get(KizSignatureBatch, res["batch_id"])
    item = batch.data_payload["returns"][0]
    assert item["clean_cis"] == trap_cis, f"Expected {trap_cis}, got {item['clean_cis']}"
    assert len(item["clean_cis"]) == 31


# ============================================================================
# Challenge B4: Concurrent Re-entrant Sync & Idempotency
# ============================================================================

@pytest.mark.asyncio
async def test_challenge_b4_sequential_reentrant_sync_zero_duplicate_batches(db_session: AsyncSession):
    """
    Challenge B4.1: Re-entrant sequential sync calls for the exact same seller and date window.
    Call 1: Generates 1 batch with 2 return items.
    Call 2: Identical WB response -> 0 duplicate batches created, already_queued_count == 2.
    Total batches in DB remains exactly 1.
    """
    seller = await _create_seller(db_session, seller_id="seller-b4-1")
    kiz_1 = "0104630199251318215REENTRANT001"
    kiz_2 = "0104630199251318215REENTRANT002"

    order_1 = Order(
        id=703001, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=kiz_1, kiz_status=KizStatus.WITHDRAWN,
    )
    order_2 = Order(
        id=703002, seller_id=seller.id, status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc), kiz_required=True,
        kiz_code=kiz_2, kiz_status=KizStatus.WITHDRAWN,
    )
    db_session.add_all([order_1, order_2])
    await db_session.commit()

    wb_returns = [
        {"orderId": 703001, "nmId": 6601, "srid": "srid.reent.1.0"},
        {"orderId": 703002, "nmId": 6602, "srid": "srid.reent.2.0"},
    ]

    cz_response = [
        {"cis": kiz_1, "status": "RETIRED", "ownerInn": seller.cz_inn},
        {"cis": kiz_2, "status": "RETIRED", "ownerInn": seller.cz_inn},
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        # Call 1: First sync run
        res_1 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res_1["batches_created"] == 1
        assert res_1["returns_count"] == 2
        batch_1_id = res_1["batch_id"]

        # Call 2: Immediate re-entrant sync with identical data
        res_2 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res_2["batches_created"] == 0, "Second sync must NOT create duplicate batch"
        assert res_2["batch_id"] is None
        assert res_2["already_queued_count"] == 2, "Both KIZs must be recognized as already queued"

    # Verify database integrity: exactly 1 batch exists
    stmt_b = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller.id)
    all_batches = (await db_session.execute(stmt_b)).scalars().all()
    assert len(all_batches) == 1
    assert all_batches[0].id == batch_1_id
    assert all_batches[0].returns_count == 2


@pytest.mark.asyncio
async def test_challenge_b4_overlapping_returns_partial_idempotency(db_session: AsyncSession):
    """
    Challenge B4.2: Partially overlapping return sets across consecutive sync runs.
    Sync 1: Items [A, B] -> Batch 1 created with items [A, B].
    Sync 2: Items [B, C] -> Batch 2 created with ONLY item [C]. Item [B] skipped as already queued.
    Total return items in DB across batches == 3 unique items.
    """
    seller = await _create_seller(db_session, seller_id="seller-b4-2")
    kiz_a = "0104630199251318215OVERLAP_AAA1"
    kiz_b = "0104630199251318215OVERLAP_BBB2"
    kiz_c = "0104630199251318215OVERLAP_CCC3"

    order_a = Order(id=703101, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_required=True, kiz_code=kiz_a, kiz_status=KizStatus.WITHDRAWN)
    order_b = Order(id=703102, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_required=True, kiz_code=kiz_b, kiz_status=KizStatus.WITHDRAWN)
    order_c = Order(id=703103, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_created_at=datetime.now(timezone.utc), kiz_required=True, kiz_code=kiz_c, kiz_status=KizStatus.WITHDRAWN)
    db_session.add_all([order_a, order_b, order_c])
    await db_session.commit()

    cz_response = [
        {"cis": kiz_a, "status": "RETIRED", "ownerInn": seller.cz_inn},
        {"cis": kiz_b, "status": "RETIRED", "ownerInn": seller.cz_inn},
        {"cis": kiz_c, "status": "RETIRED", "ownerInn": seller.cz_inn},
    ]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_cz.return_value = cz_response

        # Run 1: Items [A, B]
        mock_ret.return_value = [
            {"orderId": 703101, "nmId": 501},
            {"orderId": 703102, "nmId": 502},
        ]
        res_1 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res_1["batches_created"] == 1
        assert res_1["returns_count"] == 2

        # Run 2: Items [B, C] (B overlaps)
        mock_ret.return_value = [
            {"orderId": 703102, "nmId": 502},
            {"orderId": 703103, "nmId": 503},
        ]
        res_2 = await sync_seller_returns(db=db_session, seller_id=seller.id)
        assert res_2["batches_created"] == 1
        assert res_2["returns_count"] == 1, "Only item C should be queued"
        assert res_2["already_queued_count"] == 1, "Item B should be detected as already queued"

        batch_2 = await db_session.get(KizSignatureBatch, res_2["batch_id"])
        assert batch_2.returns_count == 1
        assert batch_2.data_payload["returns"][0]["clean_cis"] == kiz_c


@pytest.mark.asyncio
async def test_challenge_b4_concurrent_independent_sync_calls(shared_db_factory):
    """
    Challenge B4.3: Two concurrent async tasks execute sync_seller_returns simultaneously
    using independent AsyncSessions on the shared SQLite database.
    Verify:
    - At most ONE batch is created for the common items.
    - Zero duplicate items across created batches.
    - No unhandled exceptions or corrupt batch states.
    """
    # Setup seller and orders in a pre-test session
    async with shared_db_factory() as session:
        seller = await _create_seller(session, seller_id="seller-b4-3")
        kiz_concur = "0104630199251318215CONCURRENT01"
        order = Order(
            id=703201, seller_id=seller.id, status=OrderStatus.DELIVERED,
            wb_created_at=datetime.now(timezone.utc), kiz_required=True,
            kiz_code=kiz_concur, kiz_status=KizStatus.WITHDRAWN,
        )
        kinfo = KizProductInfo(
            kiz_code=kiz_concur,
            gtin="04630199251318",
            serial_number="5CONCURRENT01",
            clean_cis=kiz_concur,
            seller_id=seller.id,
            order_id=order.id,
            cz_status="RETIRED",
        )
        session.add_all([order, kinfo])
        await session.commit()

    wb_returns = [{"orderId": 703201, "nmId": 9999}]
    cz_response = [{"cis": kiz_concur, "status": "RETIRED", "ownerInn": seller.cz_inn}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.return_value = cz_response

        async def run_worker_sync():
            async with shared_db_factory() as session:
                return await sync_seller_returns(db=session, seller_id="seller-b4-3")

        # Run both sync tasks concurrently
        results = await asyncio.gather(run_worker_sync(), run_worker_sync(), return_exceptions=True)

    # Check for unexpected exceptions
    for r in results:
        assert not isinstance(r, Exception), f"Concurrent sync failed with exception: {r}"

    # Verify in DB: check all created batches
    async with shared_db_factory() as session:
        stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == "seller-b4-3")
        res = await session.execute(stmt)
        batches = res.scalars().all()

        total_returns_queued = sum(b.returns_count for b in batches)
        # Idempotency requirement: KIZ must not be queued more than once!
        assert total_returns_queued <= 1, f"Expected at most 1 return queued, found {total_returns_queued}"


@pytest.mark.asyncio
async def test_challenge_true_api_failure_must_not_falsely_classify_as_introduced(db_session: AsyncSession):
    """
    Vulnerability Challenge: True API is temporarily unreachable or throws error.
    An order was DELIVERED and WITHDRAWN.
    WB reports item return.
    If True API check fails (kinfo is None), the system must NOT falsely assume
    status is INTRODUCED, must NOT mark Order.kiz_status = RETURNED without confirmation,
    and must NOT corrupt the state.
    """
    seller = await _create_seller(db_session, seller_id="seller-vuln-1")
    kiz_code = "0104630199251318215VULN_TRUE_ERR"
    order = Order(
        id=704001,
        seller_id=seller.id,
        status=OrderStatus.DELIVERED,
        wb_created_at=datetime.now(timezone.utc),
        kiz_required=True,
        kiz_code=kiz_code,
        kiz_status=KizStatus.WITHDRAWN,
        kiz_cz_status="RETIRED",
    )
    db_session.add(order)
    await db_session.commit()

    wb_returns = [{"orderId": 704001, "nmId": 12121}]

    with patch("app.services.wb_analytics_client.WBAnalyticsClient.get_item_returns", new_callable=AsyncMock) as mock_ret, \
         patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
        mock_ret.return_value = wb_returns
        mock_cz.side_effect = RuntimeError("True API 503 Service Unavailable")

        res = await sync_seller_returns(db=db_session, seller_id=seller.id)

    reloaded = await db_session.get(Order, 704001)
    # If True API failed, we must NOT mark it as INTRODUCED!
    assert reloaded.kiz_cz_status != "INTRODUCED", "True API failure must not fabricate INTRODUCED status"
    assert reloaded.kiz_status != KizStatus.RETURNED, "True API failure must not advance kiz_status to RETURNED"

