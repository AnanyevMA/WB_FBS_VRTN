"""
Unit and Integration Tests for WB Warehouse Sales & FBO KIZ Processing.
Verifies WBAnalyticsClient, wb_warehouse_sales_service, Celery tasks, and API endpoints.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from httpx import Response

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin
from app.services.wb_analytics_client import (
    WBAnalyticsClient,
    WBAnalyticsRateLimitError,
    WBAnalyticsUnauthorizedError,
)
from app.services.wb_warehouse_sales_service import (
    fetch_wb_excise_data,
    process_warehouse_sales_for_seller,
)


@pytest.mark.asyncio
async def test_wb_analytics_client_success():
    client = WBAnalyticsClient(api_token="test_token")
    mock_resp = Response(
        status_code=200,
        json={
            "response": {
                "data": [
                    {
                        "barcode": "2044967817804",
                        "excise_short": "0104630199251332215+gIUHbTKIpgQ",
                        "fiscal_doc_number": 145211,
                        "fiscal_drive_number": "7380440903830012",
                        "fiscal_dt": "2026-08-28",
                        "nm_id": 473442280,
                        "price": 3194,
                        "srid": "eBF.i058f3279c2caa654bb7ede320b14fb0c.0.0",
                    }
                ]
            }
        },
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        data = await client.get_excise_report("2026-08-01", "2026-08-28")
        assert len(data) == 1
        assert data[0]["excise_short"] == "0104630199251332215+gIUHbTKIpgQ"
        assert data[0]["fiscal_doc_number"] == 145211


@pytest.mark.asyncio
async def test_wb_analytics_client_unauthorized():
    client = WBAnalyticsClient(api_token="invalid_token")
    mock_resp = Response(status_code=401, text="Unauthorized")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(WBAnalyticsUnauthorizedError):
            await client.get_excise_report("2026-08-01", "2026-08-28")


@pytest.mark.asyncio
async def test_warehouse_sales_no_token():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="No WB Token Seller",
            wb_api_token_encrypted="",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        result = await process_warehouse_sales_for_seller(seller, session, days=14)
        assert result["created"] is False
        assert result["needs_withdrawal_count"] == 0


@pytest.mark.asyncio
async def test_warehouse_sales_already_retired():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Retired Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Товар FBO",
            status=OrderStatus.CANCELLED,
            kiz_code="0104630199251332215+gIUHbTKIpgQ",
            kiz_status=KizStatus.RETURNED,
            wb_created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()

        mock_excise_rows = [
            {
                "barcode": "2044967817804",
                "excise_short": "0104630199251332215+gIUHbTKIpgQ",
                "fiscal_doc_number": 145211,
                "fiscal_drive_number": "7380440903830012",
                "fiscal_dt": "2026-08-28",
                "nm_id": 473442280,
                "price": 3194,
                "srid": "srid-123",
            }
        ]

        # Mock True API returning RETIRED
        mock_kinfo = MagicMock()
        mock_kinfo.cz_status = "RETIRED"
        mock_kinfo.cz_status_ex = None
        mock_kinfo.raw_cz_payload = {}

        with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
             patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
            mock_fetch.return_value = mock_excise_rows
            mock_verify.return_value = {"0104630199251332215+gIUHbTKIpgQ": mock_kinfo}

            res = await process_warehouse_sales_for_seller(seller, session, days=14)
            assert res["created"] is False
            assert res["already_retired_count"] == 1

            # Verify order status was updated
            updated_order = await session.get(Order, order_id)
            assert updated_order.kiz_status == KizStatus.WITHDRAWN


@pytest.mark.asyncio
async def test_warehouse_sales_creates_signature_batch():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Batch Create Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        mock_excise_rows = [
            {
                "barcode": "2044967817804",
                "excise_short": "0104630199251332215+gIUHbTKIpgQ",
                "fiscal_doc_number": 145211,
                "fiscal_drive_number": "7380440903830012",
                "fiscal_dt": "2026-08-28",
                "nm_id": 473442280,
                "price": 3194,
                "srid": "srid-fbo-999",
            }
        ]

        # Mock True API returning INTRODUCED on seller's balance
        mock_kinfo = MagicMock()
        mock_kinfo.cz_status = "INTRODUCED"
        mock_kinfo.cz_status_ex = None
        mock_kinfo.raw_cz_payload = {"ownerInn": "7700112233"}

        with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
             patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify, \
             patch("app.services.wb_warehouse_sales_service._send_telegram_notification", new_callable=AsyncMock) as mock_tg:
            mock_fetch.return_value = mock_excise_rows
            mock_verify.return_value = {"0104630199251332215+gIUHbTKIpgQ": mock_kinfo}

            res = await process_warehouse_sales_for_seller(seller, session, days=14)
            assert res["created"] is True
            assert res["sales_count"] == 1
            assert res["status"] == "PENDING_SIGNATURE"

            # Check KizSignatureBatch record in DB
            batch = await session.get(KizSignatureBatch, res["batch_id"])
            assert batch is not None
            assert batch.source == "wb_warehouse_sale"
            assert batch.sales_count == 1
            payload = batch.data_payload
            assert len(payload["withdrawals"]) == 1
            w = payload["withdrawals"][0]
            assert w["kiz_code"] == "0104630199251332215+gIUHbTKIpgQ"
            assert w["receipt_number"] == "145211"
            assert w["fn_number"] == "7380440903830012"
            assert w["receipt_date"] == "2026-08-28"
            assert w["price"] == 3194.0
            assert w["price_kopecks"] == 319400
            assert w["task_status"] == "Продажа со склада WB (FBO)"


@pytest.mark.asyncio
async def test_warehouse_sales_deduplication():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Dedup Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)

        # Existing pending batch with the same KIZ
        existing_batch = KizSignatureBatch(
            seller_id=seller_id,
            filename="wb_warehouse_old.json",
            source="wb_warehouse_sale",
            status=BatchStatus.PENDING_SIGNATURE,
            sales_count=1,
            returns_count=0,
            already_withdrawn_count=0,
            total_count=1,
            data_payload={
                "withdrawals": [{"kiz_code": "0104630199251332215+gIUHbTKIpgQ"}]
            },
        )
        session.add(existing_batch)
        await session.commit()

        mock_excise_rows = [
            {
                "barcode": "2044967817804",
                "excise_short": "0104630199251332215+gIUHbTKIpgQ",
                "fiscal_doc_number": 145211,
                "price": 3194,
            }
        ]
        mock_kinfo = MagicMock()
        mock_kinfo.cz_status = "INTRODUCED"
        mock_kinfo.cz_status_ex = None
        mock_kinfo.raw_cz_payload = {"ownerInn": "7700112233"}

        with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
             patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
            mock_fetch.return_value = mock_excise_rows
            mock_verify.return_value = {"0104630199251332215+gIUHbTKIpgQ": mock_kinfo}

            res = await process_warehouse_sales_for_seller(seller, session, days=14)
            # Already queued -> nothing new to create
            assert res["created"] is False
            assert res["needs_withdrawal_count"] == 0


@pytest.mark.asyncio
async def test_warehouse_sales_skips_wb_owned_kiz():
    """Verify that KIZ codes on WB balance (ownerInn=9714053621) are skipped."""
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="WB Owner Test Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        mock_excise_rows = [
            {
                "barcode": "2044967817804",
                "excise_short": "0104630199251332215+gIUHbTKIpgQ",
                "fiscal_doc_number": 145211,
                "price": 3194,
            }
        ]
        mock_kinfo = MagicMock()
        mock_kinfo.cz_status = "INTRODUCED"
        mock_kinfo.cz_status_ex = None
        mock_kinfo.raw_cz_payload = {
            "cisInfo": {
                "ownerInn": "9714053621",  # ООО "РВБ" (Wildberries)
                "ownerName": "ООО РВБ",
            }
        }

        with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
             patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
            mock_fetch.return_value = mock_excise_rows
            mock_verify.return_value = {"0104630199251332215+gIUHbTKIpgQ": mock_kinfo}

            res = await process_warehouse_sales_for_seller(seller, session, days=14)
            assert res["created"] is False
            assert res["needs_withdrawal_count"] == 0
            assert res["other_owner_count"] == 1
            assert "ООО «РВБ»" in res["message"]


@pytest.mark.asyncio
async def test_warehouse_sales_celery_tasks():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Celery Task Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        from app.agents.wb_warehouse_sales_agent import (
            sync_all_sellers_warehouse_sales,
            sync_seller_warehouse_sales,
        )

        with patch("app.agents.wb_warehouse_sales_agent.sync_seller_warehouse_sales.delay") as mock_delay:
            res_all = sync_all_sellers_warehouse_sales()
            assert "dispatched" in res_all
            assert res_all["dispatched"] >= 1

        with patch("app.agents.wb_warehouse_sales_agent.process_warehouse_sales_for_seller", new_callable=AsyncMock) as mock_process:
            mock_process.return_value = {"success": True, "created": False}
            res_single = sync_seller_warehouse_sales(seller_id=seller_id, days=14)
            assert res_single["success"] is True


@pytest.mark.asyncio
async def test_warehouse_sales_api_endpoints():
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    await init_db()
    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )

        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="API Test Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        headers = {"Authorization": f"Bearer {auth_token}"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch("app.api.kiz.warehouse_sales.process_warehouse_sales_for_seller", new_callable=AsyncMock) as mock_proc:
                mock_proc.return_value = {
                    "success": True,
                    "created": True,
                    "sales_count": 5,
                    "already_retired_count": 2,
                }
                resp = await ac.post(
                    f"/api/v1/sellers/{seller_id}/warehouse-sales/sync?days=14",
                    headers=headers,
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["sales_count"] == 5

            resp_history = await ac.get(
                f"/api/v1/sellers/{seller_id}/warehouse-sales/batches",
                headers=headers,
            )
            assert resp_history.status_code == 200
            assert "batches" in resp_history.json()

