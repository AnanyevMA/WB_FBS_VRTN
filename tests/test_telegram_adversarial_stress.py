"""
Empirical Adversarial Stress Test Suite for TelegramService.
Tests HTML escaping, entity injection, explicit None fields, zero/None/abnormal prices,
malformed payloads, and Telegram parse mode compliance.
"""
import html
import pytest
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from decimal import Decimal
from unittest.mock import MagicMock

def _stub_aiogram():
    """Stub out aiogram so tests run reliably offline."""
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


class TelegramHTMLValidator(HTMLParser):
    """Strict HTML Parser validating Telegram parse-mode tags and balanced structure."""
    ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "code", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.disallowed_found = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in self.ALLOWED_TAGS:
            self.disallowed_found.append(tag)
        self.stack.append(tag.lower())

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag.lower():
            self.stack.pop()
        else:
            self.stack.append(f"unmatched_close_{tag}")


def _verify_telegram_html_validity(text: str):
    """
    Empirical check: Verify that message text forms valid XML/HTML entities
    and does not contain unescaped raw '<' or '>' that cause Telegram 400 Parse Error.
    """
    validator = TelegramHTMLValidator()
    validator.feed(text)
    assert not validator.disallowed_found, f"Found disallowed HTML tags for Telegram parse mode: {validator.disallowed_found}\nText:\n{text}"
    assert not validator.stack, f"Unbalanced HTML tags in message: {validator.stack}\nText:\n{text}"

    # Also test standard XML parsing for clean well-formedness
    clean_xml_text = "".join(ch for ch in text if ch >= ' ' or ch in '\n\r\t')
    wrapped_xml = f"<root>{clean_xml_text}</root>"
    try:
        ET.fromstring(wrapped_xml)
    except ET.ParseError as e:
        pytest.fail(f"Generated Telegram message failed XML validation: {e}\nMessage content:\n{text}")


# ---------------------------------------------------------------------------
# 1. ADVERSARIAL HTML & XSS STRESS TESTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_new_order_adversarial_html_xss_injection():
    """Verify send_new_order_notification escapes all dangerous HTML/XSS and unescaped brackets."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    dangerous_payloads = [
        "<script>alert('xss')</script>",
        "<b>Injected Bold</b>",
        "<img src=x onerror=alert(1)>",
        "<a href='http://evil.com'>Click me</a>",
        "A < B & C > D \"quotes\" 'single'",
        "Broken <tag without closing bracket",
        "Unbalanced </b></i></code></pre>",
        "<<<Nested <<< Brackets >>> >>",
        "Unicode: \u200e\u200f \U0001f4a9 <tag>",
    ]

    for i, attack in enumerate(dangerous_payloads):
        order_data = {
            "id": 1000 + i,
            "name": attack,
            "brand": attack,
            "subject": attack,
            "article": attack,
            "price": 150000,
            "kiz_required": True,
        }

        res = await service.send_new_order_notification(
            chat_ids=["100200300"],
            order_id=1000 + i,
            order_data=order_data,
        )

        assert res is True
        msg = captured[-1]

        # Check XML/HTML parseability and tag balancing
        _verify_telegram_html_validity(msg)


@pytest.mark.asyncio
async def test_all_methods_adversarial_html_escaping():
    """Verify that every single method in TelegramService escapes dynamic string parameters."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    xss = "<script>alert('hack')</script> & <test> \"quote\""

    # 1. send_kiz_required_alert
    await service.send_kiz_required_alert(
        chat_ids=["123"],
        order_id=101,
        order_name=xss,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]

    # 2. send_cz_withdrawal_status (success)
    await service.send_cz_withdrawal_status(
        chat_ids=["123"],
        order_id=102,
        success=True,
        doc_id=xss,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]

    # 3. send_cz_withdrawal_status (failure)
    await service.send_cz_withdrawal_status(
        chat_ids=["123"],
        order_id=103,
        success=False,
        error=xss,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]

    # 4. send_supply_delivered
    await service.send_supply_delivered(
        chat_ids=["123"],
        supply_id=xss,
        orders_count=5,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]

    # 5. send_error_alert
    await service.send_error_alert(
        chat_ids=["123"],
        agent=xss,
        message=xss,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]

    # 6. send_archive_reminder
    await service.send_archive_reminder(
        chat_ids=["123"],
        seller_name=xss,
        days_since_last=3,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "&lt;script&gt;" in captured[-1]


# ---------------------------------------------------------------------------
# 2. EXPLICIT NONE KEYS & EMPTY PAYLOADS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_none_keys_in_all_fields():
    """Verify that dictionaries with explicit None values do not crash or render 'None'."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    # send_new_order_notification with all None
    none_order_data = {
        "id": None,
        "name": None,
        "brand": None,
        "subject": None,
        "article": None,
        "price": None,
        "kiz_required": None,
        "wb_created_at": None,
    }

    res = await service.send_new_order_notification(
        chat_ids=["123"],
        order_id=999,
        order_data=none_order_data,
    )
    assert res is True
    msg = captured[-1]
    _verify_telegram_html_validity(msg)
    assert "None" not in msg
    assert "—" in msg
    assert "0 ₽" in msg

    # send_new_order_notification with order_data=None
    res = await service.send_new_order_notification(
        chat_ids=["123"],
        order_id=999,
        order_data=None,
    )
    assert res is True
    _verify_telegram_html_validity(captured[-1])

    # send_kiz_required_alert with order_name=None
    await service.send_kiz_required_alert(
        chat_ids=["123"],
        order_id=999,
        order_name=None,
    )
    _verify_telegram_html_validity(captured[-1])
    assert "(—)" in captured[-1]

    # send_cz_withdrawal_status with doc_id=None, error=None
    await service.send_cz_withdrawal_status(chat_ids=["123"], order_id=999, success=True, doc_id=None)
    _verify_telegram_html_validity(captured[-1])
    await service.send_cz_withdrawal_status(chat_ids=["123"], order_id=999, success=False, error=None)
    _verify_telegram_html_validity(captured[-1])

    # send_supply_delivered with supply_id=None
    await service.send_supply_delivered(chat_ids=["123"], supply_id=None, orders_count=0)
    _verify_telegram_html_validity(captured[-1])

    # send_error_alert with agent=None, message=None
    await service.send_error_alert(chat_ids=["123"], agent=None, message=None)
    _verify_telegram_html_validity(captured[-1])

    # send_archive_reminder with seller_name=None, days_since_last=None
    await service.send_archive_reminder(chat_ids=["123"], seller_name=None, days_since_last=None)
    _verify_telegram_html_validity(captured[-1])


# ---------------------------------------------------------------------------
# 3. ZERO, NONE, AND NON-STANDARD PRICES
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_handling_variations():
    """Verify price formatting across integer kopecks, zero, None, floats, strings, and Decimals."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    test_cases = [
        (0, "0 ₽"),
        (None, "0 ₽"),
        (100, "1 ₽"),
        (149000, "1490 ₽"),
        (99, "1 ₽"),  # 99 / 100 = 0.99 -> 1 ₽
        ("1500", "1500 ₽"),
        (Decimal("250.00"), "250.00 ₽"),
    ]

    for price_val, expected_str in test_cases:
        captured.clear()
        res = await service.send_new_order_notification(
            chat_ids=["123"],
            order_id=1234,
            order_data={"price": price_val, "name": "Item"},
        )
        assert res is True
        msg = captured[-1]
        _verify_telegram_html_validity(msg)
        assert expected_str in msg


# ---------------------------------------------------------------------------
# 4. BATCH NOTIFICATIONS & MORNING DIGEST STRESS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_batch_orders_stress_100_items():
    """Verify batch notification handles large lists (>10 items), None prices, missing keys, and HTML."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    orders = []
    for i in range(100):
        orders.append({
            "id": 1000 + i if i % 2 == 0 else None,
            "name": f"Товар <#{i}> & special" if i % 3 == 0 else None,
            "article": f"ART<{i}>" if i % 4 == 0 else None,
            "price": (i * 10000) if i % 5 != 0 else None,
            "kiz_required": (i % 2 == 0),
        })

    res = await service.send_batch_orders_notification(
        chat_ids=["123"],
        seller_id="seller-stress",
        orders=orders,
    )

    assert res is True
    assert len(captured) == 1
    msg = captured[0]

    assert "НОВЫЕ ЗАКАЗЫ FBS — 100 шт." in msg
    assert "... и ещё 90 заказов" in msg
    assert "&lt;#0&gt;" in msg
    _verify_telegram_html_validity(msg)


@pytest.mark.asyncio
async def test_morning_digest_empty_and_stress_cases():
    """Verify morning digest formats cleanly with 0 items, malformed timestamps, and None fields."""
    _stub_aiogram()
    from app.services.telegram_service import TelegramService

    service = TelegramService("mock_token")
    captured = []

    async def mock_broadcast(chat_ids, text, keyboard=None):
        captured.append(text)
        return True

    service._broadcast = mock_broadcast

    # 1. Empty pending orders
    res_empty = await service.send_morning_digest(
        chat_ids=["123"],
        seller_id="seller-1",
        pending_orders=[],
        digest_time_str="09:00 MSK",
    )
    assert res_empty is True
    assert "Необработанных заказов нет" in captured[-1]
    _verify_telegram_html_validity(captured[-1])

    # 2. Malformed orders and ISO dates
    malformed_pending = [
        {
            "id": None,
            "name": "<Item with Unescaped HTML>",
            "price": None,
            "kiz_required": True,
            "wb_created_at": "invalid-iso-date",
        },
        {
            "id": 8888,
            "name": None,
            "article": "SKU<TEST>",
            "price": 250000,
            "kiz_required": None,
            "wb_created_at": "2026-09-02T10:00:00Z",
        },
        {},  # Empty dictionary
    ]

    res_stress = await service.send_morning_digest(
        chat_ids=["123"],
        seller_id="seller-1",
        pending_orders=malformed_pending,
        digest_time_str="09:00 MSK",
    )
    assert res_stress is True
    msg = captured[-1]
    assert "Ожидает сборки:</b> 3 заказов" in msg
    assert "&lt;Item with Unescaped HTML&gt;" in msg
    assert "SKU&lt;TEST&gt;" in msg
    _verify_telegram_html_validity(msg)
