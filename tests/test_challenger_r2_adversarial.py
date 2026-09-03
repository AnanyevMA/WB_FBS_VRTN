"""
Challenger 2 Adversarial Verification Test Harness for Objective R2.

This test module empirically stress-tests:
1. is_kiz_required and _check_kiz_required:
   - Empty requiredMeta: [] with clothing categories
   - Empty requiredMeta: [] with perfume/cosmetics
   - Empty requiredMeta: [] with bed linen / textiles
   - Empty requiredMeta: [] with TN VED prefixes
   - Explicit requiredMeta: ["sgtin"], ["SGTIN"], ["kiz"], ["KIZ"], etc.
   - Non-marked items (Канцтовары, Ручка шариковая, Ноутбук, TN VED "8517120000", requiredMeta: [])
   - Malicious, corrupt, None, and unexpected types (int, list, dict, bool, special chars)
   - Case-insensitivity and punctuation resilience
2. Telegram notification formatting:
   - Order with kiz_required=True -> message MUST contain 🏷️ <b>КИЗ:</b> ⚠️ ТРЕБУЕТСЯ
   - Order with kiz_required=False -> message MUST contain 🏷️ <b>КИЗ:</b> ✅ не нужен
   - Truthy / falsy variants (None, 1, 0, "True", empty dict)
   - Batch and digest notifications correctly tallying KIZ requirements
3. Order Poller integration:
   - End-to-end flow from raw order with requiredMeta: [] through database persistence and payload creation
"""
import sys
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

# Ensure aiogram stub if not installed in environment
def _ensure_aiogram_mock():
    try:
        import aiogram
        return
    except ImportError:
        pass

    if "aiogram" not in sys.modules:
        aiogram_mock = MagicMock()
        aiogram_mock.__path__ = []
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

_ensure_aiogram_mock()

from app.database import init_db
from app.services.wb_client import is_kiz_required, WBClient
from app.agents.order_poller import (
    SyncSessionLocal,
    _check_kiz_required,
    _resolve_order_metadata,
    poll_seller_orders,
)
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.services.encryption import encrypt
from app.services.telegram_service import TelegramService


@pytest.fixture(autouse=True)
def setup_db():
    import asyncio
    asyncio.run(init_db())
    yield


# ===========================================================================
# 1. EMPIRICAL VERIFICATION: CLOTHING CATEGORIES (requiredMeta: []) -> True
# ===========================================================================

@pytest.mark.parametrize("category", [
    "Платья",
    "Брюки",
    "Куртки",
    "Ботинки",
    "Пальто",
    "Джинсы",
    "ПЛАТЬЯ",
    "брюки",
    "  Куртки  ",
    "Платье летнее",
    "Брюки классические",
    "Куртка зимняя",
    "Ботинки зимние",
    "Пальто демисезонное",
    "Джинсы прямого кроя",
])
def test_kiz_required_clothing_categories_with_empty_meta(category):
    """Empty requiredMeta: [] with clothing categories MUST return True."""
    # Test via is_kiz_required
    assert is_kiz_required(subject=category, order_raw={"requiredMeta": []}) is True
    # Test via _check_kiz_required
    assert _check_kiz_required(order_raw={"requiredMeta": []}, subject=category) is True
    # Test when category is inside order_raw["subject"]
    assert is_kiz_required(order_raw={"requiredMeta": [], "subject": category}) is True
    # Test when category is in order_raw["name"]
    assert is_kiz_required(order_raw={"requiredMeta": [], "name": category}) is True


# ===========================================================================
# 2. EMPIRICAL VERIFICATION: PERFUME / COSMETICS (requiredMeta: []) -> True
# ===========================================================================

@pytest.mark.parametrize("perfume_item", [
    "Духи",
    "Туалетная вода",
    "Парфюмерия",
    "ДУХИ",
    "туалетная вода",
    "Парфюмерная вода 50мл",
    "Духи женские стойкие",
    "Французская парфюмерия",
])
def test_kiz_required_perfume_cosmetics_with_empty_meta(perfume_item):
    """Empty requiredMeta: [] with perfume/cosmetics MUST return True."""
    assert is_kiz_required(subject=perfume_item, order_raw={"requiredMeta": []}) is True
    assert _check_kiz_required(order_raw={"requiredMeta": []}, subject=perfume_item) is True
    assert is_kiz_required(order_raw={"requiredMeta": [], "subject": perfume_item}) is True
    assert is_kiz_required(order_raw={"requiredMeta": [], "name": perfume_item}) is True


# ===========================================================================
# 3. EMPIRICAL VERIFICATION: BED LINEN / TEXTILES (requiredMeta: []) -> True
# ===========================================================================

@pytest.mark.parametrize("textile_item", [
    "Постельное белье",
    "Полотенце махровое",
    "ПОСТЕЛЬНОЕ БЕЛЬЕ",
    "полотенце махровое",
    "Комплект постельного белья сатин",
    "Полотенце кухонное",
    "Текстиль для дома",
])
def test_kiz_required_textiles_with_empty_meta(textile_item):
    """Empty requiredMeta: [] with bed linen / textiles MUST return True."""
    assert is_kiz_required(subject=textile_item, order_raw={"requiredMeta": []}) is True
    assert _check_kiz_required(order_raw={"requiredMeta": []}, subject=textile_item) is True
    assert is_kiz_required(order_raw={"requiredMeta": [], "subject": textile_item}) is True
    assert is_kiz_required(order_raw={"requiredMeta": [], "name": textile_item}) is True


# ===========================================================================
# 4. EMPIRICAL VERIFICATION: TN VED PREFIXES (requiredMeta: []) -> True
# ===========================================================================

@pytest.mark.parametrize("tnved_code", [
    "6109100000",   # 61: knitted clothing
    "6203423500",   # 62: non-knitted clothing
    "6403599900",   # 64: footwear
    "3303001000",   # 3303: perfumes & toilet waters
    "4203100000",   # 4203: leather apparel
    "4011100000",   # 4011: pneumatic tires
    "6302210000",   # 6302: bed linen
    "6505009000",   # 6505: hats/headgear
    "9004100000",   # 9004: spectacles/sunglasses
    6109100000,     # integer input
    6203423500,     # integer input
    " 6109100000 ", # leading/trailing spaces
    "61.09.10.00.00", # dotted formatting
    "6403-59-9900", # dashed formatting
])
def test_kiz_required_tnved_prefixes_with_empty_meta(tnved_code):
    """Empty requiredMeta: [] with marked TN VED prefixes MUST return True."""
    # With non-marked subject
    assert is_kiz_required(subject="Товар без категории", tnved=tnved_code, order_raw={"requiredMeta": []}) is True
    assert _check_kiz_required(order_raw={"requiredMeta": []}, subject="Без категории", tnved=str(tnved_code)) is True
    # With tnved passed inside order_raw
    assert is_kiz_required(order_raw={"requiredMeta": [], "tnved": tnved_code}) is True


# ===========================================================================
# 5. EMPIRICAL VERIFICATION: EXPLICIT requiredMeta -> True
# ===========================================================================

@pytest.mark.parametrize("meta_val", [
    ["sgtin"],
    ["SGTIN"],
    ["kiz"],
    ["KIZ"],
    ["Sgtin"],
    ["KiZ"],
    ["unknown", "sgtin"],
    ["other_flag", "KIZ", "something"],
    "sgtin",
    "SGTIN",
    "kiz",
    "KIZ",
    "contains sgtin inside text",
])
def test_kiz_required_explicit_required_meta_variations(meta_val):
    """Explicit requiredMeta with sgtin or kiz MUST return True regardless of subject/tnved."""
    # Even when category is non-marked stationery or electronics
    assert is_kiz_required(subject="Канцтовары", order_raw={"requiredMeta": meta_val}) is True
    assert _check_kiz_required(order_raw={"requiredMeta": meta_val}, subject="Канцтовары") is True
    assert is_kiz_required(subject=None, tnved=None, order_raw={"requiredMeta": meta_val}) is True


# ===========================================================================
# 6. EMPIRICAL VERIFICATION: NON-MARKED GOODS -> False
# ===========================================================================

@pytest.mark.parametrize("subject,tnved", [
    ("Канцтовары", "4820100000"),
    ("Ручка шариковая", "9608101000"),
    ("Ноутбук", "8517120000"),
    ("Смартфон", "8517120000"),
    ("Тетрадь школьная", "4820200000"),
    ("Кабель USB Type-C", "8544429007"),
    ("Чехол для телефона", "3926909709"),
    ("Игрушка пластиковая", "9503003500"),
    ("Книга печатная", "4901990000"),
    ("Посуда стеклянная", "7013379900"),
])
def test_kiz_required_non_marked_items_return_false(subject, tnved):
    """Non-marked items with empty requiredMeta: [] MUST return False."""
    assert is_kiz_required(subject=subject, tnved=tnved, order_raw={"requiredMeta": []}) is False
    assert _check_kiz_required(order_raw={"requiredMeta": []}, subject=subject, tnved=tnved) is False
    assert is_kiz_required(subject=subject, order_raw={"requiredMeta": []}) is False
    assert is_kiz_required(order_raw={"requiredMeta": [], "subject": subject, "tnved": tnved}) is False


# ===========================================================================
# 7. ADVERSARIAL STRESS: MALICIOUS, CORRUPT & UNEXPECTED TYPES
# ===========================================================================

@pytest.mark.parametrize("corrupt_raw", [
    None,
    {},
    {"requiredMeta": None},
    {"requiredMeta": []},
    {"requiredMeta": [None, 12345, False, {}]},
    {"requiredMeta": 12345},
    {"requiredMeta": True},
    {"requiredMeta": {"key": "val"}},
    {"requiredMeta": [b"bytes", object()]},
    "not a dictionary",
    12345678,
    [1, 2, 3],
])
def test_is_kiz_required_adversarial_types_no_crash(corrupt_raw):
    """Function MUST gracefully handle corrupt, non-dict, None, or weird order_raw without raising."""
    result = is_kiz_required(order_raw=corrupt_raw)
    assert isinstance(result, bool)

    result_poller = _check_kiz_required(order_raw=corrupt_raw)
    assert isinstance(result_poller, bool)


@pytest.mark.parametrize("bad_tnved", [
    None,
    "",
    "   ",
    12345,
    9999999999,
    ["6109100000"],
    {"code": "6109100000"},
    object(),
])
def test_is_kiz_required_adversarial_tnved_types_no_crash(bad_tnved):
    """Function MUST handle odd TN VED data types without crashing."""
    result = is_kiz_required(tnved=bad_tnved, order_raw={"requiredMeta": []})
    assert isinstance(result, bool)


@pytest.mark.parametrize("bad_subject", [
    None,
    "",
    "   ",
    12345,
    ["Платья"],
    {"subject": "Платья"},
    False,
])
def test_is_kiz_required_adversarial_subject_types_no_crash(bad_subject):
    """Function MUST handle odd subject data types without crashing."""
    result = is_kiz_required(subject=bad_subject, order_raw={"requiredMeta": []})
    assert isinstance(result, bool)


def test_is_kiz_required_priority_and_fallbacks():
    """Verify fallback from subject to name in order_raw and priority rules."""
    # When subject is None, but order_raw contains name with marked keyword
    order_raw = {"name": "Пальто шерстяное", "requiredMeta": []}
    assert is_kiz_required(subject=None, order_raw=order_raw) is True

    # When subject is non-marked but order_raw has marked TN VED
    assert is_kiz_required(subject="Непонятный товар", order_raw={"requiredMeta": [], "tnved": "6403599900"}) is True

    # When subject is non-marked, TN VED is non-marked, but requiredMeta has sgtin
    assert is_kiz_required(
        subject="Канцтовары",
        tnved="4820100000",
        order_raw={"requiredMeta": ["sgtin"]}
    ) is True


# ===========================================================================
# 8. EMPIRICAL VERIFICATION: TELEGRAM NOTIFICATION TEMPLATE FORMATTING
# ===========================================================================

@pytest.mark.asyncio
async def test_telegram_single_order_notification_kiz_required_true():
    """Order with kiz_required=True -> message MUST contain 🏷️ <b>КИЗ:</b> ⚠️ ТРЕБУЕТСЯ."""
    service = TelegramService(bot_token="123456:FAKE_TOKEN_TEST")
    captured = []
    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True
    service._broadcast = mock_broadcast

    order_data = {
        "article": "DR-101",
        "name": "Платье шелковое",
        "brand": "FashionBrand",
        "subject": "Платья",
        "price": 250000,
        "kiz_required": True,
    }

    res = await service.send_new_order_notification(chat_ids=["999888"], order_id=777001, order_data=order_data)
    assert res is True
    assert len(captured) == 1
    assert "🏷️ <b>КИЗ:</b> ⚠️ ТРЕБУЕТСЯ" in captured[0]
    assert "✅ не нужен" not in captured[0]


@pytest.mark.asyncio
async def test_telegram_single_order_notification_kiz_required_false():
    """Order with kiz_required=False -> message MUST contain 🏷️ <b>КИЗ:</b> ✅ не нужен."""
    service = TelegramService(bot_token="123456:FAKE_TOKEN_TEST")
    captured = []
    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True
    service._broadcast = mock_broadcast

    order_data = {
        "article": "PEN-55",
        "name": "Ручка шариковая синяя",
        "brand": "OfficePro",
        "subject": "Канцтовары",
        "price": 15000,
        "kiz_required": False,
    }

    res = await service.send_new_order_notification(chat_ids=["999888"], order_id=777002, order_data=order_data)
    assert res is True
    assert len(captured) == 1
    assert "🏷️ <b>КИЗ:</b> ✅ не нужен" in captured[0]
    assert "⚠️ ТРЕБУЕТСЯ" not in captured[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_kiz", [
    None,
    False,
    0,
    "",
])
async def test_telegram_single_order_notification_falsy_kiz_handled(falsy_kiz):
    """Any falsy kiz_required value MUST safely produce '✅ не нужен'."""
    service = TelegramService(bot_token="123456:FAKE_TOKEN_TEST")
    captured = []
    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True
    service._broadcast = mock_broadcast

    order_data = {
        "name": "Тетрадь 48л",
        "kiz_required": falsy_kiz,
    }
    await service.send_new_order_notification(chat_ids=["999888"], order_id=777003, order_data=order_data)
    assert len(captured) == 1
    assert "🏷️ <b>КИЗ:</b> ✅ не нужен" in captured[0]


@pytest.mark.asyncio
async def test_telegram_single_order_notification_missing_kiz_handled():
    """Order data dictionary missing kiz_required entirely MUST default to '✅ не нужен'."""
    service = TelegramService(bot_token="123456:FAKE_TOKEN_TEST")
    captured = []
    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True
    service._broadcast = mock_broadcast

    order_data = {"name": "Папка для бумаг"}
    await service.send_new_order_notification(chat_ids=["999888"], order_id=777004, order_data=order_data)
    assert len(captured) == 1
    assert "🏷️ <b>КИЗ:</b> ✅ не нужен" in captured[0]


@pytest.mark.asyncio
async def test_telegram_batch_notification_kiz_tally():
    """Batch notification correctly identifies items requiring KIZ and tags them."""
    service = TelegramService(bot_token="123456:FAKE_TOKEN_TEST")
    captured = []
    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True
    service._broadcast = mock_broadcast

    orders_list = [
        {"id": 101, "name": "Платье шелк", "kiz_required": True, "price": 250000},
        {"id": 102, "name": "Брюки лен", "kiz_required": True, "price": 180000},
        {"id": 103, "name": "Ручка шариковая", "kiz_required": False, "price": 10000},
    ]

    await service.send_batch_orders_notification(chat_ids=["999888"], seller_id="seller_123", orders=orders_list)
    assert len(captured) == 1
    assert "Требуют КИЗ:</b> 2 из 3" in captured[0]
    assert "⚠️<b>КИЗ</b>" in captured[0]


# ===========================================================================
# 9. INTEGRATION VERIFICATION: ORDER POLLER PERSISTENCE WITH EMPTY requiredMeta
# ===========================================================================

def test_order_poller_e2e_clothing_empty_meta_persistence():
    """
    Simulate WB FBS returning order with requiredMeta: [] and clothing category.
    Verify:
    1. Order.kiz_required is persisted as True
    2. Order.kiz_status is persisted as KizStatus.PENDING
    3. order_payload['kiz_required'] is True
    """
    seller_id = str(uuid.uuid4())
    import random
    wb_order_id = random.randint(100_000_000, 999_999_999)

    with SyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Test Seller Clothing",
            wb_supplier_id="WB-CLOTH-01",
            cz_inn="7700998877",
            wb_api_token_encrypted=encrypt("valid_token"),
            telegram_bot_token_encrypted=encrypt("123:TOKEN"),
            telegram_chat_ids=["100200"],
            is_active=True,
            polling_enabled=True,
            notification_mode="instant",
        )
        session.add(seller)
        session.commit()

    raw_orders = [{
        "id": wb_order_id,
        "createdAt": "2026-09-03T10:00:00Z",
        "price": 320000,
        "requiredMeta": [],
        "article": "DRESS-001",
        "nmId": 99887711,
        "chrtId": 445566,
    }]

    mock_client = MagicMock()
    mock_client.get_new_orders.return_value = raw_orders
    # Content API returns clothing category "Платья"
    mock_client.get_cards_catalog.return_value = {
        99887711: {
            "title": "Платье летнее хлопковое",
            "name": "Платье летнее хлопковое",
            "brand": "SummerVibes",
            "subjectName": "Платья",
            "subject": "Платья",
            "tnved": "6204420000",
        }
    }

    with patch("app.agents.order_poller.WBClient", return_value=mock_client), \
         patch("app.agents.order_poller.notify_new_order.delay") as mock_notify, \
         patch("app.agents.order_poller.get_order_sticker.delay"):

        with SyncSessionLocal() as session:
            seller = session.query(Seller).filter(Seller.id == seller_id).first()
            processed_ids, payloads = poll_seller_orders(seller, session)

    assert wb_order_id in processed_ids

    # Check persistence in DB
    with SyncSessionLocal() as session:
        saved_order = session.query(Order).filter(Order.id == wb_order_id).first()
        assert saved_order is not None
        assert saved_order.kiz_required is True
        assert saved_order.kiz_status == KizStatus.PENDING
        assert saved_order.subject == "Платья"
        assert saved_order.name == "Платье летнее хлопковое"

    # Check payload
    assert len(payloads) == 1
    assert payloads[0]["kiz_required"] is True
    assert payloads[0]["subject"] == "Платья"
    assert payloads[0]["name"] == "Платье летнее хлопковое"


def test_order_poller_e2e_stationery_empty_meta_persistence():
    """
    Simulate WB FBS returning order with requiredMeta: [] and stationery category.
    Verify:
    1. Order.kiz_required is persisted as False
    2. Order.kiz_status is persisted as KizStatus.NOT_REQUIRED
    3. order_payload['kiz_required'] is False
    """
    seller_id = str(uuid.uuid4())
    import random
    wb_order_id = random.randint(100_000_000, 999_999_999)

    with SyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Test Seller Stationery",
            wb_supplier_id="WB-STAT-01",
            cz_inn="7700998877",
            wb_api_token_encrypted=encrypt("valid_token"),
            telegram_bot_token_encrypted=encrypt("123:TOKEN"),
            telegram_chat_ids=["100200"],
            is_active=True,
            polling_enabled=True,
            notification_mode="instant",
        )
        session.add(seller)
        session.commit()

    raw_orders = [{
        "id": wb_order_id,
        "createdAt": "2026-09-03T10:00:00Z",
        "price": 12000,
        "requiredMeta": [],
        "article": "PEN-001",
        "nmId": 99887722,
        "chrtId": 445577,
    }]

    mock_client = MagicMock()
    mock_client.get_new_orders.return_value = raw_orders
    # Content API returns stationery category "Канцтовары"
    mock_client.get_cards_catalog.return_value = {
        99887722: {
            "title": "Ручка шариковая синяя",
            "name": "Ручка шариковая синяя",
            "brand": "OfficeHub",
            "subjectName": "Канцтовары",
            "subject": "Канцтовары",
            "tnved": "9608101000",
        }
    }

    with patch("app.agents.order_poller.WBClient", return_value=mock_client), \
         patch("app.agents.order_poller.notify_new_order.delay") as mock_notify, \
         patch("app.agents.order_poller.get_order_sticker.delay"):

        with SyncSessionLocal() as session:
            seller = session.query(Seller).filter(Seller.id == seller_id).first()
            processed_ids, payloads = poll_seller_orders(seller, session)

    assert wb_order_id in processed_ids

    # Check persistence in DB
    with SyncSessionLocal() as session:
        saved_order = session.query(Order).filter(Order.id == wb_order_id).first()
        assert saved_order is not None
        assert saved_order.kiz_required is False
        assert saved_order.kiz_status == KizStatus.NOT_REQUIRED
        assert saved_order.subject == "Канцтовары"

    # Check payload
    assert len(payloads) == 1
    assert payloads[0]["kiz_required"] is False
    assert payloads[0]["subject"] == "Канцтовары"
