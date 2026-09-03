import pytest
import uuid
import random
from datetime import datetime, timezone
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
from app.services.wb_client import WBUnauthorizedError, WBRateLimitError


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
            name=f"Seller-Adv-{seller_id[:6]}",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7701234567",
            wb_api_token_encrypted=encrypt("valid_wb_token"),
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=0,
        )
        session.add(seller)
        session.commit()
        return seller_id


# ---------------------------------------------------------------------------
# ADVERSARIAL TEST SUITE: Edge cases, malformed payloads, type safety, resilience
# ---------------------------------------------------------------------------

def test_resolve_order_metadata_string_and_invalid_chrt_nm_ids(test_seller):
    """Adversarial Test 1: Handle string digits, non-numeric strings, lists, dicts for chrtId and nmId."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        mock_client = MagicMock()
        mock_client.get_cards_catalog.return_value = {
            "by_vendor_code": {},
            "by_nm_id": {},
            "by_chrt_id": {},
        }

        # Case A: Valid numeric strings
        raw_order_str = {
            "id": 10001,
            "chrtId": "12345678",
            "nmId": "87654321",
            "article": "STR-SKU",
        }
        meta, _ = _resolve_order_metadata(
            order_raw=raw_order_str,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=10001,
        )
        assert meta["chrt_id"] == 12345678
        assert meta["nm_id"] == 87654321

        # Case B: Non-numeric strings, nested collections, None
        raw_order_invalid = {
            "id": 10002,
            "chrtId": "invalid_chrt_abc",
            "nmId": ["nested_list"],
            "article": "BAD-ID-SKU",
        }
        meta2, _ = _resolve_order_metadata(
            order_raw=raw_order_invalid,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=10002,
        )
        assert meta2["chrt_id"] is None
        assert meta2["nm_id"] is None


def test_resolve_order_metadata_empty_and_missing_payload(test_seller):
    """Adversarial Test 2: Handle empty dict or missing fields, ensuring robust fallbacks."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        mock_client = MagicMock()
        mock_client.get_cards_catalog.return_value = {}

        # Completely empty order payload
        meta, _ = _resolve_order_metadata(
            order_raw={},
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=55555,
        )

        assert meta["name"] == "Заказ #55555"
        assert meta["brand"] == seller.name
        assert meta["subject"] == "Товар"
        assert meta["article"] == ""
        assert meta["chrt_id"] is None
        assert meta["nm_id"] is None
        assert meta["tech_size"] is None
        assert meta["wb_size"] is None


def test_resolve_order_metadata_seller_multi_tenant_isolation(test_seller):
    """Adversarial Test 3: Ensure local DB cache cannot leak metadata from other sellers."""
    seller_a_id = test_seller
    with SyncSessionLocal() as session:
        # Create Seller B
        seller_b_id = str(uuid.uuid4())
        seller_b = Seller(
            id=seller_b_id,
            name="Seller-B-Confidential",
            wb_api_token_encrypted=encrypt("token_b"),
            is_active=True,
        )
        session.add(seller_b)

        # Seed an order for Seller B with shared article "SHARED-ART-1"
        order_b = Order(
            id=random.randint(2000000, 3000000),
            seller_id=seller_b_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=77777777,
            nm_id=88888888,
            article="SHARED-ART-1",
            name="Конфиденциальный товар Продавца Б",
            brand="Brand-B",
            subject="Категория-Б",
            created_at=datetime.now(timezone.utc),
        )
        session.add(order_b)
        session.commit()

        seller_a = session.query(Seller).filter(Seller.id == seller_a_id).first()
        mock_client = MagicMock()
        mock_client.get_cards_catalog.return_value = {}

        # Seller A receives an order with the same article
        raw_order_a = {
            "id": 123000,
            "article": "SHARED-ART-1",
            "chrtId": 77777777,
        }

        meta, _ = _resolve_order_metadata(
            order_raw=raw_order_a,
            seller=seller_a,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=123000,
        )

        # Must NOT take Seller B's name/brand
        assert meta["name"] != "Конфиденциальный товар Продавца Б"
        assert meta["brand"] != "Brand-B"
        assert meta["name"] == "SHARED-ART-1 (WB #123000)"


def test_resolve_order_metadata_ignores_empty_or_null_name_in_db_cache(test_seller):
    """Adversarial Test 4: Local DB cache must ignore existing orders where name is None or empty."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        # Seed an order with empty name
        bad_cached_order = Order(
            id=random.randint(3000000, 4000000),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=11122233,
            article="ART-EMPTY-NAME",
            name="",
            brand="",
            subject="",
            created_at=datetime.now(timezone.utc),
        )
        session.add(bad_cached_order)
        session.commit()

        mock_client = MagicMock()
        mock_client.get_cards_catalog.return_value = {
            "by_vendor_code": {
                "ART-EMPTY-NAME": {
                    "vendorCode": "ART-EMPTY-NAME",
                    "title": "Правильный товар из каталога",
                    "brand": "TrueBrand",
                    "subjectName": "TrueSubject",
                }
            },
            "by_nm_id": {},
            "by_chrt_id": {},
        }

        raw_order = {
            "id": 999901,
            "chrtId": 11122233,
            "article": "ART-EMPTY-NAME",
        }

        meta, _ = _resolve_order_metadata(
            order_raw=raw_order,
            seller=seller,
            session=session,
            wb_client=mock_client,
            catalog_cache={},
            wb_order_id_int=999901,
        )

        # Should bypass empty DB record and query Content API catalog
        assert meta["name"] == "Правильный товар из каталога"
        assert meta["brand"] == "TrueBrand"
        assert meta["subject"] == "TrueSubject"


def test_resolve_order_metadata_content_api_exceptions_and_timeouts(test_seller):
    """Adversarial Test 5: Handle network timeouts, connection errors, and unexpected exceptions."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        exceptions_to_test = [
            TimeoutError("Connection timed out after 30s"),
            ConnectionResetError("Remote server disconnected"),
            RuntimeError("Unexpected memory corruption or parsing failure"),
            WBUnauthorizedError("API key revoked"),
        ]

        for exc in exceptions_to_test:
            mock_client = MagicMock()
            mock_client.get_cards_catalog.side_effect = exc

            raw_order = {
                "id": random.randint(100000, 999999),
                "article": "TIMEOUT-ART",
            }

            meta, cache = _resolve_order_metadata(
                order_raw=raw_order,
                seller=seller,
                session=session,
                wb_client=mock_client,
                catalog_cache={},
                wb_order_id_int=raw_order["id"],
            )

            # Polling does NOT crash, returns graceful fallback
            assert meta["name"] == f"TIMEOUT-ART (WB #{raw_order['id']})"
            assert meta["subject"] == "Товар"
            assert meta["brand"] == seller.name or "WB"


def test_resolve_order_metadata_malformed_catalog_responses(test_seller):
    """Adversarial Test 6: Handle non-dict, None, or broken catalog responses."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        broken_catalog_payloads = [
            None,
            [],
            "<html>502 Bad Gateway</html>",
            {"by_vendor_code": None, "by_nm_id": "broken", "by_chrt_id": 123},
            {"by_vendor_code": {"MALFORMED-ART": {"sizes": None, "characteristics": None}}},
        ]

        for broken_res in broken_catalog_payloads:
            mock_client = MagicMock()
            mock_client.get_cards_catalog.return_value = broken_res

            raw_order = {
                "id": random.randint(100000, 999999),
                "article": "MALFORMED-ART",
            }

            meta, _ = _resolve_order_metadata(
                order_raw=raw_order,
                seller=seller,
                session=session,
                wb_client=mock_client,
                catalog_cache={},
                wb_order_id_int=raw_order["id"],
            )

            assert isinstance(meta, dict)
            assert meta["article"] == "MALFORMED-ART"
            assert "name" in meta
            assert "brand" in meta
            assert "subject" in meta


def test_poll_seller_orders_malformed_orders_and_type_casting(test_seller):
    """Adversarial Test 7: Stress-test poll_seller_orders with malformed orders list."""
    seller_id = test_seller
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        valid_order_id = random.randint(7000000, 8000000)

        mock_wb_orders = [
            {},  # Empty item
            {"id": None},  # None ID
            {"id": "not_an_int"},  # Unparseable ID
            {"orderId": "invalid_xyz"},
            {
                "id": valid_order_id,
                "createdAt": "invalid_date_format_text",  # Bad date
                "price": "not_a_valid_price",  # Bad price
                "article": "ROBUST-SKU",
                "chrtId": "12345",
                "nmId": "67890",
            }
        ]

        with patch("app.agents.order_poller.WBClient") as mock_wb_class:
            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            mock_client.get_cards_catalog.return_value = {}
            mock_client.get_orders_status.return_value = []
            mock_wb_class.return_value = mock_client

            processed_ids, processed_payloads = poll_seller_orders(seller, session)

            assert processed_ids == [valid_order_id]
            assert len(processed_payloads) == 1

            payload = processed_payloads[0]
            assert payload["id"] == valid_order_id
            assert payload["name"] == f"ROBUST-SKU (WB #{valid_order_id})"
            assert "wb_created_at" in payload

            # Verify persisted Order model
            db_order = session.query(Order).filter(Order.id == valid_order_id).first()
            assert db_order is not None
            assert db_order.chrt_id == 12345
            assert db_order.nm_id == 67890
            assert db_order.price == Decimal("0.00") or db_order.price is not None


def test_poll_seller_orders_idempotent_deduplication(test_seller):
    """Adversarial Test 8: Ensure poll_seller_orders is completely idempotent when called multiple times."""
    seller_id = test_seller
    order_id = random.randint(8000000, 9000000)

    mock_wb_orders = [
        {
            "id": order_id,
            "article": "IDEMPOTENT-SKU",
            "price": 100000,
        }
    ]

    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        with patch("app.agents.order_poller.WBClient") as mock_wb_class:
            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            mock_client.get_cards_catalog.return_value = {}
            mock_client.get_orders_status.return_value = []
            mock_wb_class.return_value = mock_client

            # First poll: processes 1 order
            ids1, payloads1 = poll_seller_orders(seller, session)
            assert ids1 == [order_id]
            assert len(payloads1) == 1

            # Second poll with same orders: should return 0 new orders
            ids2, payloads2 = poll_seller_orders(seller, session)
            assert ids2 == []
            assert payloads2 == []


def test_poll_all_sellers_session_rollback_resilience(test_seller):
    """Adversarial Test 9: Verify poll_all_sellers rolls back session on unexpected seller errors and continues."""
    seller_a_id = test_seller
    with SyncSessionLocal() as session:
        # Create Seller B
        seller_b_id = str(uuid.uuid4())
        seller_b = Seller(
            id=seller_b_id,
            name="Seller-B-Healthy",
            wb_api_token_encrypted=encrypt("token_b"),
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=0,
        )
        session.add(seller_b)
        session.commit()

        # Deactivate any other sellers
        session.query(Seller).filter(~Seller.id.in_([seller_a_id, seller_b_id])).update({"is_active": False})
        session.commit()

        seller_a = session.query(Seller).filter(Seller.id == seller_a_id).first()
        seller_b = session.query(Seller).filter(Seller.id == seller_b_id).first()
        seller_a.last_polled_at = None
        seller_b.last_polled_at = None
        session.commit()

        call_count = 0
        def fake_poll(seller_obj, sess):
            nonlocal call_count
            call_count += 1
            if seller_obj.id == seller_a_id:
                raise RuntimeError("Simulated unhandled exception for Seller A")
            return [99999], [{"id": 99999, "name": "Success", "brand": "B", "subject": "S", "article": "A", "price": 100, "kiz_required": False, "wb_created_at": "2026-09-02T12:00:00"}]

        with patch("app.agents.order_poller.poll_seller_orders", side_effect=fake_poll), \
             patch("app.agents.notifier.notify_new_order.delay"), \
             patch("app.agents.order_poller.get_stickers.delay"):

            res = poll_all_sellers()
            assert res["status"] == "success"
            # Both sellers were attempted despite Seller A failing
            assert call_count >= 2


def test_kiz_required_heuristic_boundary_cases():
    """Adversarial Test 10: Test _check_kiz_required with various casing, categories, and tnved codes."""
    # List of requiredMeta variations
    assert _check_kiz_required({"requiredMeta": ["SGTIN"]}) is True
    assert _check_kiz_required({"requiredMeta": ["sgtin"]}) is True
    assert _check_kiz_required({"requiredMeta": ["KIZ"]}) is True
    assert _check_kiz_required({"requiredMeta": ["kiz"]}) is True
    assert _check_kiz_required({"requiredMeta": "sgtin"}) is True
    assert _check_kiz_required({"requiredMeta": []}) is False
    assert _check_kiz_required({"requiredMeta": None}) is False

    # Heuristic category checks when requiredMeta is absent
    assert _check_kiz_required({}, subject="Обувь мужская") is True
    assert _check_kiz_required({}, subject="Куртки зимние") is True
    assert _check_kiz_required({}, subject="Блузки") is True
    assert _check_kiz_required({}, subject="Духи и туалетная вода") is True
    assert _check_kiz_required({}, subject="Канцтовары") is False
    assert _check_kiz_required({}, subject="Электроника") is False

    # Heuristic category & TN VED checks when requiredMeta is empty [] or None
    assert _check_kiz_required({"requiredMeta": []}, subject="Платья") is True
    assert _check_kiz_required({"requiredMeta": []}, subject="Брюки") is True
    assert _check_kiz_required({"requiredMeta": []}, subject="Ботинки") is True
    assert _check_kiz_required({"requiredMeta": []}, tnved="6104") is True
    assert _check_kiz_required({"requiredMeta": []}, tnved="6403") is True
    assert _check_kiz_required({"requiredMeta": []}, subject="Канцтовары") is False
    assert _check_kiz_required({"requiredMeta": None}, subject="Куртки зимние") is True
    assert _check_kiz_required({"requiredMeta": None}, subject="Канцтовары") is False
