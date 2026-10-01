"""
Tests for WB Archive API Integration:
- WBClient.get_archive_orders and get_all_archive_orders pagination
- wb_archive_service: get_recent_months calculation, order upsert, KIZ linking
- FastAPI endpoint /sellers/{seller_id}/archive/sync-wb-api
- Unified KIZ batch reconciliation with synced archive orders
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.kiz import KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.auth_service import ensure_initial_admin
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch
from app.services.wb_archive_service import (
    get_recent_months,
    resolve_order_status,
    sync_seller_archive_orders_recent_months,
)
from app.services.wb_client import WBClient


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


def test_get_recent_months_calculation():
    """Проверяет корректность расчета месяцев, включая переход через границу года."""
    # Обычный случай: октябрь 2026
    dt1 = datetime(2026, 10, 15, tzinfo=timezone.utc)
    res1 = get_recent_months(months_count=3, base_date=dt1)
    assert res1 == [(2026, 10), (2026, 9), (2026, 8)]

    # Граница года: январь 2026 -> 2026-01, 2025-12, 2025-11
    dt2 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    res2 = get_recent_months(months_count=3, base_date=dt2)
    assert res2 == [(2026, 1), (2025, 12), (2025, 11)]

    # 1 месяц
    res3 = get_recent_months(months_count=1, base_date=dt1)
    assert res3 == [(2026, 10)]


def test_resolve_order_status():
    """Проверяет маппинг статусов архива WB на внутренние статусы заказа."""
    assert resolve_order_status("canceled_by_client", "complete") == OrderStatus.CANCELLED
    assert resolve_order_status("declined_by_client", "complete") == OrderStatus.CANCELLED
    assert resolve_order_status("defect", "complete") == OrderStatus.CANCELLED
    assert resolve_order_status("canceled", "complete") == OrderStatus.CANCELLED
    assert resolve_order_status("delivered", "cancel") == OrderStatus.CANCELLED
    assert resolve_order_status("sold", "complete") == OrderStatus.DELIVERED
    assert resolve_order_status("delivered", "complete") == OrderStatus.DELIVERED


@pytest.mark.asyncio
async def test_wb_client_get_archive_orders_call():
    """Проверяет, что WBClient.get_archive_orders корректно формирует запрос."""
    client = WBClient(api_token="test_token")
    mock_response = {
        "orders": [{"id": 123456, "status": {"wbStatus": "canceled_by_client"}}],
        "next": 0,
    }

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_response
        res = await client.get_archive_orders(year=2026, month=9, next_cursor=0, limit=500)
        
        mock_req.assert_called_once_with(
            "GET",
            "/api/marketplace/v3/fbs/orders/archive",
            params={"year": 2026, "month": 9, "next": 0, "limit": 500},
        )
        assert len(res["orders"]) == 1


@pytest.mark.asyncio
async def test_wb_client_get_all_archive_orders_pagination():
    """Проверяет работу пагинатора по курсору next."""
    client = WBClient(api_token="test_token")

    page1 = {"orders": [{"id": 101}], "next": 50}
    page2 = {"orders": [{"id": 102}], "next": 0}

    with patch.object(client, "get_archive_orders", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [page1, page2]
        all_orders = await client.get_all_archive_orders(year=2026, month=8)

        assert len(all_orders) == 2
        assert all_orders[0]["id"] == 101
        assert all_orders[1]["id"] == 102
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_sync_seller_archive_orders_creates_and_updates():
    """
    Проверяет, что синхронизация архива:
    1. Создает новый заказ со статусом CANCELLED и привязывает КИЗ.
    2. Обновляет существующий заказ со статуса DELIVERING в CANCELLED.
    3. Регистрирует КИЗ в KizProductInfo.
    """
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Archive Test Seller",
            cz_inn="190207495060",
            wb_api_token_encrypted="encrypted_wb_token",
        )
        db.add(seller)

        existing_order_id = int(str(uuid.uuid4().int)[:9])
        existing_order = Order(
            id=existing_order_id,
            seller_id=seller_id,
            wb_created_at=datetime.now(timezone.utc),
            status=OrderStatus.DELIVERING,
            wb_status="sorted",
            kiz_status=KizStatus.ATTACHED,
            kiz_code="0104630199252612215*EXISTING_01",
        )
        db.add(existing_order)
        await db.commit()

        new_order_id = int(str(uuid.uuid4().int)[:9])
        kiz_for_new_order = "0104630199252612215*NEW_KIZ_02"

        mock_archive_data = [
            # 1. Отмененный заказ, которого нет в БД
            {
                "id": new_order_id,
                "createdAt": "2026-09-10T12:00:00Z",
                "priceInfo": {"price": 180000},
                "product": {"article": "ART-01", "name": "Товар 1"},
                "status": {"supplierStatus": "complete", "wbStatus": "canceled_by_client"},
                "metaDetails": {"sgtin": kiz_for_new_order, "gtin": "04630199252612"},
            },
            # 2. Существующий заказ, который был отменен
            {
                "id": existing_order_id,
                "createdAt": "2026-09-08T10:00:00Z",
                "priceInfo": {"price": 250000},
                "product": {"article": "ART-02", "name": "Товар 2"},
                "status": {"supplierStatus": "complete", "wbStatus": "declined_by_client"},
                "metaDetails": {"sgtin": "0104630199252612215*EXISTING_01"},
            },
        ]

        with patch("app.services.wb_archive_service.decrypt", return_value="raw_token"), \
             patch("app.services.wb_client.WBClient.get_all_archive_orders", new_callable=AsyncMock) as mock_all:
            mock_all.return_value = mock_archive_data

            summary = await sync_seller_archive_orders_recent_months(
                seller=seller,
                db=db,
                months_count=1,
            )

            assert summary["total_fetched"] == 2
            assert summary["orders_created"] == 1
            assert summary["orders_updated"] == 1
            assert summary["cancelled_orders"] == 2

            # Проверяем заказ 1 в БД
            db_new = await db.get(Order, new_order_id)
            assert db_new is not None
            assert db_new.status == OrderStatus.CANCELLED
            assert db_new.wb_status == "canceled_by_client"
            assert db_new.kiz_code == kiz_for_new_order
            assert db_new.price == Decimal("1800.00")

            # Проверяем существующий заказ 2 в БД
            db_exist = await db.get(Order, existing_order_id)
            assert db_exist is not None
            assert db_exist.status == OrderStatus.CANCELLED
            assert db_exist.wb_status == "declined_by_client"

            # Проверяем регистрацию KizProductInfo
            kinfo_new = (await db.execute(
                KizProductInfo.__table__.select().where(KizProductInfo.kiz_code == kiz_for_new_order)
            )).first()
            assert kinfo_new is not None
            assert kinfo_new.order_id == new_order_id


async def _get_auth_headers(client: AsyncClient) -> dict:
    from app.config import settings
    res = await client.post(
        "/api/v1/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert res.status_code == 200, f"Login failed: {res.text}"
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_fastapi_sync_wb_archive_endpoint():
    """Проверяет эндпоинт POST /api/v1/sellers/{seller_id}/archive/sync-wb-api."""
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Router Test Seller",
            cz_inn="190207495060",
            wb_api_token_encrypted="encrypted_token",
        )
        db.add(seller)
        await db.commit()

    mock_summary = {
        "seller_id": seller_id,
        "total_fetched": 15,
        "orders_created": 3,
        "orders_updated": 12,
        "cancelled_orders": 5,
        "delivered_orders": 10,
        "kiz_linked": 4,
    }

    with patch("app.services.wb_archive_service.sync_seller_archive_orders_recent_months", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = mock_summary

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _get_auth_headers(client)
            resp = await client.post(
                f"/api/v1/sellers/{seller_id}/archive/sync-wb-api",
                json={"months_count": 3},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["summary"]["total_fetched"] == 15
            assert data["summary"]["cancelled_orders"] == 5


@pytest.mark.asyncio
async def test_unified_batch_includes_synced_archive_orders():
    """
    Проверяет, что синхронизированный из архива отмененный заказ
    автоматически попадает в возвратный пакет 'Единой кнопки'.
    """
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Unified Archive Sync Seller",
            cz_inn="190207495060",
            wb_api_token_encrypted="encrypted_token",
        )
        db.add(seller)

        # Отмененный заказ, появившийся из архива WB
        ord_id = int(str(uuid.uuid4().int)[:9])
        unique_kiz = f"0104630199252612215*{uuid.uuid4().hex[:10]}"
        cancelled_order = Order(
            id=ord_id,
            seller_id=seller_id,
            wb_created_at=datetime.now(timezone.utc),
            status=OrderStatus.CANCELLED,
            wb_status="canceled_by_client",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_code=unique_kiz,
        )
        db.add(cancelled_order)

        # Карточка КИЗ со статусом RETIRED в ЧЗ (выбыл) и ИНН продавца
        kinfo = KizProductInfo(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            order_id=ord_id,
            kiz_code=unique_kiz,
            gtin="04630199252612",
            clean_cis=unique_kiz,
            cz_status="RETIRED",
            cz_owner_inn="190207495060",
        )
        db.add(kinfo)
        await db.commit()

        # Мокаем проверку в True API
        with patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = {unique_kiz: kinfo}

            res = await create_unified_kiz_signature_batch(
                seller=seller,
                db=db,
                days=90,
                sync_finance_api=False,
                sync_archive_api=False,
            )

            assert res["success"] is True
            batch = await db.get(KizSignatureBatch, res["batch_id"])
            assert batch is not None
            assert batch.returns_count >= 1
            returns_list = batch.data_payload["returns"]
            matching = [r for r in returns_list if r.get("order_id") == ord_id]
            assert len(matching) == 1
            assert matching[0]["kiz_code"] == unique_kiz
