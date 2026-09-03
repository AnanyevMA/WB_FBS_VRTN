"""
Tests for Scheduled Orders Digest Task (app.agents.notifier.send_scheduled_orders_digest)

Verifies:
- Slot matching logic with grace window and timezone conversion.
- Only sellers with notification_mode="scheduled" are processed.
- Only unnotified orders (notified_at IS NULL) and non-cancelled orders are batched.
- Successful batch notification stamps order.notified_at = datetime.now(timezone.utc).
- Idempotency: multiple invocations within the same schedule slot send only once.
- Direct helper unit test for is_scheduled_slot_due.
"""
import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from app.database import init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.services.encryption import encrypt
from app.agents.order_poller import SyncSessionLocal
from app.agents.notifier import (
    send_scheduled_orders_digest,
    is_scheduled_slot_due,
    _scheduled_digest_sent,
)


@pytest.fixture(autouse=True)
def setup_db_and_digest():
    import asyncio
    asyncio.run(init_db())
    _scheduled_digest_sent.clear()
    yield
    _scheduled_digest_sent.clear()


def test_is_scheduled_slot_due_helper():
    schedule = ["09:00", "14:00", "18:30"]

    # Exact match
    slot = is_scheduled_slot_due(schedule, datetime(2026, 9, 3, 14, 0, 0))
    assert slot == "14:00"

    # Within 15-minute grace window
    slot = is_scheduled_slot_due(schedule, datetime(2026, 9, 3, 14, 12, 30))
    assert slot == "14:00"

    # Outside grace window (15m01s)
    slot = is_scheduled_slot_due(schedule, datetime(2026, 9, 3, 14, 15, 1))
    assert slot is None

    # Before slot
    slot = is_scheduled_slot_due(schedule, datetime(2026, 9, 3, 13, 59, 0))
    assert slot is None

    # Empty schedule
    slot = is_scheduled_slot_due([], datetime(2026, 9, 3, 14, 0, 0))
    assert slot is None


def test_send_scheduled_orders_digest_dispatches_and_stamps_orders():
    import random
    seller_id = f"sched-sel-{uuid.uuid4().hex[:8]}"
    order1_id = random.randint(10000000, 12000000)
    order2_id = random.randint(12000001, 14000000)
    order_cancelled_id = random.randint(14000001, 16000000)
    order_already_notified_id = random.randint(16000001, 18000000)

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Digest Test Shop",
            wb_api_token_encrypted=encrypt("valid-wb-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg-token"),
            telegram_chat_ids=["987654321"],
            notification_mode="scheduled",
            notification_schedule=["10:00", "14:00", "18:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)

        now_utc = datetime.now(timezone.utc)
        # 2 unnotified valid orders
        o1 = Order(
            id=order1_id,
            seller_id=seller_id,
            wb_created_at=now_utc,
            article="SKU-1",
            price=150000,
            status=OrderStatus.NEW,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=None,
        )
        o2 = Order(
            id=order2_id,
            seller_id=seller_id,
            wb_created_at=now_utc,
            article="SKU-2",
            price=250000,
            status=OrderStatus.ASSEMBLING,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=None,
        )
        # 1 cancelled order (must be skipped)
        o3 = Order(
            id=order_cancelled_id,
            seller_id=seller_id,
            wb_created_at=now_utc,
            article="SKU-CANCELLED",
            price=100000,
            status=OrderStatus.CANCELLED,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=None,
        )
        # 1 already notified order (must be skipped)
        o4 = Order(
            id=order_already_notified_id,
            seller_id=seller_id,
            wb_created_at=now_utc,
            article="SKU-NOTIFIED",
            price=300000,
            status=OrderStatus.NEW,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=now_utc,
        )
        session.add_all([o1, o2, o3, o4])
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_tg_batch:
        mock_tg_batch.return_value = {"sent": 1, "failed": 0}

        # 14:05 Moscow time = 11:05 UTC (Europe/Moscow is UTC+3)
        override_utc = datetime(2026, 9, 3, 11, 5, 0, tzinfo=timezone.utc)
        result = send_scheduled_orders_digest(now_utc_override=override_utc)

        assert result["sent_digests"] >= 1
        assert result["orders_notified"] == 2

        mock_tg_batch.assert_called_once()
        call_kwargs = mock_tg_batch.call_args[1]
        orders_payload = call_kwargs["orders"]
        assert len(orders_payload) == 2
        order_ids_in_payload = [p["id"] for p in orders_payload]
        assert order1_id in order_ids_in_payload
        assert order2_id in order_ids_in_payload
        assert order_cancelled_id not in order_ids_in_payload
        assert order_already_notified_id not in order_ids_in_payload

    # Verify orders now have notified_at timestamp in database
    with SyncSessionLocal() as session:
        db_o1 = session.query(Order).filter(Order.id == order1_id).first()
        db_o2 = session.query(Order).filter(Order.id == order2_id).first()
        assert db_o1.notified_at is not None
        assert db_o2.notified_at is not None


def test_send_scheduled_orders_digest_idempotency():
    import random
    seller_id = f"sched-idemp-{uuid.uuid4().hex[:8]}"
    order_id = random.randint(20000000, 25000000)

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Idempotency Test Shop",
            wb_api_token_encrypted=encrypt("valid-wb-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg-token"),
            telegram_chat_ids=["11223344"],
            notification_mode="scheduled",
            notification_schedule=["10:00", "14:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        o = Order(
            id=order_id,
            seller_id=seller_id,
            wb_created_at=datetime.now(timezone.utc),
            article="ITEM",
            price=1000,
            status=OrderStatus.NEW,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=None,
        )
        session.add(o)
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_tg_batch:
        mock_tg_batch.return_value = {"sent": 1, "failed": 0}

        # 1st run: 14:02 MSK (11:02 UTC) -> triggers send
        t1 = datetime(2026, 9, 3, 11, 2, 0, tzinfo=timezone.utc)
        res1 = send_scheduled_orders_digest(now_utc_override=t1)
        assert res1["orders_notified"] == 1
        assert mock_tg_batch.call_count == 1

        # 2nd run: 14:08 MSK (11:08 UTC) within same slot window -> skips because already sent today for slot 14:00
        t2 = datetime(2026, 9, 3, 11, 8, 0, tzinfo=timezone.utc)
        res2 = send_scheduled_orders_digest(now_utc_override=t2)
        assert res2["orders_notified"] == 0
        assert mock_tg_batch.call_count == 1  # No second call!


def test_send_scheduled_orders_digest_skips_when_slot_not_due():
    seller_id = f"sched-skip-{uuid.uuid4().hex[:8]}"

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = Seller(
            id=seller_id,
            name="Skip Test Shop",
            wb_api_token_encrypted=encrypt("valid-wb-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg-token"),
            telegram_chat_ids=["55667788"],
            notification_mode="scheduled",
            notification_schedule=["10:00", "14:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_tg_batch:
        # 12:30 MSK (09:30 UTC) - neither 10:00 nor 14:00
        t_off = datetime(2026, 9, 3, 9, 30, 0, tzinfo=timezone.utc)
        res = send_scheduled_orders_digest(now_utc_override=t_off)
        assert res["orders_notified"] == 0
        mock_tg_batch.assert_not_called()


def test_send_scheduled_orders_digest_timezone_conversion():
    import random
    seller_id = f"sched-tz-{uuid.uuid4().hex[:8]}"
    order_id = random.randint(30000000, 35000000)

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        # Asia/Vladivostok is UTC+10
        seller = Seller(
            id=seller_id,
            name="Vladivostok Shop",
            wb_api_token_encrypted=encrypt("valid-wb-token"),
            telegram_bot_token_encrypted=encrypt("valid-tg-token"),
            telegram_chat_ids=["999888111"],
            notification_mode="scheduled",
            notification_schedule=["10:00"],
            timezone="Asia/Vladivostok",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        o = Order(
            id=order_id,
            seller_id=seller_id,
            wb_created_at=datetime.now(timezone.utc),
            article="VLAD-ITEM",
            price=1000,
            status=OrderStatus.NEW,
            kiz_status=KizStatus.NOT_REQUIRED,
            notified_at=None,
        )
        session.add(o)
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_tg_batch:
        mock_tg_batch.return_value = {"sent": 1, "failed": 0}

        # 00:03 UTC -> 10:03 Vladivostok time (UTC+10) -> matches slot 10:00!
        t_vlad_utc = datetime(2026, 9, 3, 0, 3, 0, tzinfo=timezone.utc)
        res = send_scheduled_orders_digest(now_utc_override=t_vlad_utc)
        assert res["orders_notified"] == 1
        mock_tg_batch.assert_called_once()
