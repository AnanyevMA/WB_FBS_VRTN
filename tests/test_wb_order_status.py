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
