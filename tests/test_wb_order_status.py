import pytest
import uuid
import random
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.services.wb_client import WBClient
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.database import AsyncSessionLocal, init_db
from app.services.encryption import encrypt
from app.api.orders import refresh_orders


@pytest.mark.asyncio
async def test_wb_client_get_orders_status_endpoint():
    """Verify WBClient.get_orders_status makes correct POST request to /api/v3/orders/status."""
    client = WBClient(api_token="mock_wb_token")
    mock_order_ids = [5482820626, 5462395042]
    mock_response = {
        "orders": [
            {
                "id": 5482820626,
                "supplierStatus": "complete",
                "wbStatus": "sorted",
                "isCancellable": False
            },
            {
                "id": 5462395042,
                "supplierStatus": "complete",
                "wbStatus": "ready_for_pickup",
                "isCancellable": False
            }
        ]
    }

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_response
        result = await client.get_orders_status(mock_order_ids)

        mock_req.assert_called_once_with("POST", "/api/v3/orders/status", json={"orders": mock_order_ids})
        assert len(result) == 2
        assert result[0]["wbStatus"] == "sorted"
        assert result[1]["wbStatus"] == "ready_for_pickup"


@pytest.mark.asyncio
async def test_refresh_orders_syncs_wb_status_and_supplier_status():
    """Verify that refresh_orders fetches WB statuses and updates Order.wb_status / supplier_status."""
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller WB Status",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700991122",
            wb_api_token_encrypted=encrypt("wb_valid_token"),
            cz_token_encrypted=encrypt("cz_token"),
            is_active=True
        )
        session.add(seller)
        await session.commit()

        order_id = random.randint(5400000000, 5499999999)
        mock_raw_order = {
            "id": order_id,
            "rid": f"rid-{order_id}",
            "createdAt": "2026-08-18T10:00:00Z",
            "article": "hood.brown.100",
            "price": 247500,
            "deliveryType": "fbs",
        }

        mock_status_response = [
            {
                "id": order_id,
                "supplierStatus": "complete",
                "wbStatus": "sorted",
                "isCancellable": False
            }
        ]

        with patch("app.services.wb_client.WBClient.get_new_orders", new_callable=AsyncMock) as mock_new, \
             patch("app.services.wb_client.WBClient.get_orders", new_callable=AsyncMock) as mock_orders, \
             patch("app.services.wb_client.WBClient.get_orders_meta", new_callable=AsyncMock) as mock_meta, \
             patch("app.services.wb_client.WBClient.get_supplies", new_callable=AsyncMock) as mock_sup, \
             patch("app.services.wb_client.WBClient.get_cards_catalog", new_callable=AsyncMock) as mock_cat, \
             patch("app.services.wb_client.WBClient.get_orders_status", new_callable=AsyncMock) as mock_st:

            mock_new.return_value = []
            mock_orders.return_value = [mock_raw_order]
            mock_meta.return_value = {"orders": []}
            mock_sup.return_value = {"supplies": []}
            mock_cat.return_value = {"by_vendor_code": {}, "by_nm_id": {}, "by_chrt_id": {}}
            mock_st.return_value = mock_status_response

            res = await refresh_orders(seller_id=seller_id, db=session)
            assert res["new_count"] >= 1 or res["updated_count"] >= 1

            saved_order = await session.get(Order, order_id)
            assert saved_order is not None
            assert saved_order.wb_status == "sorted"
            assert saved_order.supplier_status == "complete"
            assert saved_order.status == OrderStatus.DELIVERING


@pytest.mark.asyncio
async def test_sync_all_orders_cz_status_not_found():
    """Verify 404 if seller does not exist."""
    from app.api.orders import sync_all_orders_cz_status
    from fastapi import HTTPException
    await init_db()
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await sync_all_orders_cz_status(seller_id=str(uuid.uuid4()), db=session)
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_sync_all_orders_cz_status_missing_inn():
    """Verify 400 if seller has no cz_inn."""
    from app.api.orders import sync_all_orders_cz_status
    from fastapi import HTTPException
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Seller No INN",
            cz_inn=None,
            wb_api_token_encrypted=encrypt("token"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await sync_all_orders_cz_status(seller_id=seller_id, db=session)
        assert exc_info.value.status_code == 400
        assert "ИНН Честного Знака" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sync_all_orders_cz_status_no_kiz_codes():
    """Verify friendly response when seller has orders but no KIZ codes."""
    from app.api.orders import sync_all_orders_cz_status
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Seller No KIZ",
            cz_inn="7712345678",
            wb_api_token_encrypted=encrypt("token"),
            cz_token_encrypted=encrypt("cz_token"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        res = await sync_all_orders_cz_status(seller_id=seller_id, db=session)
        assert res["success"] is True
        assert res["total_checked"] == 0
        assert "нет прикрепленных кодов КИЗ" in res["message"]


@pytest.mark.asyncio
async def test_sync_all_orders_cz_status_success_flow():
    """Verify bulk sync queries True API, updates KizProductInfo and Order records."""
    from app.api.orders import sync_all_orders_cz_status
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Seller Bulk KIZ Sync",
            cz_inn="7700112233",
            wb_api_token_encrypted=encrypt("token"),
            cz_token_encrypted=encrypt("cz_token"),
            is_active=True,
        )
        session.add(seller)

        kiz_1 = f"0104630199251001215A{random.randint(1000, 9999)}"
        kiz_2 = f"0104630199251002215B{random.randint(1000, 9999)}"

        order_1 = Order(
            id=random.randint(1000000, 9999999),
            seller_id=seller_id,
            status=OrderStatus.ASSEMBLING,
            kiz_code=kiz_1,
            kiz_status=KizStatus.ATTACHED,
            kiz_required=True,
            price=1500.0,
            wb_created_at=datetime.now(timezone.utc),
        )
        order_2 = Order(
            id=random.randint(1000000, 9999999),
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            kiz_code=kiz_2,
            kiz_status=KizStatus.ATTACHED,
            kiz_required=True,
            price=2200.0,
            wb_created_at=datetime.now(timezone.utc),
        )
        session.add_all([order_1, order_2])
        await session.commit()

        mock_cises_info = [
            {
                "cisInfo": {
                    "requestedCis": kiz_1,
                    "status": "INTRODUCED",
                    "statusEx": "IN_CIRCULATION",
                    "productName": "Футболка белая M",
                }
            },
            {
                "cisInfo": {
                    "requestedCis": kiz_2,
                    "status": "RETIRED",
                    "statusEx": "RETIRED_SALE",
                    "productName": "Худи оверсайз",
                }
            },
        ]

        with patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_get_info:
            mock_get_info.return_value = mock_cises_info

            res = await sync_all_orders_cz_status(seller_id=seller_id, db=session)

            assert res["success"] is True
            assert res["total_checked"] == 2
            assert res["summary"]["in_circulation"] == 1
            assert res["summary"]["withdrawn"] == 1
            assert "В обороте: 1, Выбыли: 1" in res["message"]

        await session.refresh(order_1)
        await session.refresh(order_2)

        assert order_1.kiz_cz_status == "INTRODUCED"
        assert order_1.kiz_status == KizStatus.VALIDATED

        assert order_2.kiz_cz_status == "RETIRED"
        assert order_2.kiz_status == KizStatus.WITHDRAWN

