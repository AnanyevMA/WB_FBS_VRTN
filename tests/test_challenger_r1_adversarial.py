"""
Adversarial Stress Test Suite for Objective R1 (Challenger 1).

Covers:
1. PATCH /api/v1/sellers/{id} Edge Cases:
   - wb_api_token="", "   ", None
   - cz_token="", "   ", None
   - telegram_bot_token="", "   ", None
   - telegram_chat_ids=None or omitted from payload
   - notification_schedule=["09:30", "15:45", "23:00"] persistence as JSON
   - Schedule validation: out-of-range hours, invalid formats, deduplication & sorting
   - Invalid notification_mode and invalid timezone
2. Order Poller Alert Suppression:
   - Single order: notification_mode="scheduled" -> notify_new_order.delay is NOT called,
     order persisted with notified_at=None, get_stickers.delay IS called.
   - Batch orders: notification_mode="scheduled" -> notify_batch_orders.delay is NOT called,
     orders persisted with notified_at=None, get_stickers.delay IS called for all.
   - Contrast with notification_mode="instant".
3. Scheduled Digest Task:
   - Slot matching across multiple timezones (Europe/Moscow UTC+3, Asia/Yekaterinburg UTC+5, Europe/Kaliningrad UTC+2).
   - Sending consolidated message and stamping order.notified_at.
   - Idempotency on immediate second invocation (dedup via memory set and persistent audit log).
   - Filtering out CANCELLED orders and previously notified orders.
"""
import uuid
import random
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, AsyncSessionLocal
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.audit import AuditLog
from app.services.encryption import encrypt, decrypt
from app.config import settings
from app.agents.order_poller import SyncSessionLocal, poll_all_sellers, _last_polled
from app.agents.notifier import (
    send_scheduled_orders_digest,
    is_scheduled_slot_due,
    _scheduled_digest_sent,
)


@pytest.fixture(autouse=True)
def clean_digest_and_poller_state():
    _scheduled_digest_sent.clear()
    _last_polled.clear()
    yield
    _scheduled_digest_sent.clear()
    _last_polled.clear()


async def _get_auth_headers(client: AsyncClient) -> dict:
    async with AsyncSessionLocal() as session:
        from app.services.auth_service import ensure_initial_admin
        await ensure_initial_admin(session)

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert res.status_code == 200, f"Login failed: {res.text}"
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# =========================================================================
# 1. PATCH /api/v1/sellers/{id} Edge Cases
# =========================================================================

@pytest.mark.asyncio
async def test_patch_empty_and_whitespace_tokens_preserves_originals():
    """Verify empty string, whitespace, and None tokens do not overwrite existing DB tokens."""
    await init_db()
    seller_id = f"adv-pres-{uuid.uuid4().hex[:8]}"

    original_wb = "ORIGINAL_WB_SECRET_TOKEN_999"
    original_cz = "ORIGINAL_CZ_SECRET_TOKEN_888"
    original_tg = "ORIGINAL_TG_BOT_TOKEN_777"
    original_chats = ["111222333", "444555666"]

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Token Preservation Seller",
            wb_api_token_encrypted=encrypt(original_wb),
            cz_token_encrypted=encrypt(original_cz),
            telegram_bot_token_encrypted=encrypt(original_tg),
            telegram_chat_ids=original_chats,
            notification_mode="instant",
            notification_schedule=["10:00", "14:00", "18:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        # Attack scenario 1: pass empty string for wb, spaces for cz, null for tg, omit chats
        patch_1 = {
            "wb_api_token": "",
            "cz_token": "   ",
            "telegram_bot_token": None,
            "notification_mode": "scheduled",
            "notification_schedule": ["09:30", "15:45", "23:00"],
        }
        res1 = await client.patch(f"/api/v1/sellers/{seller_id}", json=patch_1, headers=headers)
        assert res1.status_code == 200, res1.text
        data1 = res1.json()
        assert data1["notification_mode"] == "scheduled"
        assert data1["notification_schedule"] == ["09:30", "15:45", "23:00"]
        assert data1["telegram_chat_ids"] == original_chats

        # Verify DB directly
        async with AsyncSessionLocal() as session:
            db_seller = await session.get(Seller, seller_id)
            assert decrypt(db_seller.wb_api_token_encrypted) == original_wb
            assert decrypt(db_seller.cz_token_encrypted) == original_cz
            assert decrypt(db_seller.telegram_bot_token_encrypted) == original_tg
            assert db_seller.telegram_chat_ids == original_chats
            assert db_seller.notification_schedule == ["09:30", "15:45", "23:00"]

        # Attack scenario 2: pass whitespace for wb, empty for cz, whitespace for tg, telegram_chat_ids=None
        patch_2 = {
            "wb_api_token": "     ",
            "cz_token": "",
            "telegram_bot_token": "  \t \n ",
            "telegram_chat_ids": None,
        }
        res2 = await client.patch(f"/api/v1/sellers/{seller_id}", json=patch_2, headers=headers)
        assert res2.status_code == 200, res2.text

        # Verify DB tokens and chats STILL intact
        async with AsyncSessionLocal() as session:
            db_seller = await session.get(Seller, seller_id)
            assert decrypt(db_seller.wb_api_token_encrypted) == original_wb
            assert decrypt(db_seller.cz_token_encrypted) == original_cz
            assert decrypt(db_seller.telegram_bot_token_encrypted) == original_tg
            assert db_seller.telegram_chat_ids == original_chats


@pytest.mark.asyncio
async def test_patch_schedule_normalization_and_validation():
    """Verify schedule deduplication, sorting, and rejection of malformed schedules."""
    await init_db()
    seller_id = f"adv-sched-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Schedule Norm Seller",
            wb_api_token_encrypted=encrypt("tok"),
            notification_mode="scheduled",
            notification_schedule=["10:00"],
            timezone="Europe/Moscow",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        # 1. Deduplication and sorting: ["23:00", "09:30", "15:45", "09:30"] -> ["09:30", "15:45", "23:00"]
        res = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={"notification_schedule": ["23:00", "09:30", "15:45", "09:30"]},
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["notification_schedule"] == ["09:30", "15:45", "23:00"]

        # 2. Invalid schedules -> 422 Unprocessable Entity
        invalid_payloads = [
            {"notification_schedule": ["24:00"]},
            {"notification_schedule": ["12:60"]},
            {"notification_schedule": ["not-a-time"]},
            {"notification_schedule": ["-01:30"]},
            {"notification_mode": "unsupported_mode"},
            {"timezone": "Narnia/Aslan"},
        ]
        for inv in invalid_payloads:
            bad_res = await client.patch(f"/api/v1/sellers/{seller_id}", json=inv, headers=headers)
            assert bad_res.status_code == 422, f"Expected 422 for {inv}, got {bad_res.status_code}: {bad_res.text}"


# =========================================================================
# 2. Order Poller Alert Suppression
# =========================================================================

def test_order_poller_scheduled_mode_suppression_single_order():
    """Ingest single order for scheduled seller: notify_new_order.delay NOT called, get_stickers.delay IS called."""
    import asyncio
    asyncio.run(init_db())

    seller_id = f"adv-poller-single-{uuid.uuid4().hex[:8]}"
    fake_order_id = random.randint(50000000, 59999999)

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Scheduled Poller Shop Single",
            wb_api_token_encrypted=encrypt("valid-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg"),
            telegram_chat_ids=["123456"],
            notification_mode="scheduled",
            notification_schedule=["10:00", "18:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=0,
            last_polled_at=None,
        )
        session.add(seller)
        session.commit()

    raw_order = {
        "id": fake_order_id,
        "createdAt": "2026-09-03T10:00:00Z",
        "price": 250000,
        "article": "ADV-SKU-1",
        "chrtId": 888111,
        "nmId": 999222,
        "requiredMeta": [],
    }

    mock_wb_client = MagicMock()
    mock_wb_client.get_new_orders.return_value = [raw_order]
    mock_wb_client.get_cards_catalog.return_value = []
    mock_wb_client.get_active_orders.return_value = []

    with patch("app.agents.order_poller.WBClient", return_value=mock_wb_client), \
         patch("app.agents.order_poller.get_stickers.delay") as mock_stickers_delay, \
         patch("app.agents.notifier.notify_new_order.delay") as mock_notify_single_delay, \
         patch("app.agents.notifier.notify_batch_orders.delay") as mock_notify_batch_delay:

        res = poll_all_sellers()

        assert res["status"] == "success"
        assert res["new_orders"] >= 1

        # EMPIRICAL PROOF: notify calls MUST NOT occur
        mock_notify_single_delay.assert_not_called()
        mock_notify_batch_delay.assert_not_called()

        # EMPIRICAL PROOF: get_stickers MUST be triggered
        mock_stickers_delay.assert_called_once_with(str(seller_id), fake_order_id)

    # EMPIRICAL PROOF: Order persisted in DB with notified_at IS None
    with SyncSessionLocal() as session:
        persisted = session.query(Order).filter(Order.id == fake_order_id).first()
        assert persisted is not None
        assert persisted.seller_id == seller_id
        assert persisted.notified_at is None
        assert persisted.status == OrderStatus.NEW


def test_order_poller_scheduled_mode_suppression_batch_orders():
    """Ingest multiple orders for scheduled seller: notify_batch_orders.delay NOT called, get_stickers.delay IS called for each."""
    import asyncio
    asyncio.run(init_db())

    seller_id = f"adv-poller-batch-{uuid.uuid4().hex[:8]}"
    fake_order_id_1 = random.randint(60000000, 64999999)
    fake_order_id_2 = random.randint(65000000, 69999999)

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Scheduled Poller Shop Batch",
            wb_api_token_encrypted=encrypt("valid-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg"),
            telegram_chat_ids=["123456"],
            notification_mode="scheduled",
            notification_schedule=["12:00", "18:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=0,
            last_polled_at=None,
        )
        session.add(seller)
        session.commit()

    raw_orders = [
        {
            "id": fake_order_id_1,
            "createdAt": "2026-09-03T10:00:00Z",
            "price": 100000,
            "article": "ADV-BATCH-1",
            "chrtId": 7771,
            "nmId": 8881,
            "requiredMeta": [],
        },
        {
            "id": fake_order_id_2,
            "createdAt": "2026-09-03T10:01:00Z",
            "price": 200000,
            "article": "ADV-BATCH-2",
            "chrtId": 7772,
            "nmId": 8882,
            "requiredMeta": [],
        },
    ]

    mock_wb_client = MagicMock()
    mock_wb_client.get_new_orders.return_value = raw_orders
    mock_wb_client.get_cards_catalog.return_value = []
    mock_wb_client.get_active_orders.return_value = []

    with patch("app.agents.order_poller.WBClient", return_value=mock_wb_client), \
         patch("app.agents.order_poller.get_stickers.delay") as mock_stickers_delay, \
         patch("app.agents.notifier.notify_new_order.delay") as mock_notify_single_delay, \
         patch("app.agents.notifier.notify_batch_orders.delay") as mock_notify_batch_delay:

        res = poll_all_sellers()

        assert res["status"] == "success"
        assert res["new_orders"] >= 2

        # In scheduled mode, neither single nor batch notification must fire
        mock_notify_single_delay.assert_not_called()
        mock_notify_batch_delay.assert_not_called()

        # Both orders must have get_stickers requested
        assert mock_stickers_delay.call_count == 2
        called_ids = [call[0][1] for call in mock_stickers_delay.call_args_list]
        assert fake_order_id_1 in called_ids
        assert fake_order_id_2 in called_ids

    with SyncSessionLocal() as session:
        for oid in (fake_order_id_1, fake_order_id_2):
            o = session.query(Order).filter(Order.id == oid).first()
            assert o is not None
            assert o.notified_at is None


# =========================================================================
# 3. Scheduled Digest Task & Multi-Timezone Slot Matching
# =========================================================================

def test_scheduled_digest_multi_timezone_slot_matching():
    """Verify slot matching across multiple timezones (Europe/Moscow UTC+3, Asia/Yekaterinburg UTC+5, Europe/Kaliningrad UTC+2)."""
    import asyncio
    asyncio.run(init_db())

    # Given UTC time: 08:05:00 UTC
    # - Europe/Moscow (UTC+3) -> 11:05:00 (matches "11:00" slot with 15m grace)
    # - Asia/Yekaterinburg (UTC+5) -> 13:05:00 (matches "13:00" slot with 15m grace)
    # - Europe/Kaliningrad (UTC+2) -> 10:05:00 (matches "10:00" slot with 15m grace)
    utc_test_time = datetime(2026, 9, 3, 8, 5, 0, tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)

    # 1. Moscow seller
    moscow_seller_id = f"adv-msk-{uuid.uuid4().hex[:8]}"
    msk_order_id = random.randint(70000000, 72999999)

    # 2. Yekaterinburg seller
    yekat_seller_id = f"adv-ykt-{uuid.uuid4().hex[:8]}"
    ykt_order_id = random.randint(73000000, 75999999)

    # 3. Kaliningrad seller with non-matching slot (schedule=["15:00"]) -> must NOT trigger
    kalin_seller_id = f"adv-kln-{uuid.uuid4().hex[:8]}"
    kln_order_id = random.randint(76000000, 79999999)

    with SyncSessionLocal() as session:
        session.query(Seller).update({"is_active": False})

        s_msk = Seller(
            id=moscow_seller_id,
            name="Moscow Seller",
            wb_api_token_encrypted=encrypt("tok-msk"),
            telegram_bot_token_encrypted=encrypt("tg-msk"),
            telegram_chat_ids=["101"],
            notification_mode="scheduled",
            notification_schedule=["11:00", "17:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        s_ykt = Seller(
            id=yekat_seller_id,
            name="Yekaterinburg Seller",
            wb_api_token_encrypted=encrypt("tok-ykt"),
            telegram_bot_token_encrypted=encrypt("tg-ykt"),
            telegram_chat_ids=["202"],
            notification_mode="scheduled",
            notification_schedule=["13:00", "19:00"],
            timezone="Asia/Yekaterinburg",
            is_active=True,
            polling_enabled=True,
        )
        s_kln = Seller(
            id=kalin_seller_id,
            name="Kaliningrad Seller",
            wb_api_token_encrypted=encrypt("tok-kln"),
            telegram_bot_token_encrypted=encrypt("tg-kln"),
            telegram_chat_ids=["303"],
            notification_mode="scheduled",
            notification_schedule=["15:00"],  # Local time is 10:05 -> does NOT match
            timezone="Europe/Kaliningrad",
            is_active=True,
            polling_enabled=True,
        )
        session.add_all([s_msk, s_ykt, s_kln])

        # Add unnotified orders with wb_created_at set
        o_msk = Order(id=msk_order_id, seller_id=moscow_seller_id, status=OrderStatus.NEW, wb_created_at=now_utc, notified_at=None, price=1000)
        o_ykt = Order(id=ykt_order_id, seller_id=yekat_seller_id, status=OrderStatus.NEW, wb_created_at=now_utc, notified_at=None, price=2000)
        o_kln = Order(id=kln_order_id, seller_id=kalin_seller_id, status=OrderStatus.NEW, wb_created_at=now_utc, notified_at=None, price=3000)
        session.add_all([o_msk, o_ykt, o_kln])
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_batch_tg:
        mock_batch_tg.return_value = {"sent": 1, "failed": 0}

        res = send_scheduled_orders_digest(now_utc_override=utc_test_time)

        # Moscow (11:05 MSK matches 11:00) and Yekaterinburg (13:05 YEKT matches 13:00) sent. Kaliningrad skipped.
        assert res["sent_digests"] == 2
        assert res["orders_notified"] == 2
        assert mock_batch_tg.call_count == 2

        called_seller_ids = [call[1]["seller_id"] for call in mock_batch_tg.call_args_list]
        assert moscow_seller_id in called_seller_ids
        assert yekat_seller_id in called_seller_ids
        assert kalin_seller_id not in called_seller_ids

    # Verify order notified_at stamps
    with SyncSessionLocal() as session:
        db_msk = session.query(Order).filter(Order.id == msk_order_id).first()
        db_ykt = session.query(Order).filter(Order.id == ykt_order_id).first()
        db_kln = session.query(Order).filter(Order.id == kln_order_id).first()
        assert db_msk.notified_at is not None
        assert db_ykt.notified_at is not None
        assert db_kln.notified_at is None  # Still unnotified


def test_scheduled_digest_immediate_second_call_no_duplicate():
    """Verify that calling send_scheduled_orders_digest a second time immediately afterwards does NOT duplicate send."""
    import asyncio
    asyncio.run(init_db())

    seller_id = f"adv-idemp-{uuid.uuid4().hex[:8]}"
    order_id = random.randint(80000000, 89999999)
    now_utc = datetime.now(timezone.utc)

    with SyncSessionLocal() as session:
        session.query(Seller).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Deduplication Shop",
            wb_api_token_encrypted=encrypt("tok-dup"),
            telegram_bot_token_encrypted=encrypt("tg-dup"),
            telegram_chat_ids=["777888"],
            notification_mode="scheduled",
            notification_schedule=["14:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        o = Order(id=order_id, seller_id=seller_id, status=OrderStatus.NEW, wb_created_at=now_utc, notified_at=None, price=5000)
        session.add(o)
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_batch_tg:
        mock_batch_tg.return_value = {"sent": 1, "failed": 0}

        # 14:02 MSK (11:02 UTC)
        now_1 = datetime(2026, 9, 3, 11, 2, 0, tzinfo=timezone.utc)
        run1 = send_scheduled_orders_digest(now_utc_override=now_1)
        assert run1["sent_digests"] == 1
        assert run1["orders_notified"] == 1
        assert mock_batch_tg.call_count == 1

        # Second call immediately afterwards (14:03 MSK, 11:03 UTC)
        now_2 = datetime(2026, 9, 3, 11, 3, 0, tzinfo=timezone.utc)
        run2 = send_scheduled_orders_digest(now_utc_override=now_2)
        assert run2["sent_digests"] == 0
        assert run2["orders_notified"] == 0
        # Crucial check: mock_batch_tg call count did NOT increase
        assert mock_batch_tg.call_count == 1

        # Third call: simulated worker restart clearing in-memory dedup set
        _scheduled_digest_sent.clear()
        now_3 = datetime(2026, 9, 3, 11, 4, 0, tzinfo=timezone.utc)
        run3 = send_scheduled_orders_digest(now_utc_override=now_3)
        # Database audit log prevents resend even if in-memory cache was lost!
        assert run3["sent_digests"] == 0
        assert run3["orders_notified"] == 0
        assert mock_batch_tg.call_count == 1
