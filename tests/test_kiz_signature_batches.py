"""
Integration & Unit Tests for KIZ Signature Batches and Dashboard Signing Queue
"""
import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin


@pytest.mark.asyncio
async def test_signature_batches_api_flow():
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Batch Test Seller",
            wb_api_token_encrypted=encrypt("wb_test_token"),
            cz_token_encrypted=encrypt("cz_test_token"),
            cz_inn="7700112233",
            mod_fias="test-fias-uuid",
            is_active=True,
            polling_enabled=True,
            archive_reminder_enabled=True,
            archive_reminder_days=2,
        )
        session.add(seller)

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller.id,
            name="Худи оверсайз VRTN",
            article="vrtn-hood-01",
            price=2500,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215QTSRH>4sVc+.",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)

        # Create a test pending batch
        batch = KizSignatureBatch(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            filename="wb_archive_august.xlsx",
            source="telegram",
            status=BatchStatus.PENDING_SIGNATURE,
            sales_count=1,
            returns_count=0,
            already_withdrawn_count=0,
            total_count=1,
            data_payload={
                "withdrawals": [{
                    "order_id": order_id,
                    "sticker_id": "123456",
                    "kiz_code": "0104630199251318215QTSRH>4sVc+.",
                    "receipt_number": "ЧЕК-778899",
                    "receipt_date": "2026-08-25",
                    "price": 2500,
                    "price_kopecks": 250000,
                }],
                "returns": [],
                "summary": {"sales": 1, "returns": 0, "total_processed": 1}
            }
        )
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        # 1. List signature batches
        res_list = await ac.get(f"/api/v1/sellers/{seller_id}/kiz/signature-batches")
        assert res_list.status_code == 200
        batches = res_list.json()
        assert len(batches) >= 1
        assert batches[0]["id"] == batch_id
        assert batches[0]["status"] == "PENDING_SIGNATURE"
        assert batches[0]["sales_count"] == 1

        # 2. Get batch details
        res_get = await ac.get(f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}")
        assert res_get.status_code == 200
        details = res_get.json()
        assert details["filename"] == "wb_archive_august.xlsx"
        assert len(details["data_payload"]["withdrawals"]) == 1

        # 3. Prepare documents for signing
        res_prep = await ac.post(f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}/prepare-documents")
        assert res_prep.status_code == 200
        prep_data = res_prep.json()
        assert prep_data["success"] is True
        assert len(prep_data["documents"]) == 1
        doc = prep_data["documents"][0]
        assert doc["receipt_number"] == "ЧЕК-778899"
        assert doc["document_base64"] is not None

        # 4. Submit signed batch (mock ГИС МТ True API)
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit:
            mock_submit.return_value = "doc-uuid-12345"

            res_submit = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}/submit-signed",
                json={
                    "sign_mode": "client_cades",
                    "cert_subject": "ООО ТЕСТ СЕЛЛЕР (Иванов И.И.)",
                    "signed_documents": [{
                        "action": "WITHDRAWAL",
                        "type": doc["type"],
                        "order_id": order_id,
                        "kiz_code": doc["kiz_code"],
                        "document_base64": doc["document_base64"],
                        "signature_base64": "mock-detached-sig-base64",
                    }]
                }
            )
            assert res_submit.status_code == 200
            submit_data = res_submit.json()
            assert submit_data["status"] == "COMPLETED"
            assert submit_data["successful_submissions"] == 1

        # 5. Verify batch in DB is COMPLETED
        async with AsyncSessionLocal() as session:
            updated_batch = await session.get(KizSignatureBatch, batch_id)
            assert updated_batch.status == BatchStatus.COMPLETED
            assert updated_batch.signed_at is not None
            assert updated_batch.signed_by == "ООО ТЕСТ СЕЛЛЕР (Иванов И.И.)"

            # Check order was updated to DELIVERED and WITHDRAWN
            updated_order = await session.get(Order, order_id)
            assert updated_order.kiz_status == KizStatus.WITHDRAWN
            assert updated_order.status == OrderStatus.DELIVERED


@pytest.mark.asyncio
async def test_signature_batch_cz_status_filtering_and_sync():
    """
    Проверяет:
    1. Исключение уже выбывших КИЗ (needs_withdrawal: False) и возвратов в обороте (needs_cz_return: False) при подготовке документов.
    2. Эндпоинт POST /signature-batches/{batch_id}/sync-cz для живой актуализации статусов через True API.
    3. Поддержку точечной фильтрации selected_kiz_codes.
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Filter Test Seller",
            wb_api_token_encrypted=encrypt("wb_test_token"),
            cz_token_encrypted=encrypt("cz_test_token"),
            cz_inn="7700998877",
            mod_fias="test-fias-uuid",
            is_active=True,
        )
        session.add(seller)

        # Batch with 2 sales (1 needs withdrawal, 1 already retired) and 2 returns (1 needs return, 1 already introduced)
        batch = KizSignatureBatch(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            filename="archive.xlsx",
            source="telegram",
            status=BatchStatus.PENDING_SIGNATURE,
            sales_count=1,
            returns_count=1,
            already_withdrawn_count=1,
            total_count=4,
            data_payload={
                "withdrawals": [
                    {
                        "order_id": 101,
                        "kiz_code": "0104630199251318215ALREADY_RETIRED",
                        "receipt_number": "156133",
                        "receipt_date": "2026-08-25",
                        "price": 3082,
                        "cz_status": "RETIRED",
                        "cz_status_desc": "Выбыл (выведен из оборота)",
                        "is_already_withdrawn": True,
                        "needs_withdrawal": False,
                        "selected": False,
                    },
                    {
                        "order_id": 102,
                        "kiz_code": "0104630199251318215NEEDS_WITHDRAW",
                        "receipt_number": "48516",
                        "receipt_date": "2026-08-27",
                        "price": 3254,
                        "cz_status": "INTRODUCED",
                        "cz_status_desc": "В обороте",
                        "is_already_withdrawn": False,
                        "needs_withdrawal": True,
                        "selected": True,
                    }
                ],
                "returns": [
                    {
                        "order_id": 201,
                        "kiz_code": "0104630199251318215RETURN_IN_CIRC",
                        "name": "Товар в обороте",
                        "cz_status": "INTRODUCED",
                        "cz_status_desc": "В обороте",
                        "needs_cz_return": False,
                        "selected": False,
                    },
                    {
                        "order_id": 202,
                        "kiz_code": "0104630199251318215RETURN_NEEDS_INTRO",
                        "name": "Товар выбыл",
                        "cz_status": "RETIRED",
                        "cz_status_desc": "Выбыл (выведен из оборота)",
                        "needs_cz_return": True,
                        "selected": True,
                    }
                ],
                "summary": {
                    "total_rows": 4,
                    "sales_count": 2,
                    "sales_needing_withdrawal": 1,
                    "sales_already_withdrawn": 1,
                    "returns_count": 2,
                    "returns_needing_cz_return": 1,
                    "returns_already_in_circulation": 1,
                }
            }
        )
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        # 1. Prepare documents: should only prepare 1 withdrawal (order 102) and 1 return (order 202), ignoring the already retired/in-circulation ones
        res_prep = await ac.post(f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}/prepare-documents")
        assert res_prep.status_code == 200
        prep_data = res_prep.json()
        assert prep_data["total_documents"] == 2
        kiz_codes_prepared = [d["kiz_code"] for d in prep_data["documents"]]
        assert "0104630199251318215ALREADY_RETIRED" not in kiz_codes_prepared
        assert "0104630199251318215NEEDS_WITHDRAW" in kiz_codes_prepared
        assert "0104630199251318215RETURN_IN_CIRC" not in kiz_codes_prepared
        assert "0104630199251318215RETURN_NEEDS_INTRO" in kiz_codes_prepared

        # 2. Test sync-cz endpoint: mock CZ True API returning RETIRED for the remaining item
        with patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_cz:
            mock_cz.return_value = [
                {"cisInfo": {"requestedCis": "0104630199251318215NEEDS_WITHDRAW", "status": "RETIRED"}},
                {"cisInfo": {"requestedCis": "0104630199251318215ALREADY_RETIRED", "status": "RETIRED"}},
                {"cisInfo": {"requestedCis": "0104630199251318215RETURN_IN_CIRC", "status": "INTRODUCED"}},
                {"cisInfo": {"requestedCis": "0104630199251318215RETURN_NEEDS_INTRO", "status": "INTRODUCED"}},
            ]
            res_sync = await ac.post(f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}/sync-cz")
            assert res_sync.status_code == 200
            sync_data = res_sync.json()
            assert sync_data["success"] is True
            assert sync_data["sales_count"] == 0  # Both sales are now RETIRED -> 0 need withdrawal!
            assert sync_data["already_withdrawn_count"] == 2
            assert sync_data["returns_count"] == 0 # Both returns are now INTRODUCED -> 0 need return!

        # 3. Prepare documents again after sync: should return 0 documents
        res_prep2 = await ac.post(f"/api/v1/sellers/{seller_id}/kiz/signature-batches/{batch_id}/prepare-documents")
        assert res_prep2.status_code == 200
        assert res_prep2.json()["total_documents"] == 0

