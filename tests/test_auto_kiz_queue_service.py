"""
Tests for Auto KIZ Queue Service (app/services/auto_kiz_queue_service.py)
"""
import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.services.encryption import encrypt
from app.services.telegram_service import TelegramService
from app.services.auto_kiz_queue_service import (
    sync_delivered_orders_with_wb,
    collect_auto_kiz_candidates,
    process_auto_kiz_queue_for_seller,
)


@pytest.mark.asyncio
async def test_sync_wb_orders_status():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Sync WB Test Seller",
            wb_api_token_encrypted=encrypt("test_wb_token"),
            is_active=True,
        )
        session.add(seller)

        order1_id = int(str(uuid.uuid4().int)[:9])
        order1 = Order(
            id=order1_id,
            seller_id=seller_id,
            name="Товар 1",
            article="art-01",
            price=1500,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215QTSRH>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order1)
        await session.commit()

        # Mock WBClient.get_orders_status: returns delivered status
        mock_statuses = [
            {"id": order1_id, "supplierStatus": "complete", "wbStatus": "sold"}
        ]
        with patch("app.services.auto_kiz_queue_service.WBClient") as mock_wb_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.__aexit__.return_value = None
            mock_client_instance.get_orders_status.return_value = mock_statuses
            mock_wb_client_cls.return_value = mock_client_instance

            updated_count = await sync_delivered_orders_with_wb(seller, session)
            assert updated_count >= 1

        await session.refresh(order1)
        assert order1.status == OrderStatus.DELIVERED
        assert order1.wb_status == "sold"


@pytest.mark.asyncio
async def test_collect_candidates_and_deduplication():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Candidate Test Seller",
            wb_api_token_encrypted=encrypt("test_wb_token"),
            cz_token_encrypted=encrypt("test_cz_token"),
            cz_inn="1234567890",
            is_active=True,
        )
        session.add(seller)

        # Order 1: Delivered, attached KIZ -> Candidate for withdrawal
        ord1_id = int(str(uuid.uuid4().int)[:9])
        ord1 = Order(
            id=ord1_id,
            seller_id=seller_id,
            name="Платье",
            article="dress-01",
            price=3000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code="0104630199251318215AAAAA>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        # Order 2: Cancelled, withdrawn KIZ -> Candidate for return
        ord2_id = int(str(uuid.uuid4().int)[:9])
        ord2 = Order(
            id=ord2_id,
            seller_id=seller_id,
            name="Куртка",
            article="jacket-02",
            price=7000,
            status=OrderStatus.CANCELLED,
            kiz_required=True,
            kiz_code="0104630199251318215BBBBB>4sVc+.",
            kiz_status=KizStatus.WITHDRAWN,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        # Order 3: Delivered, but already in PENDING batch -> Should be deduplicated
        ord3_id = int(str(uuid.uuid4().int)[:9])
        ord3 = Order(
            id=ord3_id,
            seller_id=seller_id,
            name="Юбка",
            article="skirt-03",
            price=2000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code="0104630199251318215CCCCC>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )

        session.add_all([ord1, ord2, ord3])
        await session.commit()

        # Add pending batch containing ord3
        pending_batch = KizSignatureBatch(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            filename="test_batch.xlsx",
            source="manual",
            status=BatchStatus.PENDING_SIGNATURE,
            sales_count=1,
            returns_count=0,
            already_withdrawn_count=0,
            total_count=1,
            data_payload={
                "withdrawals": [{"order_id": ord3_id, "kiz_code": ord3.kiz_code}]
            },
        )
        session.add(pending_batch)
        await session.commit()

        withdrawals, returns = await collect_auto_kiz_candidates(seller, session)

        # ord1 should be in withdrawals
        w_order_ids = [w.id for w in withdrawals]
        assert ord1_id in w_order_ids
        # ord3 should NOT be in withdrawals because it's already in pending_batch
        assert ord3_id not in w_order_ids

        # ord2 should be in returns
        r_order_ids = [r.id for r in returns]
        assert ord2_id in r_order_ids


@pytest.mark.asyncio
async def test_process_auto_kiz_queue_flow():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        manager_chat_id = "987654321"
        seller = Seller(
            id=seller_id,
            name="Queue Flow Seller",
            wb_api_token_encrypted=encrypt("test_wb_token"),
            cz_token_encrypted=encrypt("test_cz_token"),
            cz_inn="7701234567",
            auto_kiz_queue_enabled=True,
            auto_kiz_auto_sign_server=False,
            auto_kiz_manager_chat_id=manager_chat_id,
            telegram_chat_ids=["111222333"],  # General chat should NOT receive personal manager report
            telegram_bot_token_encrypted=encrypt("test_tg_token"),
            is_active=True,
        )
        session.add(seller)

        ord_id = int(str(uuid.uuid4().int)[:9])
        ord_candidate = Order(
            id=ord_id,
            seller_id=seller_id,
            name="Свитер",
            article="sweater-01",
            price=4500,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code="0104630199251318215DDDDD>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(ord_candidate)
        await session.commit()

        # Mock KizProductInfo in database or batch_verify_and_sync_cises
        async def fake_verify(*args, **kwargs):
            cises = kwargs.get("kiz_codes") or (args[1] if len(args) > 1 else [])
            res = {}
            for c in cises:
                info = KizProductInfo(
                    cis=c,
                    gtin="04630199251318",
                    sgtin="215DDDDD",
                    cz_status="INTRODUCED",
                    cz_status_ex="IN_CIRCULATION",
                    is_valid=True,
                    is_withdrawn=False,
                )
                res[c] = info
            return res

        with patch("app.services.auto_kiz_queue_service.sync_delivered_orders_with_wb", new_callable=AsyncMock) as mock_sync_wb, \
             patch("app.services.auto_kiz_queue_service.batch_verify_and_sync_cises", side_effect=fake_verify), \
             patch.object(TelegramService, "send_auto_kiz_batch_notification", new_callable=AsyncMock) as mock_tg:

            mock_sync_wb.return_value = 0
            mock_tg.return_value = True

            res = await process_auto_kiz_queue_for_seller(seller, session)

            assert res["created"] is True
            assert res["sales_count"] == 1
            assert res["returns_count"] == 0

            # Verify telegram notification sent specifically to manager_chat_id
            mock_tg.assert_called_once()
            call_kwargs = mock_tg.call_args.kwargs
            assert call_kwargs["chat_ids"] == [manager_chat_id]
            assert call_kwargs["seller_name"] == seller.name
            assert call_kwargs["sales_count"] == 1

        # Check seller's last_auto_kiz_queue_at updated
        await session.refresh(seller)
        assert seller.last_auto_kiz_queue_at is not None

        # Verify second run returns created=False because order is already in the pending batch
        res_dup = await process_auto_kiz_queue_for_seller(seller, session)
        assert res_dup["created"] is False
        assert res_dup["sales_count"] == 0
