import pytest
import pytest_asyncio
import uuid
import random
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

from app.database import init_db
from app.agents.order_poller import (
    poll_seller_orders,
    _resolve_order_metadata,
    _check_kiz_required,
    poll_all_sellers,
    SyncSessionLocal,
    _last_polled,
)
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.services.encryption import encrypt


@pytest.fixture(autouse=True)
def setup_db():
    import asyncio
    asyncio.run(init_db())
    _last_polled.clear()
    yield


@pytest.fixture
def test_seller():
    with SyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name=f"Seller-Enrichment-{seller_id[:6]}",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7712345678",
            wb_api_token_encrypted=encrypt("valid_wb_token"),
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=0,
        )
        session.add(seller)
        session.commit()
        return seller_id


# ---------------------------------------------------------------------------
# Tier 1 & 2: Unit tests for _resolve_order_metadata & _check_kiz_required
# ---------------------------------------------------------------------------

def test_resolve_order_metadata_from_local_db_cache_by_chrt_id(test_seller):
    """Verify metadata resolution hits local DB cache when matching chrt_id exists."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        # Seed existing order with full metadata
        cached_order = Order(
            id=random.randint(1000000, 2000000),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=98765432,
            nm_id=12345678,
            article="ART-TSHIRT-BLUE-M",
            name="Футболка хлопковая синяя M",
            brand="MegaBrand",
            subject="Футболки",
            tech_size="M",
            wb_size="48",
            price=Decimal("1500.00"),
            kiz_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(cached_order)
        session.commit()

        mock_client = MagicMock()
        mock_client.get_cards_catalog = MagicMock(return_value={
            "by_vendor_code": {},
            "by_nm_id": {},
            "by_chrt_id": {},
        })

        raw_order = {
            "id": 999111222,
            "chrtId": 98765432,
            "nmId": 12345678,
            "article": "ART-TSHIRT-BLUE-M",
            "price": 150000,
        }

        meta, cache = _resolve_order_metadata(
            order_raw=raw_order,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=999111222,
        )

        # Content API should NOT be called because DB cache resolved it
        assert mock_client.get_cards_catalog.call_count == 0
        assert meta["name"] == "Футболка хлопковая синяя M"
        assert meta["brand"] == "MegaBrand"
        assert meta["subject"] == "Футболки"
        assert meta["tech_size"] == "M"
        assert meta["wb_size"] == "48"
        assert meta["chrt_id"] == 98765432
        assert meta["nm_id"] == 12345678


def test_resolve_order_metadata_from_local_db_cache_by_article(test_seller):
    """Verify metadata resolution hits local DB cache when matching article exists."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        cached_order = Order(
            id=random.randint(1000000, 2000000),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=11223344,
            nm_id=55667788,
            article="HOODIE-BLACK-XL",
            name="Худи оверсайз чёрное XL",
            brand="WarmCo",
            subject="Толстовки",
            tech_size="XL",
            wb_size="52",
            price=Decimal("2500.00"),
            kiz_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(cached_order)
        session.commit()

        mock_client = MagicMock()
        raw_order = {
            "id": 888222333,
            "article": "HOODIE-BLACK-XL",
            "price": 250000,
        }

        meta, cache = _resolve_order_metadata(
            order_raw=raw_order,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=888222333,
        )

        assert mock_client.get_cards_catalog.call_count == 0
        assert meta["name"] == "Худи оверсайз чёрное XL"
        assert meta["brand"] == "WarmCo"
        assert meta["subject"] == "Толстовки"
        assert meta["tech_size"] == "XL"
        assert meta["wb_size"] == "52"


def test_resolve_order_metadata_from_wb_content_api(test_seller):
    """Verify metadata resolution queries WB Content API when DB cache misses."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        mock_client = MagicMock()
        mock_client.get_cards_catalog = MagicMock(return_value={
            "by_vendor_code": {
                "SNEAKER-AIR-42": {
                    "vendorCode": "SNEAKER-AIR-42",
                    "nmID": 44556677,
                    "title": "Кроссовки беговые",
                    "subjectName": "Кроссовки",
                    "brand": "SportStep",
                    "sizes": [{"chrtID": 77889900, "techSize": "42", "wbSize": "42"}],
                    "tnved": "6404110000",
                }
            },
            "by_nm_id": {},
            "by_chrt_id": {
                77889900: {
                    "vendorCode": "SNEAKER-AIR-42",
                    "nmID": 44556677,
                    "title": "Кроссовки беговые",
                    "subjectName": "Кроссовки",
                    "brand": "SportStep",
                    "techSize": "42",
                    "wbSize": "42",
                    "tnved": "6404110000",
                }
            },
        })

        raw_order = {
            "id": 777333444,
            "chrtId": 77889900,
            "nmId": 44556677,
            "article": "SNEAKER-AIR-42",
            "price": 490000,
        }

        meta, cache = _resolve_order_metadata(
            order_raw=raw_order,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=777333444,
        )

        assert mock_client.get_cards_catalog.call_count == 1
        assert meta["name"] == "Кроссовки беговые"
        assert meta["brand"] == "SportStep"
        assert meta["subject"] == "Кроссовки"
        assert meta["tech_size"] == "42"
        assert meta["wb_size"] == "42"
        assert meta["tnved"] == "6404110000"


def test_resolve_order_metadata_graceful_fallback_on_content_api_error(test_seller):
    """Verify graceful fallback to article and default values when Content API raises exception."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        mock_client = MagicMock()
        mock_client.get_cards_catalog = MagicMock(side_effect=Exception("WB Content API 500 Internal Server Error"))

        raw_order = {
            "id": 666444555,
            "article": "UNKNOWN-SKU-99",
            "price": 99000,
        }

        meta, cache = _resolve_order_metadata(
            order_raw=raw_order,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=666444555,
        )

        assert meta["name"] == "UNKNOWN-SKU-99 (WB #666444555)"
        assert meta["brand"] == seller.name or "WB"
        assert meta["subject"] == "Товар"
        assert meta["article"] == "UNKNOWN-SKU-99"


# ---------------------------------------------------------------------------
# Tier 3 & 4: poll_seller_orders end-to-end integration tests
# ---------------------------------------------------------------------------

def test_poll_seller_orders_end_to_end_enrichment_and_persistence(test_seller):
    """
    Test poll_seller_orders full flow:
    - Fetches new orders from WB API
    - Resolves metadata via WB Content API
    - Saves Order record with chrt_id, nm_id, name, brand, subject, tech_size, wb_size
    - Generates notification payload with brand and subject
    """
    seller_id = test_seller
    wb_order_id = random.randint(5000000, 9000000)

    mock_wb_orders = [
        {
            "id": wb_order_id,
            "rid": "rid_12345",
            "createdAt": "2026-09-02T10:00:00Z",
            "article": "DRESS-SILK-RED-S",
            "chrtId": 33445566,
            "nmId": 99887766,
            "price": 350000,
            "requiredMeta": ["sgtin"],
        }
    ]

    mock_catalog = {
        "by_vendor_code": {
            "DRESS-SILK-RED-S": {
                "vendorCode": "DRESS-SILK-RED-S",
                "nmID": 99887766,
                "title": "Платье шёлковое вечернее S",
                "subjectName": "Платья",
                "brand": "SilkElegance",
                "sizes": [{"chrtID": 33445566, "techSize": "S", "wbSize": "42"}],
                "tnved": "6204420000",
            }
        },
        "by_nm_id": {},
        "by_chrt_id": {
            33445566: {
                "vendorCode": "DRESS-SILK-RED-S",
                "nmID": 99887766,
                "title": "Платье шёлковое вечернее S",
                "subjectName": "Платья",
                "brand": "SilkElegance",
                "techSize": "S",
                "wbSize": "42",
                "tnved": "6204420000",
            }
        },
    }

    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        with patch("app.agents.order_poller.WBClient") as mock_wb_class:
            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            mock_client.get_cards_catalog.return_value = mock_catalog
            mock_client.get_orders_status.return_value = []
            mock_wb_class.return_value = mock_client

            processed_ids, processed_payloads = poll_seller_orders(seller, session)

            assert processed_ids == [wb_order_id]
            assert len(processed_payloads) == 1

            payload = processed_payloads[0]
            assert payload["id"] == wb_order_id
            assert payload["name"] == "Платье шёлковое вечернее S"
            assert payload["brand"] == "SilkElegance"
            assert payload["subject"] == "Платья"
            assert payload["article"] == "DRESS-SILK-RED-S"
            assert payload["price"] == 350000
            assert payload["kiz_required"] is True
            assert "wb_created_at" in payload

            # Verify persisted DB record
            db_order = session.query(Order).filter(Order.id == wb_order_id).first()
            assert db_order is not None
            assert db_order.seller_id == seller_id
            assert db_order.chrt_id == 33445566
            assert db_order.nm_id == 99887766
            assert db_order.name == "Платье шёлковое вечернее S"
            assert db_order.brand == "SilkElegance"
            assert db_order.subject == "Платья"
            assert db_order.tech_size == "S"
            assert db_order.wb_size == "42"
            assert db_order.price == Decimal("3500.00")
            assert db_order.kiz_required is True
            assert db_order.kiz_status == KizStatus.PENDING


def test_poll_seller_orders_batch_mixed_cache_and_catalog(test_seller):
    """
    Test batch order polling with multiple orders:
    - Order 1: Hits local DB cache
    - Order 2: Hits WB Content API catalog
    - Order 3: Fallback due to missing card info
    """
    seller_id = test_seller
    order_id_1 = random.randint(5000000, 6000000)
    order_id_2 = random.randint(6000001, 7000000)
    order_id_3 = random.randint(7000001, 8000000)

    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        # Seed cache for Order 1
        cached_order = Order(
            id=random.randint(1000000, 2000000),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=111111,
            nm_id=222222,
            article="SKU-CACHED",
            name="Товар из кэша БД",
            brand="BrandCache",
            subject="КэшКатегория",
            tech_size="L",
            wb_size="50",
            price=Decimal("1000.00"),
            kiz_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(cached_order)
        session.commit()

        mock_wb_orders = [
            {
                "id": order_id_1,
                "chrtId": 111111,
                "nmId": 222222,
                "article": "SKU-CACHED",
                "price": 100000,
            },
            {
                "id": order_id_2,
                "chrtId": 333333,
                "nmId": 444444,
                "article": "SKU-CATALOG",
                "price": 200000,
            },
            {
                "id": order_id_3,
                "article": "SKU-UNKNOWN",
                "price": 50000,
            },
        ]

        mock_catalog = {
            "by_vendor_code": {
                "SKU-CATALOG": {
                    "vendorCode": "SKU-CATALOG",
                    "nmID": 444444,
                    "title": "Товар из каталога WB",
                    "subjectName": "КаталогКатегория",
                    "brand": "BrandCatalog",
                    "sizes": [{"chrtID": 333333, "techSize": "M", "wbSize": "48"}],
                }
            },
            "by_nm_id": {},
            "by_chrt_id": {},
        }

        with patch("app.agents.order_poller.WBClient") as mock_wb_class:
            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            mock_client.get_cards_catalog.return_value = mock_catalog
            mock_client.get_orders_status.return_value = []
            mock_wb_class.return_value = mock_client

            processed_ids, processed_payloads = poll_seller_orders(seller, session)

            assert len(processed_ids) == 3
            assert len(processed_payloads) == 3

            # Order 1 (DB Cache)
            p1 = next(p for p in processed_payloads if p["id"] == order_id_1)
            assert p1["name"] == "Товар из кэша БД"
            assert p1["brand"] == "BrandCache"
            assert p1["subject"] == "КэшКатегория"

            # Order 2 (WB Catalog)
            p2 = next(p for p in processed_payloads if p["id"] == order_id_2)
            assert p2["name"] == "Товар из каталога WB"
            assert p2["brand"] == "BrandCatalog"
            assert p2["subject"] == "КаталогКатегория"

            # Order 3 (Fallback)
            p3 = next(p for p in processed_payloads if p["id"] == order_id_3)
            assert p3["name"] == f"SKU-UNKNOWN (WB #{order_id_3})"
            assert p3["brand"] == seller.name or "WB"
            assert p3["subject"] == "Товар"


def test_poll_all_sellers_dispatches_single_and_batch_notifications(test_seller):
    """Verify poll_all_sellers triggers single order notification or batch notification appropriately."""
    seller_id = test_seller
    order_id = random.randint(8000000, 9000000)

    with SyncSessionLocal() as session:
        # Deactivate other sellers for this specific unit test so only test_seller is polled
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = session.query(Seller).filter(Seller.id == seller_id).first()
        seller.last_polled_at = None
        seller.is_active = True
        seller.polling_enabled = True
        session.commit()

        mock_wb_orders = [
            {
                "id": order_id,
                "article": "SOLO-ITEM",
                "price": 120000,
            }
        ]

        with patch("app.agents.order_poller.WBClient") as mock_wb_class, \
             patch("app.agents.notifier.notify_new_order.delay") as mock_notify_single, \
             patch("app.agents.order_poller.get_stickers.delay") as mock_stickers:

            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            mock_client.get_cards_catalog.return_value = {}
            mock_client.get_orders_status.return_value = []
            mock_wb_class.return_value = mock_client

            result = poll_all_sellers()

            assert result["status"] == "success"
            assert result["new_orders"] >= 1

            mock_notify_single.assert_called_once()
            args, _ = mock_notify_single.call_args
            assert args[0] == seller_id
            assert args[1] == order_id
            payload = args[2]
            assert payload["id"] == order_id
            assert payload["name"] == f"SOLO-ITEM (WB #{order_id})"
            assert "brand" in payload
            assert "subject" in payload


def test_poll_all_sellers_scheduled_mode_suppresses_instant_alerts_and_runs_stickers(test_seller):
    """Verify that when seller has notification_mode='scheduled', instant telegram alerts are suppressed,
    orders are saved with notified_at=None, and get_stickers.delay IS called."""
    seller_id = test_seller
    order_id = random.randint(9100000, 9900000)
    _last_polled.clear()

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = session.query(Seller).filter(Seller.id == seller_id).first()
        seller.last_polled_at = None
        seller.is_active = True
        seller.polling_enabled = True
        seller.polling_interval_seconds = 0
        seller.notification_mode = "scheduled"
        seller.notification_schedule = ["10:00", "14:00", "18:00"]
        session.commit()

    mock_wb_orders = [
        {
            "id": order_id,
            "article": "SCHEDULED-ITEM",
            "price": 250000,
        }
    ]

    with patch("app.agents.order_poller.WBClient") as mock_wb_class, \
         patch("app.agents.notifier.notify_new_order.delay") as mock_notify_single, \
         patch("app.agents.notifier.notify_batch_orders.delay") as mock_notify_batch, \
         patch("app.agents.order_poller.get_stickers.delay") as mock_stickers:

        mock_client = MagicMock()
        mock_client.get_new_orders.return_value = mock_wb_orders
        mock_client.get_cards_catalog.return_value = {}
        mock_client.get_orders_status.return_value = []
        mock_wb_class.return_value = mock_client

        result = poll_all_sellers()

        assert result["status"] == "success"
        assert result["new_orders"] >= 1

        # Instant notifications MUST be suppressed
        mock_notify_single.assert_not_called()
        mock_notify_batch.assert_not_called()

        # Background stickers generation MUST still be queued
        mock_stickers.assert_called_once_with(seller_id, order_id)

    # Verify in DB: order exists and notified_at is None
    with SyncSessionLocal() as session:
        saved_order = session.query(Order).filter(Order.id == str(order_id)).first()
        assert saved_order is not None
        assert saved_order.notified_at is None
        assert saved_order.seller_id == seller_id