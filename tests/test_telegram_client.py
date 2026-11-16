"""Отправка сообщений в Telegram напрямую через Bot API, без aiogram."""
import json
import logging
import subprocess
import sys

import httpx
import pytest

from app.services.telegram_service import (
    TelegramAPIError, TelegramBotClient, TelegramService, _RedactBotTokenFilter,
)

TOKEN = "123456789:AAE-secret_Token"


def _client(handler):
    return TelegramBotClient(TOKEN, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_send_message_posts_html_text_and_keyboard():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = _client(handler)
    keyboard = {"inline_keyboard": [[{"text": "Детали", "callback_data": "order:detail:1"}]]}
    result = await client.send_message(chat_id=42, text="<b>hi</b>", reply_markup=keyboard)
    await client.close()

    assert result == {"message_id": 7}
    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert seen["body"] == {"chat_id": 42, "text": "<b>hi</b>", "parse_mode": "HTML", "reply_markup": keyboard}


@pytest.mark.asyncio
async def test_send_message_without_keyboard_omits_reply_markup():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _client(handler)
    await client.send_message(chat_id="1", text="t")
    await client.close()
    assert "reply_markup" not in bodies[0]


@pytest.mark.asyncio
async def test_api_error_is_raised_with_description():
    client = _client(lambda r: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"}))
    with pytest.raises(TelegramAPIError, match="bot was blocked"):
        await client.send_message(chat_id=1, text="t")
    await client.close()


@pytest.mark.asyncio
async def test_network_error_does_not_leak_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = _client(handler)
    with pytest.raises(TelegramAPIError) as err:
        await client.send_message(chat_id=1, text="t")
    await client.close()
    assert TOKEN not in str(err.value)
    assert "/bot<redacted>" in str(err.value)


@pytest.mark.asyncio
async def test_broadcast_survives_failed_chat_and_reports_success():
    def handler(request):
        chat = json.loads(request.content)["chat_id"]
        if chat == "bad":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    svc = TelegramService(TOKEN)
    svc._bot = _client(handler)
    assert await svc.send_text(["bad", "good"], "hello") is True
    assert svc._bot is None  # connection is not carried over to the next event loop

    svc._bot = _client(handler)
    assert await svc.send_text(["bad"], "hello") is False


def test_httpx_log_filter_hides_bot_token():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
        ("POST", httpx.URL(f"https://api.telegram.org/bot{TOKEN}/sendMessage"), "HTTP/1.1 200 OK"), None,
    )
    assert _RedactBotTokenFilter().filter(record) is True
    assert TOKEN not in record.getMessage()
    assert "https://api.telegram.org/bot<redacted>/sendMessage" in record.getMessage()


def test_telegram_service_does_not_import_aiogram():
    """aiogram costs ~50 MB per process; the API and the worker must not load it for notifications."""
    code = "import sys; import app.services.telegram_service; print('aiogram' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
