"""
Unit and integration tests for Milestone 2 (R2):
Honest Sign (Честный Знак) KIZ heuristics, requiredMeta handling,
order poller metadata injection, and Telegram notification templates.
"""
import pytest
import sys
import uuid
import random
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

from app.database import init_db
from app.agents.order_poller import SyncSessionLocal, _check_kiz_required, poll_seller_orders
from app.services.wb_client import is_kiz_required
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.services.encryption import encrypt


# ---------------------------------------------------------------------------
# Setup aiogram stub for TelegramService tests
# ---------------------------------------------------------------------------
def _stub_aiogram():
    """Stub out aiogram so tests run reliably without network/bot tokens."""
    if "aiogram" not in sys.modules:
        aiogram_mock = MagicMock()
        aiogram_mock.Bot = MagicMock
        aiogram_mock.enums.ParseMode = MagicMock()
        aiogram_mock.exceptions.TelegramAPIError = Exception
        aiogram_mock.types.InlineKeyboardMarkup = MagicMock
        aiogram_mock.types.InlineKeyboardButton = MagicMock
        client_mock = MagicMock()
        client_mock.default.DefaultBotProperties = MagicMock
        sys.modules["aiogram"] = aiogram_mock
        sys.modules["aiogram.client"] = client_mock
        sys.modules["aiogram.client.default"] = client_mock.default
        sys.modules["aiogram.enums"] = aiogram_mock.enums
        sys.modules["aiogram.exceptions"] = aiogram_mock.exceptions
        sys.modules["aiogram.types"] = aiogram_mock.types


@pytest.fixture(autouse=True)
def setup_db():
    import asyncio
    asyncio.run(init_db())
    yield


@pytest.fixture
def test_seller():
    with SyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name=f"Seller-KIZ-{seller_id[:6]}",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700112233",
            wb_api_token_encrypted=encrypt("valid_wb_token"),
            telegram_bot_token_encrypted=encrypt("123456:MOCK_BOT_TOKEN"),
            telegram_chat_ids=["100200300"],
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        session.commit()
        return seller_id


# ---------------------------------------------------------------------------
# 1. Direct Unit Tests for is_kiz_required: Primary Check (requiredMeta)
# ---------------------------------------------------------------------------

def test_is_kiz_required_sgtin_in_required_meta_precedence():
    """Verify that requiredMeta containing 'sgtin' or 'kiz' returns True even for non-marked category."""
    # Even if subject is stationery or electronics, explicit sgtin means marking required
    assert is_kiz_required(subject="Канцтовары", order_raw={"requiredMeta": ["sgtin"]}) is True
    assert is_kiz_required(subject="Канцтовары", order_raw={"requiredMeta": ["SGTIN"]}) is True
    assert is_kiz_required(subject="Электроника", order_raw={"requiredMeta": ["kiz"]}) is True
    assert is_kiz_required(subject="Электроника", order_raw={"requiredMeta": ["KIZ"]}) is True
    assert is_kiz_required(subject="Канцтовары", order_raw={"requiredMeta": "sgtin"}) is True
    assert is_kiz_required(subject=None, order_raw={"requiredMeta": ["sgtin"]}) is True
    assert is_kiz_required(subject="", order_raw={"requiredMeta": ["kiz"]}) is True


# ---------------------------------------------------------------------------
# 2. Direct Unit Tests for is_kiz_required: Empty requiredMeta with Categories
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category", [
    "Платья",
    "Платье вечернее",
    "Брюки женские",
    "Куртки зимние",
    "Куртка демисезонная",
    "Ботинки мужские",
    "Туфли",
    "Сапоги кожаные",
    "Кроссовки",
    "Духи",
    "Туалетная вода",
    "Парфюмерия",
    "Худи",
    "Свитшот",
    "Свитер вязаный",
    "Футболка хлопок",
    "Постельное белье сатин",
    "Полотенца банные",
    "Текстиль для дома",
    "Пальто шерстяное",
    "Пуховик",
    "Джинсы классические",
    "Юбки плиссе",
    "Рубашки",
    "Блузки",
    "Капор утепленный",
    "Шапка трикотажная",
])
def test_is_kiz_required_empty_required_meta_marked_categories(category):
    """Verify empty requiredMeta: [] does NOT return False for marked apparel, footwear, textiles, perfumes."""
    # Passing empty requiredMeta list
    assert is_kiz_required(subject=category, order_raw={"requiredMeta": []}) is True
    # Passing None requiredMeta
    assert is_kiz_required(subject=category, order_raw={"requiredMeta": None}) is True
    # Passing empty order_raw
    assert is_kiz_required(subject=category, order_raw={}) is True


# ---------------------------------------------------------------------------
# 3. Direct Unit Tests for is_kiz_required: Empty requiredMeta with TN VED Codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tnved_code", [
    "6109100000",   # T-shirts knitted
    "6104420000",   # Knitted dresses
    "6201110000",   # Woven coats
    "6203420000",   # Men's trousers
    "6403599900",   # Leather footwear
    "6404110000",   # Sports footwear
    "3303001000",   # Perfumes
    "6302210000",   # Bed linen
    "6505009000",   # Headwear
    "4203100000",   # Leather apparel
    "4303100000",   # Fur apparel
    "4011100000",   # Pneumatic tyres
    "9004100000",   # Sunglasses
    "61",           # Group prefix 61
    "62",           # Group prefix 62
    "64",           # Group prefix 64
    "6301",         # Blankets
    "6302",         # Bed linen
    "6303",         # Curtains
    "6304",         # Furnishing articles
    "6109.10.00",   # Formatted with dots
    "6403 59 99",   # Formatted with spaces
])
def test_is_kiz_required_empty_required_meta_marked_tnved(tnved_code):
    """Verify empty requiredMeta: [] evaluates TN VED prefixes and returns True for marked groups."""
    assert is_kiz_required(tnved=tnved_code, order_raw={"requiredMeta": []}) is True
    assert is_kiz_required(tnved=tnved_code, order_raw={}) is True
    assert is_kiz_required(order_raw={"requiredMeta": [], "tnved": tnved_code}) is True


# ---------------------------------------------------------------------------
# 4. Direct Unit Tests for is_kiz_required: Non-Marked Goods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,tnved_code", [
    ("Канцтовары", "4820100000"),
    ("Электроника", "8517120000"),
    ("Тетрадь школьная", "4820200000"),
    ("Кабель USB Type-C", "8544429007"),
    ("Чехол для телефона", "3926909709"),
    ("Игрушка пластиковая", "9503003500"),
    ("Книга печатная", "4901990000"),
    ("Посуда стеклянная", "7013379900"),
])
def test_is_kiz_required_non_marked_goods_returns_false(category, tnved_code):
    """Verify genuinely non-marked goods return False when requiredMeta is empty and heuristics do not match."""
    # With empty requiredMeta
    assert is_kiz_required(subject=category, tnved=tnved_code, order_raw={"requiredMeta": []}) is False
    # With subject only
    assert is_kiz_required(subject=category, order_raw={"requiredMeta": []}) is False
    # With tnved only
    assert is_kiz_required(tnved=tnved_code, order_raw={"requiredMeta": []}) is False
    # Without requiredMeta
    assert is_kiz_required(subject=category, tnved=tnved_code, order_raw={}) is False


def test_is_kiz_required_empty_payload_without_heuristics():
    """Verify calling with empty order_raw and no subject/tnved returns False."""
    assert is_kiz_required(order_raw={"requiredMeta": []}) is False
    assert is_kiz_required(order_raw={"requiredMeta": None}) is False
    assert is_kiz_required(order_raw={}) is False
    assert is_kiz_required() is False


# ---------------------------------------------------------------------------
# 5. Fallback to order_raw product name
# ---------------------------------------------------------------------------

def test_is_kiz_required_fallback_to_name_in_order_raw():
    """Verify that if subject is generic ('Товар'), product title 'name' is inspected."""
    assert is_kiz_required(subject="Товар", order_raw={"requiredMeta": [], "name": "Платье летнее синее"}) is True
    assert is_kiz_required(subject=None, order_raw={"requiredMeta": [], "name": "Куртка ветрозащитная"}) is True
    assert is_kiz_required(subject="Товар", order_raw={"requiredMeta": [], "name": "Блокнот в клетку"}) is False


# ---------------------------------------------------------------------------
# 6. Order Poller Pipeline: Persistence and Notification Payload Integration
# ---------------------------------------------------------------------------

def test_order_poller_enriches_and_persists_kiz_required_with_empty_required_meta(test_seller):
    """
    Simulate full poll_seller_orders flow:
    - WB returns order with 'requiredMeta': []
    - Catalog cache resolves subject='Платья' and tnved='6104420000'
    - Verify Order.kiz_required is persisted as True
    - Verify Order.kiz_status is KizStatus.PENDING
    - Verify notification payload contains kiz_required: True
    """
    seller_id = test_seller
    order_id = random.randint(7000000, 8000000)

    raw_order = {
        "id": order_id,
        "rid": f"rid_{order_id}",
        "createdAt": "2026-09-03T10:00:00Z",
        "warehouseId": 100,
        "supplyId": None,
        "price": 250000,
        "convertedPrice": 250000,
        "currencyCode": 643,
        "article": "DRESS-RED-44",
        "chrtId": 554433,
        "nmId": 998877,
        "skus": ["SKU-DRESS-44"],
        "requiredMeta": [],  # <--- WB returns empty requiredMeta list!
    }

    mock_wb_client = MagicMock()
    mock_wb_client.get_new_orders.return_value = [raw_order]
    mock_wb_client.get_orders_status.return_value = []
    # Mock WB Content API returning product card with subject 'Платья'
    dress_card = {
        "title": "Платье шелковое красное",
        "subjectName": "Платья",
        "brand": "SilkDream",
        "techSize": "44",
        "wbSize": "44",
        "tnved": "6104420000",
    }
    mock_wb_client.get_cards_catalog.return_value = {
        "by_vendor_code": {"DRESS-RED-44": dress_card},
        "by_nm_id": {998877: dress_card},
        "by_chrt_id": {554433: dress_card},
    }

    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        with patch("app.agents.order_poller.WBClient", return_value=mock_wb_client):
            new_ids, new_payloads = poll_seller_orders(seller=seller, session=session)

            # 1. Verify returned payloads have kiz_required == True
            assert len(new_ids) == 1
            assert new_ids[0] == order_id
            assert len(new_payloads) == 1
            payload = new_payloads[0]
            assert payload["id"] == order_id
            assert payload["subject"] == "Платья"
            assert payload["kiz_required"] is True

            # 2. Verify Order model was persisted in DB with kiz_required=True and PENDING status
            saved_order = session.query(Order).filter(Order.id == order_id).first()
            assert saved_order is not None
            assert saved_order.kiz_required is True
            assert saved_order.kiz_status == KizStatus.PENDING
            assert saved_order.subject == "Платья"
            assert saved_order.brand == "SilkDream"
            assert saved_order.name == "Платье шелковое красное"


def test_order_poller_persists_non_marked_item_as_not_required(test_seller):
    """
    Simulate poll_seller_orders with non-marked item (stationery):
    - WB returns order with 'requiredMeta': []
    - Catalog resolves subject='Канцтовары', tnved='4820'
    - Verify Order.kiz_required is False
    - Verify Order.kiz_status is KizStatus.NOT_REQUIRED
    - Verify notification payload contains kiz_required: False
    """
    seller_id = test_seller
    order_id = random.randint(8000001, 9000000)

    raw_order = {
        "id": order_id,
        "createdAt": "2026-09-03T11:00:00Z",
        "price": 15000,
        "article": "PEN-BLUE-01",
        "chrtId": 665544,
        "nmId": 332211,
        "requiredMeta": [],
    }

    mock_wb_client = MagicMock()
    mock_wb_client.get_new_orders.return_value = [raw_order]
    mock_wb_client.get_orders_status.return_value = []
    mock_wb_client.get_cards_catalog.return_value = {
        332211: {
            "title": "Ручка шариковая синяя",
            "subjectName": "Канцтовары",
            "brand": "OfficePoint",
            "techSize": "",
            "wbSize": "",
            "tnved": "4820100000",
        }
    }

    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()

        with patch("app.agents.order_poller.WBClient", return_value=mock_wb_client):
            new_ids, new_payloads = poll_seller_orders(seller=seller, session=session)

            assert len(new_ids) == 1
            assert len(new_payloads) == 1
            payload = new_payloads[0]
            assert payload["kiz_required"] is False

            saved_order = session.query(Order).filter(Order.id == order_id).first()
            assert saved_order is not None
            assert saved_order.kiz_required is False
            assert saved_order.kiz_status == KizStatus.NOT_REQUIRED


def test_poll_all_sellers_dispatches_notification_with_kiz_required_true(test_seller):
    """
    End-to-end task test:
    Verify poll_all_sellers invokes notify_new_order.delay with kiz_required: True
    when order has requiredMeta: [] and clothing subject.
    """
    seller_id = test_seller
    order_id = random.randint(9000001, 9999999)

    from app.agents.order_poller import poll_all_sellers, _last_polled
    _last_polled.clear()

    with SyncSessionLocal() as session:
        session.query(Seller).filter(Seller.id != seller_id).update({"is_active": False})
        seller = session.query(Seller).filter(Seller.id == seller_id).first()
        seller.last_polled_at = None
        seller.is_active = True
        seller.polling_enabled = True
        seller.polling_interval_seconds = 0
        seller.notification_mode = "instant"
        session.commit()

        mock_wb_orders = [
            {
                "id": order_id,
                "article": "DRESS-02",
                "price": 350000,
                "requiredMeta": [],
                "chrtId": 771122,
                "nmId": 883344,
            }
        ]

        with patch("app.agents.order_poller.WBClient") as mock_wb_class, \
             patch("app.agents.notifier.notify_new_order.delay") as mock_notify_single, \
             patch("app.agents.order_poller.get_stickers.delay"):

            mock_client = MagicMock()
            mock_client.get_new_orders.return_value = mock_wb_orders
            dress_card_2 = {
                "title": "Платье летнее",
                "subjectName": "Платья",
                "brand": "Flora",
                "tnved": "6104420000",
            }
            mock_client.get_cards_catalog.return_value = {
                "by_vendor_code": {"DRESS-02": dress_card_2},
                "by_nm_id": {883344: dress_card_2},
                "by_chrt_id": {771122: dress_card_2},
            }
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
            assert payload["kiz_required"] is True
            assert payload["subject"] == "Платья"


# ---------------------------------------------------------------------------
# 7. Telegram Notification Templates: Exact Tag Formatting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_single_order_notification_template_kiz_tag_display():
    """Verify send_new_order_notification displays exact tags for True and False."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    # Case A: kiz_required is True -> must contain '🏷️ <b>КИЗ:</b> ⚠️ ТРЕБУЕТСЯ'
    await service.send_new_order_notification(
        chat_ids=["100200300"],
        order_id=555111,
        order_data={
            "name": "Куртка пуховая",
            "brand": "Nordic",
            "subject": "Куртки",
            "article": "JACKET-01",
            "price": 899000,
            "kiz_required": True,
        }
    )
    msg_a = captured[-1]
    assert "🏷️ <b>КИЗ:</b> ⚠️ ТРЕБУЕТСЯ" in msg_a
    assert "✅ не нужен" not in msg_a

    # Case B: kiz_required is False -> must contain '🏷️ <b>КИЗ:</b> ✅ не нужен'
    await service.send_new_order_notification(
        chat_ids=["100200300"],
        order_id=555222,
        order_data={
            "name": "Блокнот А5",
            "brand": "PaperCraft",
            "subject": "Канцтовары",
            "article": "NOTE-A5",
            "price": 25000,
            "kiz_required": False,
        }
    )
    msg_b = captured[-1]
    assert "🏷️ <b>КИЗ:</b> ✅ не нужен" in msg_b
    assert "⚠️ ТРЕБУЕТСЯ" not in msg_b


@pytest.mark.asyncio
async def test_telegram_batch_and_digest_kiz_count_display():
    """Verify send_batch_orders_notification and send_orders_digest display KIZ requirement summary."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    orders = [
        {"id": 1, "name": "Платье", "price": 300000, "kiz_required": True},
        {"id": 2, "name": "Ботинки", "price": 400000, "kiz_required": True},
        {"id": 3, "name": "Тетрадь", "price": 5000, "kiz_required": False},
    ]

    # Batch notification
    await service.send_batch_orders_notification(
        chat_ids=["100200300"],
        seller_id="seller-1",
        orders=orders,
    )
    batch_msg = captured[-1]
    assert "⚠️ <b>Требуют КИЗ:</b> 2 из 3" in batch_msg

    # Scheduled digest via send_orders_digest
    await service.send_orders_digest(
        chat_ids=["100200300"],
        seller_id="seller-1",
        pending_orders=orders,
        digest_time_str="14:00 Europe/Moscow",
    )
    digest_msg = captured[-1]
    assert "⚠️ <b>Требуют КИЗ:</b> 2 из 3" in digest_msg
    assert "14:00 Europe/Moscow" in digest_msg
