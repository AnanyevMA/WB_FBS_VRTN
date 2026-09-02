import pytest
import sys
import uuid
import random
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

from app.database import init_db
from app.agents.order_poller import SyncSessionLocal
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
            name=f"Seller-TG-{seller_id[:6]}",
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
# TelegramService Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_send_new_order_notification_with_full_metadata():
    """Verify send_new_order_notification formats product details (name, brand, subject, article, price, KIZ)."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured_messages = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured_messages.append({"chat_ids": chat_ids, "text": text, "keyboard": keyboard})
        return True

    service._broadcast = mock_broadcast

    order_id = 987654321
    order_data = {
        "id": order_id,
        "name": "Футболка оверсайз плотная",
        "brand": "CottonCraft",
        "subject": "Футболки",
        "article": "TSHIRT-BLK-L",
        "price": 199000,  # 1990 rubles in kopecks
        "kiz_required": True,
        "wb_created_at": "2026-09-02T12:00:00+00:00",
    }

    res = await service.send_new_order_notification(
        chat_ids=["100200300"],
        order_id=order_id,
        order_data=order_data,
    )

    assert res is True
    assert len(captured_messages) == 1
    msg = captured_messages[0]["text"]

    assert f"#{order_id}" in msg
    assert "Футболка оверсайз плотная" in msg
    assert "CottonCraft" in msg
    assert "Футболки" in msg
    assert "TSHIRT-BLK-L" in msg
    assert "1990 ₽" in msg
    assert "⚠️ ТРЕБУЕТСЯ" in msg


@pytest.mark.asyncio
async def test_telegram_send_new_order_notification_fallback_dashes_when_empty():
    """Verify send_new_order_notification gracefully falls back to dashes when metadata is missing."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured_messages = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured_messages.append({"chat_ids": chat_ids, "text": text, "keyboard": keyboard})
        return True

    service._broadcast = mock_broadcast

    order_id = 1122334455
    res = await service.send_new_order_notification(
        chat_ids=["100200300"],
        order_id=order_id,
        order_data={},
    )

    assert res is True
    assert len(captured_messages) == 1
    msg = captured_messages[0]["text"]

    assert f"#{order_id}" in msg
    assert "📦 <b>Товар:</b> —" in msg
    assert "🔖 <b>Бренд:</b> —" in msg
    assert "📁 <b>Категория:</b> —" in msg
    assert "📝 <b>Артикул:</b> —" in msg
    assert "🏷️ <b>КИЗ:</b> ✅ не нужен" in msg
    assert "💰 <b>Цена:</b> 0 ₽" in msg


@pytest.mark.asyncio
async def test_telegram_send_batch_orders_notification():
    """Verify send_batch_orders_notification correctly formats multiple items, total price, and KIZ summary."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured_messages = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured_messages.append({"chat_ids": chat_ids, "text": text, "keyboard": keyboard})
        return True

    service._broadcast = mock_broadcast

    orders = [
        {"id": 101, "name": "Кроссовки беговые 42", "price": 450000, "kiz_required": True},
        {"id": 102, "name": "Носки спортивные 3 пары", "price": 50000, "kiz_required": False},
        {"id": 103, "name": "Футболка белая XL", "price": 120000, "kiz_required": False},
    ]

    res = await service.send_batch_orders_notification(
        chat_ids=["100200300"],
        seller_id="seller-123",
        orders=orders,
    )

    assert res is True
    assert len(captured_messages) == 1
    msg = captured_messages[0]["text"]

    assert "НОВЫЕ ЗАКАЗЫ FBS — 3 шт." in msg
    assert "#101" in msg and "Кроссовки беговые 42" in msg
    assert "#102" in msg and "Носки спортивные 3 пары" in msg
    assert "#103" in msg and "Футболка белая XL" in msg
    assert "6200 ₽" in msg  # 4500 + 500 + 1200 = 6200
    assert "Требуют КИЗ:</b> 1 из 3" in msg


# ---------------------------------------------------------------------------
# Notifier Celery Task Tests
# ---------------------------------------------------------------------------

def test_notify_new_order_task_with_payload(test_seller):
    """Verify notify_new_order Celery task passes full order_data to TelegramService."""
    seller_id = test_seller
    order_id = random.randint(3000000, 4000000)

    order_payload = {
        "id": order_id,
        "name": "Джинсы прямые синие 32",
        "brand": "DenimStyle",
        "subject": "Джинсы",
        "article": "JEANS-BLU-32",
        "price": 320000,
        "kiz_required": True,
        "wb_created_at": "2026-09-02T12:00:00+00:00",
    }

    with patch("app.services.telegram_service.TelegramService.send_new_order_notification", new_callable=AsyncMock) as mock_send, \
         patch("app.services.telegram_service.TelegramService.close", new_callable=AsyncMock):

        mock_send.return_value = True

        from app.agents.notifier import notify_new_order
        notify_new_order(seller_id=seller_id, order_id=order_id, order_data=order_payload)

        mock_send.assert_called_once()
        chat_ids_arg, order_id_arg, data_arg = mock_send.call_args[0]
        assert chat_ids_arg == ["100200300"]
        assert order_id_arg == order_id
        assert data_arg["name"] == "Джинсы прямые синие 32"
        assert data_arg["brand"] == "DenimStyle"
        assert data_arg["subject"] == "Джинсы"
        assert data_arg["article"] == "JEANS-BLU-32"


def test_notify_new_order_task_fallback_to_db_record(test_seller):
    """Verify notify_new_order Celery task queries DB record when order_data is None."""
    seller_id = test_seller
    order_id = random.randint(4000001, 5000000)

    with SyncSessionLocal() as session:
        order = Order(
            id=order_id,
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            chrt_id=778899,
            nm_id=112233,
            article="COAT-WOOL-48",
            name="Пальто шерстяное серое 48",
            brand="WarmElegance",
            subject="Пальто",
            tech_size="48",
            wb_size="48",
            price=Decimal("8900.00"),
            kiz_required=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        session.commit()

    with patch("app.services.telegram_service.TelegramService.send_new_order_notification", new_callable=AsyncMock) as mock_send, \
         patch("app.services.telegram_service.TelegramService.close", new_callable=AsyncMock):

        mock_send.return_value = True

        from app.agents.notifier import notify_new_order
        notify_new_order(seller_id=seller_id, order_id=order_id, order_data=None)

        mock_send.assert_called_once()
        chat_ids_arg, order_id_arg, data_arg = mock_send.call_args[0]
        assert chat_ids_arg == ["100200300"]
        assert order_id_arg == order_id
        assert data_arg["name"] == "Пальто шерстяное серое 48"
        assert data_arg["brand"] == "WarmElegance"
        assert data_arg["subject"] == "Пальто"
        assert data_arg["article"] == "COAT-WOOL-48"
        assert data_arg["price"] == 890000
        assert data_arg["kiz_required"] is True


def test_notify_batch_orders_task(test_seller):
    """Verify notify_batch_orders Celery task calls send_batch_orders_notification."""
    seller_id = test_seller

    payloads = [
        {"id": 501, "name": "Куртка зимняя", "price": 750000, "kiz_required": True},
        {"id": 502, "name": "Перчатки кожаные", "price": 180000, "kiz_required": True},
    ]

    with patch("app.services.telegram_service.TelegramService.send_batch_orders_notification", new_callable=AsyncMock) as mock_send, \
         patch("app.services.telegram_service.TelegramService.close", new_callable=AsyncMock):

        mock_send.return_value = True

        from app.agents.notifier import notify_batch_orders
        notify_batch_orders(seller_id=seller_id, orders_payload=payloads)

        mock_send.assert_called_once_with(
            chat_ids=["100200300"],
            seller_id=seller_id,
            orders=payloads,
        )


@pytest.mark.asyncio
async def test_telegram_send_new_order_html_escaping_and_null_coalescing():
    """Verify HTML special characters are properly escaped and explicit None fields coalesce cleanly."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured_messages = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured_messages.append({"chat_ids": chat_ids, "text": text, "keyboard": keyboard})
        return True

    service._broadcast = mock_broadcast

    order_data = {
        "id": 123456,
        "name": "Футболка <Черная> & Белая",
        "brand": "H&M <Fashion>",
        "subject": 'Одежда "Премиум" & Люкс',
        "article": "ART<001>&XYZ",
        "price": None,
        "kiz_required": None,
    }

    res = await service.send_new_order_notification(
        chat_ids=["100200300"],
        order_id=123456,
        order_data=order_data,
    )

    assert res is True
    assert len(captured_messages) == 1
    msg = captured_messages[0]["text"]

    assert "&lt;Черная&gt; &amp; Белая" in msg
    assert "H&amp;M &lt;Fashion&gt;" in msg
    assert "Одежда &quot;Премиум&quot; &amp; Люкс" in msg
    assert "ART&lt;001&gt;&amp;XYZ" in msg
    assert "0 ₽" in msg
    assert "✅ не нужен" in msg


@pytest.mark.asyncio
async def test_telegram_send_batch_orders_with_null_prices_and_html():
    """Verify batch order notification handles None prices without TypeError and escapes HTML."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured_messages = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured_messages.append({"chat_ids": chat_ids, "text": text, "keyboard": keyboard})
        return True

    service._broadcast = mock_broadcast

    orders = [
        {"id": 801, "name": "Кроссовки <Nike & Adidas>", "price": None, "kiz_required": True},
        {"id": 802, "name": None, "article": "ART<TEST>", "price": 150000, "kiz_required": False},
        {"id": 803, "name": "Кепка", "price": 50000, "kiz_required": None},
    ]

    res = await service.send_batch_orders_notification(
        chat_ids=["100200300"],
        seller_id="seller-xyz",
        orders=orders,
    )

    assert res is True
    assert len(captured_messages) == 1
    msg = captured_messages[0]["text"]

    assert "НОВЫЕ ЗАКАЗЫ FBS — 3 шт." in msg
    assert "&lt;Nike &amp; Adidas&gt;" in msg
    assert "ART&lt;TEST&gt;" in msg
    assert "2000 ₽" in msg  # 0 + 1500 + 500 = 2000
    assert "Требуют КИЗ:</b> 1 из 3" in msg