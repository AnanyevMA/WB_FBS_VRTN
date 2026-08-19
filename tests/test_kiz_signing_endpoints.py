import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.services.encryption import encrypt


@pytest.mark.asyncio
async def test_prepare_and_submit_kiz_document_endpoints():
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Signing Test Seller",
            wb_api_token_encrypted=encrypt("wb_test_token"),
            cz_token_encrypted=encrypt("cz_test_token"),
            cz_inn="7700112233",
            mod_fias="test-fias-uuid",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(seller)
        await session.flush()

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller.id,
            name="Капор утепленный",
            article="hood.test.01",
            price=1500,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215QTSRH>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            kiz_cz_status="INTRODUCED",
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()


    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Test prepare withdrawal document
        res_prep_w = await ac.post(
            f"/api/v1/sellers/{seller_id}/kiz/prepare-document",
            json={"action": "WITHDRAWAL", "order_ids": [order_id]},
        )
        assert res_prep_w.status_code == 200, res_prep_w.text
        data_w = res_prep_w.json()
        assert data_w["success"] is True
        assert data_w["action"] == "WITHDRAWAL"
        assert data_w["document_type"] == "LK_RECEIPT"
        assert data_w["count"] == 1
        assert "document_base64" in data_w
        assert "document_json" in data_w
        assert "0104630199251318215QTSRH>4sVc+." in data_w["document_json"]

        # 2. Test prepare return document
        res_prep_r = await ac.post(
            f"/api/v1/sellers/{seller_id}/kiz/prepare-document",
            json={"action": "RETURN", "order_ids": [order_id]},
        )
        assert res_prep_r.status_code == 200, res_prep_r.text
        data_r = res_prep_r.json()
        assert data_r["success"] is True
        assert data_r["action"] == "RETURN"
        assert data_r["document_type"] == "LP_RETURN"
        assert "document_base64" in data_r

        # 3. Test submit signed document for withdrawal
        fake_sig = "MOCK_BASE64_CMS_SIGNATURE_DATA"
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:
            mock_submit.return_value = "doc-uuid-12345-withdrawal"

            res_submit = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
                json={
                    "document_type": data_w["document_type"],
                    "document_base64": data_w["document_base64"],
                    "signature_base64": fake_sig,
                    "order_ids": [order_id],
                    "action": "WITHDRAWAL",
                },
            )
            assert res_submit.status_code == 200, res_submit.text
            submit_data = res_submit.json()
            assert submit_data["success"] is True
            assert submit_data["doc_id"] == "doc-uuid-12345-withdrawal"

        async with AsyncSessionLocal() as session:
            o_updated = await session.get(Order, order_id)
            assert o_updated.kiz_status == KizStatus.WITHDRAWN
            assert o_updated.kiz_cz_status == "RETIRED"
            assert o_updated.cz_withdrawal_doc_id == "doc-uuid-12345-withdrawal"

        # 4. Test submit signed document for return
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:
            mock_submit.return_value = "doc-uuid-67890-return"

            res_submit_ret = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
                json={
                    "document_type": data_r["document_type"],
                    "document_base64": data_r["document_base64"],
                    "signature_base64": fake_sig,
                    "order_ids": [order_id],
                    "action": "RETURN",
                },
            )
            assert res_submit_ret.status_code == 200, res_submit_ret.text
            ret_data = res_submit_ret.json()
            assert ret_data["success"] is True
            assert ret_data["doc_id"] == "doc-uuid-67890-return"

        async with AsyncSessionLocal() as session:
            o_ret = await session.get(Order, order_id)
            assert o_ret.kiz_status == KizStatus.RETURNED
            assert o_ret.kiz_cz_status == "INTRODUCED"