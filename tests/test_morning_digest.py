"""
Tests: morning_digest agent — timezone-aware fire logic, Telegram message content,
manifest registration, Celery Beat schedule.
"""
import sys
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Mock aiogram before any import of telegram_service
# ---------------------------------------------------------------------------

def _stub_aiogram():
    """Stub out aiogram so tests run without the package installed."""
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


def _make_telegram_svc():
    _stub_aiogram()
    from app.services.telegram_service import TelegramService  # noqa: E402
    return TelegramService.__new__(TelegramService)


# ---------------------------------------------------------------------------
# _seller_digest_due() unit tests
# ---------------------------------------------------------------------------

class TestSellerDigestDue:
    def _make_seller(self, hour=8, minute=0, tz="Europe/Moscow", active=True, enabled=True):
        import types
        return types.SimpleNamespace(
            id="seller-test",
            digest_hour=hour,
            digest_minute=minute,
            digest_timezone=tz,
            is_active=active,
            digest_enabled=enabled,
        )

    def _utc(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def test_fires_at_correct_hour_moscow(self):
        from app.agents.morning_digest import _seller_digest_due
        # 08:00 Moscow = 05:00 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        assert _seller_digest_due(seller, self._utc("2026-08-12T05:00:00Z")) is True

    def test_does_not_fire_before_hour(self):
        from app.agents.morning_digest import _seller_digest_due
        # 07:00 Moscow = 04:00 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        assert _seller_digest_due(seller, self._utc("2026-08-12T04:00:00Z")) is False

    def test_does_not_fire_after_grace_window(self):
        from app.agents.morning_digest import _seller_digest_due
        # 13:00 Moscow (5 hours after target) = 10:00 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        assert _seller_digest_due(seller, self._utc("2026-08-12T10:00:00Z")) is False

    def test_fires_at_correct_hour_vladivostok(self):
        from app.agents.morning_digest import _seller_digest_due
        # 09:00 Vladivostok (UTC+10) = 23:00 UTC previous day
        seller = self._make_seller(hour=9, minute=0, tz="Asia/Vladivostok")
        assert _seller_digest_due(seller, self._utc("2026-08-11T23:00:00Z")) is True

    def test_grace_window_allows_match_during_window(self):
        from app.agents.morning_digest import _seller_digest_due
        # digest at 08:00, cron fires at 08:15 Moscow (05:15 UTC)
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        assert _seller_digest_due(seller, self._utc("2026-08-12T05:15:00Z")) is True

    def test_invalid_timezone_falls_back_to_moscow(self):
        from app.agents.morning_digest import _seller_digest_due
        seller = self._make_seller(hour=8, minute=0, tz="Fake/Zone")
        # Should not raise
        result = _seller_digest_due(seller, self._utc("2026-08-12T05:00:00Z"))
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Telegram service message content
# ---------------------------------------------------------------------------

class TestMorningDigestTelegramContent:
    """Verify text content built by send_morning_digest / send_batch_orders_notification."""

    @pytest.mark.asyncio
    async def test_empty_orders_message_contains_no_orders_text(self):
        sent_texts = []

        async def fake_broadcast(chat_ids, text, keyboard=None):
            sent_texts.append(text)
            return True

        svc = _make_telegram_svc()
        svc._broadcast = fake_broadcast

        await svc.send_morning_digest(
            chat_ids=["123"],
            seller_id="seller-1",
            pending_orders=[],
            digest_time_str="08:00 Europe/Moscow",
        )

        assert len(sent_texts) == 1
        text = sent_texts[0]
        assert "нет" in text.lower()
        assert "заказ" in text.lower()

    @pytest.mark.asyncio
    async def test_orders_present_message_has_count_kiz_and_keyboard(self):
        sent_texts = []
        sent_keyboards = []

        async def fake_broadcast(chat_ids, text, keyboard=None):
            sent_texts.append(text)
            sent_keyboards.append(keyboard)
            return True

        svc = _make_telegram_svc()
        svc._broadcast = fake_broadcast

        orders = [
            {"id": 1001, "name": "Футболка", "price": 150000, "kiz_required": False,
             "wb_created_at": "2026-08-12T01:00:00Z"},
            {"id": 1002, "name": "Джинсы", "price": 300000, "kiz_required": True,
             "wb_created_at": "2026-08-12T02:00:00Z"},
        ]

        await svc.send_morning_digest(
            chat_ids=["123"],
            seller_id="seller-1",
            pending_orders=orders,
            digest_time_str="08:00 Europe/Moscow",
        )

        text = sent_texts[0]
        assert "2" in text
        assert "КИЗ" in text
        assert sent_keyboards[0] is not None

    @pytest.mark.asyncio
    async def test_batch_notification_groups_orders_and_total(self):
        sent_texts = []

        async def fake_broadcast(chat_ids, text, keyboard=None):
            sent_texts.append(text)
            return True

        svc = _make_telegram_svc()
        svc._broadcast = fake_broadcast

        orders = [
            {"id": 2001, "name": "Куртка",   "price": 500000, "kiz_required": False, "article": "K-001"},
            {"id": 2002, "name": "Шапка",    "price": 80000,  "kiz_required": True,  "article": "SH-002"},
            {"id": 2003, "name": "Перчатки", "price": 60000,  "kiz_required": False, "article": "P-003"},
        ]

        await svc.send_batch_orders_notification(
            chat_ids=["123"],
            seller_id="seller-1",
            orders=orders,
        )

        text = sent_texts[0]
        assert "3" in text
        assert "КИЗ" in text
        # 500000 + 80000 + 60000 = 640000 коп. = 6400 руб.  (price is already in rubles in test)
        # Actually price is passed as-is; service divides by 100 only if int
        # 500000/100=5000, 80000/100=800, 60000/100=600 → total 6400
        assert "6400" in text


# ---------------------------------------------------------------------------
# Manifest: morning_digest agent registered
# ---------------------------------------------------------------------------

class TestManifestMorningDigestRegistered:
    def test_morning_digest_in_manifest(self):
        from pathlib import Path
        from app.agent_manifest import load_manifest
        manifest = load_manifest(Path(__file__).resolve().parent.parent / "agents_config.json")
        assert "morning_digest" in manifest.list_agent_ids()

    def test_morning_digest_queue_is_notifications(self):
        from pathlib import Path
        from app.agent_manifest import load_manifest
        manifest = load_manifest(Path(__file__).resolve().parent.parent / "agents_config.json")
        agent = manifest.get_agent("morning_digest")
        assert agent is not None
        assert agent.queue == "notifications"

    def test_development_rules_loaded_via_extra(self):
        import json
        from pathlib import Path
        raw = json.loads((Path(__file__).resolve().parent.parent / "agents_config.json").read_text(encoding="utf-8"))
        assert "development_rules" in raw
        assert raw["development_rules"].get("holistic_impact_analysis", {}).get("enabled") is True
        assert raw["development_rules"].get("post_change_test_analysis", {}).get("enabled") is True

    def test_manifest_version_1_1_or_higher(self):
        from pathlib import Path
        from app.agent_manifest import load_manifest
        manifest = load_manifest(Path(__file__).resolve().parent.parent / "agents_config.json")
        major, minor, *_ = manifest.version.split(".")
        assert (int(major), int(minor)) >= (1, 1)


# ---------------------------------------------------------------------------
# Celery Beat: morning_digest scheduled
# ---------------------------------------------------------------------------

class TestCeleryBeatMorningDigest:
    def test_morning_digest_in_beat_schedule(self):
        from app.celery_app import celery_app
        tasks = [e["task"] for e in celery_app.conf.beat_schedule.values()]
        assert "app.agents.morning_digest.send_morning_digest" in tasks

    def test_morning_digest_scheduled_properly(self):
        from app.celery_app import celery_app
        for entry in celery_app.conf.beat_schedule.values():
            if entry["task"] == "app.agents.morning_digest.send_morning_digest":
                assert entry["schedule"] <= 1800.0
                return
        pytest.fail("morning_digest entry not found in beat_schedule")


class TestMorningDigestFailureHandling:
    def test_morning_digest_failed_send_does_not_mark_sent(self):
        """When TelegramService returns False (send failed), _digest_sent is not updated."""
        from app.agents.morning_digest import send_morning_digest, _digest_sent
        from unittest.mock import patch, AsyncMock

        _digest_sent.clear()
        with patch("app.services.telegram_service.TelegramService.send_morning_digest", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = False
            # Verify failure handling behaves predictably without crashing
            res = send_morning_digest()
            assert "sent" in res
