"""
Tests for National Catalog (НКТ) module:
- NKClient requests and error handling
- ProductCard CRUD API endpoints
- Moderation, document retrieval, and PKCS#7 signing workflows
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, AsyncSessionLocal, engine
from app.models.seller import Seller
from app.national_catalog.models import ProductCard
from app.national_catalog.client import NKClient
from app.services.encryption import encrypt
from app.config import settings


@pytest.fixture(autouse=True)
async def cleanup_db_engine():
    yield
    await engine.dispose()


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


@pytest.mark.asyncio
async def test_nk_client_feed_creation_and_status():
    """Test NKClient feed submission and feed status polling."""
    client = NKClient(token="mock-cz-token")

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"result": {"feed_id": 999111}}

        feed_id = await client.create_or_update_feed(
            goods=[{"name": "Тестовая рубашка", "article": "SHIRT-01"}]
        )
        assert feed_id == 999111
        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "POST"
        assert mock_req.call_args[0][1] == "/nk/feed"


@pytest.mark.asyncio
async def test_nk_client_document_and_signing():
    """Test NKClient XML document fetching and detached PKCS#7 submission."""
    client = NKClient(token="mock-cz-token")

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {
            "result": [
                {
                    "xmls": [
                        {"goodId": 123456, "gtin": "04670001234567", "xml": "PHhtbD50ZXN0PC94bWw+"}
                    ]
                }
            ]
        }
        docs = await client.get_product_document_xml(good_ids=[123456])
        assert len(docs) == 1
        assert docs[0]["goodId"] == 123456
        assert docs[0]["xml"] == "PHhtbD50ZXN0PC94bWw+"

        mock_req.return_value = {"result": {"signed": [123456]}}
        sign_resp = await client.sign_product_pkcs(
            signed_items=[{"goodId": 123456, "base64Xml": "...", "signature": "..."}]
        )
        assert sign_resp["signed"] == [123456]


@pytest.mark.asyncio
async def test_product_card_api_crud_and_status_cycle():
    """Integration test for product cards REST API endpoints."""
    await init_db()
    seller_id = f"test-nk-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="НК Тест Магазин",
            wb_api_token_encrypted=encrypt("mock-wb"),
            cz_token_encrypted=encrypt("mock-cz-token"),
            cz_inn="7701234567",
            is_active=True
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        # 1. Create card via API (mock NKClient.create_or_update_feed to return feed_id)
        with patch.object(NKClient, "create_or_update_feed", new_callable=AsyncMock) as mock_feed:
            mock_feed.return_value = 777123

            create_payload = {
                "name": "Футболка оверсайз черная",
                "brand": "TrueBrand",
                "gtin": "04670001234567",
                "tnved": "6203420000",
                "category_id": 20000003,
                "category_name": "Одежда",
                "attributes": [
                    {"attr_id": 10609, "attr_value": "6203420000"},
                    {"attr_id": 10610, "attr_value": "100% хлопок"}
                ],
                "moderation": True
            }

            res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products",
                json=create_payload,
                headers=headers
            )
            assert res.status_code == 200, f"Create failed: {res.text}"
            data = res.json()
            card_id = data["id"]
            assert data["name"] == "Футболка оверсайз черная"
            assert data["feed_id"] == 777123
            assert data["status"] == "moderation"

        # 2. List cards
        list_res = await client.get(
            f"/api/v1/sellers/{seller_id}/national-catalog/products",
            headers=headers
        )
        assert list_res.status_code == 200
        items = list_res.json()
        assert any(c["id"] == card_id for c in items)

        # 3. Get single card
        get_res = await client.get(
            f"/api/v1/sellers/{seller_id}/national-catalog/products/{card_id}",
            headers=headers
        )
        assert get_res.status_code == 200
        assert get_res.json()["tnved"] == "6203420000"

        # 4. Check status (mock feed status response)
        with patch.object(NKClient, "get_feed_status", new_callable=AsyncMock) as mock_st, \
             patch.object(NKClient, "get_feed_product", new_callable=AsyncMock) as mock_fp:
            mock_st.return_value = {
                "feed_id": 777123,
                "status": "COMPLETED",
                "goods": [
                    {"good_id": 555666, "status": "notsigned", "gtin": "04670001234567"}
                ]
            }
            mock_fp.return_value = [
                {"good_id": 555666, "status": "notsigned", "gtin": "04670001234567"}
            ]

            chk_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products/{card_id}/check-status",
                headers=headers
            )
            assert chk_res.status_code == 200
            chk_data = chk_res.json()
            assert chk_data["status"] == "notsigned"
            assert chk_data["good_id"] == 555666

        # 5. Prepare sign
        with patch.object(NKClient, "get_product_document_xml", new_callable=AsyncMock) as mock_doc:
            mock_doc.return_value = [
                {"goodId": 555666, "gtin": "04670001234567", "xml": "<xml>signed_data_test</xml>"}
            ]

            prep_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products/{card_id}/prepare-sign",
                headers=headers
            )
            assert prep_res.status_code == 200
            assert prep_res.json()["raw_xml"] == "<xml>signed_data_test</xml>"

        # 6. Publish signed
        with patch.object(NKClient, "sign_product_pkcs", new_callable=AsyncMock) as mock_sign:
            mock_sign.return_value = {"signed": [555666]}

            pub_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products/{card_id}/publish",
                json={"signature": "MIIB...mock_pkcs7_signature..."},
                headers=headers
            )
            assert pub_res.status_code == 200
            assert pub_res.json()["status"] == "published"

        # 7. Cannot delete published card
        del_published = await client.delete(
            f"/api/v1/sellers/{seller_id}/national-catalog/products/{card_id}",
            headers=headers
        )
        assert del_published.status_code == 400

        # 8. Create draft card and delete it
        with patch.object(NKClient, "create_or_update_feed", new_callable=AsyncMock) as mock_feed:
            mock_feed.return_value = 777124
            draft_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products",
                json={
                    "name": "Носки хлопковые",
                    "gtin": "04670001234599",
                    "moderation": False
                },
                headers=headers
            )
            assert draft_res.status_code == 200
            draft_id = draft_res.json()["id"]

            del_draft = await client.delete(
                f"/api/v1/sellers/{seller_id}/national-catalog/products/{draft_id}",
                headers=headers
            )
            assert del_draft.status_code == 200

        # Verify draft not returned in active list
        list_after = await client.get(
            f"/api/v1/sellers/{seller_id}/national-catalog/products",
            headers=headers
        )
        assert not any(c["id"] == draft_id for c in list_after.json())


@pytest.mark.asyncio
async def test_sync_products_from_nk():
    """Test full synchronization of product cards from National Catalog (НКТ) True API."""
    await init_db()
    seller_id = f"test-nk-sync-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="НК Синхронизация Магазин",
            wb_api_token_encrypted=encrypt("mock-wb"),
            cz_token_encrypted=encrypt("mock-cz-token"),
            cz_inn="190207495060",
            is_active=True
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        mock_etags = {
            "goods_count": 2,
            "total": 2,
            "offset": 0,
            "goods": [
                {"good_id": 888101, "etag": "etag1"},
                {"good_id": 888102, "etag": "etag2"}
            ]
        }

        mock_feed_prod_1 = [{
            "good_id": 888101,
            "good_name": "Жилет утепленный синий",
            "good_status": "published",
            "identified_by": [{"type": "gtin", "value": "04603702055000"}],
            "categories": [{"cat_id": 31326, "cat_name": "Одежда"}],
            "brand_name": "VRTN",
            "good_mark_flag": True,
            "good_turn_flag": True,
            "good_attrs": [{"attr_id": 10609, "attr_value": "6202401000"}]
        }]

        mock_feed_prod_2 = [{
            "good_id": 888102,
            "good_name": "Бомбер утепленный черный",
            "good_status": "published",
            "identified_by": [{"type": "gtin", "value": "04603702055017"}],
            "categories": [{"cat_id": 234392, "cat_name": "Куртки"}],
            "brand_name": "VRTN",
            "good_mark_flag": True,
            "good_turn_flag": True,
            "good_attrs": []
        }]

        async def mock_get_feed_product(good_id=None, gtin=None):
            if good_id == 888101:
                return mock_feed_prod_1
            elif good_id == 888102:
                return mock_feed_prod_2
            return []

        with patch.object(NKClient, "get_etags_list", new_callable=AsyncMock) as mock_etags_fn, \
             patch.object(NKClient, "get_feed_product", side_effect=mock_get_feed_product):
            
            mock_etags_fn.return_value = mock_etags

            sync_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/sync-nk",
                headers=headers
            )
            assert sync_res.status_code == 200, f"Sync failed: {sync_res.text}"
            data = sync_res.json()
            assert data["success"] is True
            assert data["total_remote"] == 2
            assert data["synced_count"] == 2
            assert data["created_count"] == 2
            assert data["updated_count"] == 0

        # Verify cards were created in local DB
        list_res = await client.get(
            f"/api/v1/sellers/{seller_id}/national-catalog/products",
            headers=headers
        )
        assert list_res.status_code == 200
        cards = list_res.json()
        assert len(cards) == 2
        card_101 = next(c for c in cards if c["good_id"] == 888101)
        assert card_101["name"] == "Жилет утепленный синий"
        assert card_101["gtin"] == "04603702055000"
        assert card_101["status"] == "published"
        assert card_101["brand"] == "VRTN"
        assert card_101["tnved"] == "6202401000"

        # Sync again to test update path
        mock_feed_prod_1[0]["good_name"] = "Жилет утепленный синий (обновлен)"
        with patch.object(NKClient, "get_etags_list", new_callable=AsyncMock) as mock_etags_fn, \
             patch.object(NKClient, "get_feed_product", side_effect=mock_get_feed_product):
            
            mock_etags_fn.return_value = mock_etags

            sync_res_2 = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/sync-nk?force_refresh=true",
                headers=headers
            )
            assert sync_res_2.status_code == 200
            data2 = sync_res_2.json()
            assert data2["synced_count"] == 2
            assert data2["created_count"] == 0
            assert data2["updated_count"] == 2


@pytest.mark.asyncio
async def test_batch_generate_gtins():
    """Test batch GTIN generation via True API helper."""
    await init_db()
    seller_id = f"test-nk-gtins-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="НК GTIN Магазин",
            wb_api_token_encrypted=encrypt("mock-wb"),
            cz_token_encrypted=encrypt("mock-cz-token"),
            cz_inn="190207495060",
            is_active=True
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        with patch.object(NKClient, "generate_gtin", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = ["04603702055100", "04603702055101", "04603702055102"]

            res = await client.get(
                f"/api/v1/sellers/{seller_id}/national-catalog/helpers/generate-gtins?quantity=3",
                headers=headers
            )
            assert res.status_code == 200
            data = res.json()
            assert len(data["gtins"]) == 3
            assert data["gtins"][0] == "04603702055100"
            mock_gen.assert_called_once_with(quantity=3)


@pytest.mark.asyncio
async def test_batch_create_product_cards():
    """Test creating a series of product cards with sizes, colors, and declaration of conformity in a single feed."""
    await init_db()
    seller_id = f"test-nk-batch-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="НК Серия Магазин",
            wb_api_token_encrypted=encrypt("mock-wb"),
            cz_token_encrypted=encrypt("mock-cz-token"),
            cz_inn="190207495060",
            is_active=True
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)

        batch_payload = {
            "moderation": True,
            "items": [
                {
                    "name": "Жилет утепленный, Черный, р. 44",
                    "brand": "ADELOVE",
                    "tnved": "6202401000",
                    "category_id": 31326,
                    "category_name": "Одежда",
                    "gtin": "04603702055201",
                    "attributes": [
                        {"attr_id": 35, "attr_value": "44"},
                        {"attr_id": 36, "attr_value": "Черный"},
                        {"attr_id": 2483, "attr_value": "100% полиэстер"},
                        {"attr_id": 13914, "attr_value": "VEST-BLK-44"},
                        {"attr_id": 23557, "attr_value": "ЕАЭС N RU Д-RU.РА01.В.12345/22"},
                        {"attr_id": 13836, "attr_value": "ТР ТС 017/2011 \"О безопасности продукции легкой промышленности\""},
                    ]
                },
                {
                    "name": "Жилет утепленный, Черный, р. 46",
                    "brand": "ADELOVE",
                    "tnved": "6202401000",
                    "category_id": 31326,
                    "category_name": "Одежда",
                    "gtin": "04603702055202",
                    "attributes": [
                        {"attr_id": 35, "attr_value": "46"},
                        {"attr_id": 36, "attr_value": "Черный"},
                        {"attr_id": 2483, "attr_value": "100% полиэстер"},
                        {"attr_id": 13914, "attr_value": "VEST-BLK-46"},
                        {"attr_id": 23557, "attr_value": "ЕАЭС N RU Д-RU.РА01.В.12345/22"},
                        {"attr_id": 13836, "attr_value": "ТР ТС 017/2011 \"О безопасности продукции легкой промышленности\""},
                    ]
                }
            ]
        }

        with patch.object(NKClient, "create_or_update_feed", new_callable=AsyncMock) as mock_feed:
            mock_feed.return_value = 555888

            batch_res = await client.post(
                f"/api/v1/sellers/{seller_id}/national-catalog/products/batch",
                json=batch_payload,
                headers=headers
            )
            assert batch_res.status_code == 200, f"Batch create failed: {batch_res.text}"
            data = batch_res.json()
            assert data["success"] is True
            assert data["feed_id"] == 555888
            assert data["created_count"] == 2
            assert len(data["cards"]) == 2

            # Check goods items sent to True API
            mock_feed.assert_called_once()
            goods_sent = mock_feed.call_args[0][0]
            assert len(goods_sent) == 2
            assert goods_sent[0]["good_name"] == "Жилет утепленный, Черный, р. 44"
            assert goods_sent[1]["good_name"] == "Жилет утепленный, Черный, р. 46"

            # Check that declaration attribute was sent
            attrs_0 = goods_sent[0]["good_attrs"]
            decl_attr = next(a for a in attrs_0 if a["attr_id"] == 23557)
            assert decl_attr["attr_value"] == "ЕАЭС N RU Д-RU.РА01.В.12345/22"

        # Verify cards created in local DB
        list_res = await client.get(
            f"/api/v1/sellers/{seller_id}/national-catalog/products",
            headers=headers
        )
        assert list_res.status_code == 200
        cards = list_res.json()
        assert any(c["gtin"] == "04603702055201" and c["feed_id"] == 555888 for c in cards)
        assert any(c["gtin"] == "04603702055202" and c["feed_id"] == 555888 for c in cards)
