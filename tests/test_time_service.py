"""
Unit tests for app.services.time_service.
Verifies server time detection, timezone resolution, seller local time formatting,
and persistent daily morning digest trigger calculation.
"""
from datetime import datetime, timezone
import types
import pytest

from app.services.time_service import (
    resolve_timezone,
    get_server_time_info,
    get_now_in_timezone,
    get_seller_local_time,
    format_seller_digest_time,
    is_seller_digest_due,
    DEFAULT_TIMEZONE,
)


class TestTimeServiceResolution:
    def test_resolve_standard_timezones(self):
        tz_msk = resolve_timezone("Europe/Moscow")
        assert tz_msk.key == "Europe/Moscow"

        tz_krsk = resolve_timezone("Asia/Krasnoyarsk")
        assert tz_krsk.key == "Asia/Krasnoyarsk"

        tz_utc = resolve_timezone("UTC")
        assert tz_utc.key == "UTC"

    def test_resolve_invalid_or_empty_falls_back_to_moscow(self):
        tz_none = resolve_timezone(None)
        assert tz_none.key == DEFAULT_TIMEZONE

        tz_empty = resolve_timezone("   ")
        assert tz_empty.key == DEFAULT_TIMEZONE

        tz_invalid = resolve_timezone("Invalid/NonExistentZone")
        assert tz_invalid.key == DEFAULT_TIMEZONE


class TestServerTimeInfo:
    def test_get_server_time_info_structure(self):
        info = get_server_time_info()
        assert "server_utc_now" in info
        assert "server_local_now" in info
        assert "server_timezone" in info
        assert "utc_offset_seconds" in info
        assert "utc_offset_hours" in info
        assert "utc_offset_str" in info
        assert isinstance(info["utc_offset_seconds"], int)
        assert isinstance(info["utc_offset_hours"], float)


class TestSellerTimeFormatting:
    def test_format_seller_digest_time_krasnoyarsk(self):
        seller = types.SimpleNamespace(
            id="seller-krsk",
            digest_timezone="Asia/Krasnoyarsk",
        )
        # Fixed UTC time: 2026-08-21 02:44:00 UTC (which is 09:44 in UTC+7)
        fixed_utc = datetime(2026, 8, 21, 2, 44, 0, tzinfo=timezone.utc)
        
        formatted = format_seller_digest_time(seller, dt=fixed_utc)
        assert formatted == "09:44 (Asia/Krasnoyarsk)"

    def test_format_seller_digest_time_moscow(self):
        seller = types.SimpleNamespace(
            id="seller-msk",
            digest_timezone="Europe/Moscow",
        )
        # Fixed UTC time: 2026-08-21 05:00:00 UTC (which is 08:00 in UTC+3)
        fixed_utc = datetime(2026, 8, 21, 5, 0, 0, tzinfo=timezone.utc)
        
        formatted = format_seller_digest_time(seller, dt=fixed_utc)
        assert formatted == "08:00 (Europe/Moscow)"


class TestIsSellerDigestDue:
    def _make_seller(self, hour=8, minute=0, tz="Europe/Moscow", active=True, enabled=True):
        return types.SimpleNamespace(
            id="test-seller-1",
            digest_hour=hour,
            digest_minute=minute,
            digest_timezone=tz,
            is_active=active,
            digest_enabled=enabled,
        )

    def _utc(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def test_due_at_exact_time_moscow(self):
        # 08:00 Moscow (UTC+3) = 05:00 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        sent_tracker = {}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T05:00:00Z"), in_memory_sent_tracker=sent_tracker) is True

    def test_not_due_before_target_time(self):
        # 07:55 Moscow = 04:55 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        sent_tracker = {}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T04:55:00Z"), in_memory_sent_tracker=sent_tracker) is False

    def test_due_within_grace_window_if_not_yet_sent(self):
        # 08:30 Moscow (30 min after target) = 05:30 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        sent_tracker = {}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T05:30:00Z"), in_memory_sent_tracker=sent_tracker) is True

    def test_not_due_after_grace_window(self):
        # 12:00 Moscow (4 hours after target, grace=3) = 09:00 UTC
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        sent_tracker = {}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T09:00:00Z"), in_memory_sent_tracker=sent_tracker) is False

    def test_not_due_if_already_sent_today_in_memory(self):
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow")
        sent_tracker = {"test-seller-1": "2026-08-21"}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T05:15:00Z"), in_memory_sent_tracker=sent_tracker) is False

    def test_due_in_krasnoyarsk_timezone(self):
        # 09:00 Krasnoyarsk (UTC+7) = 02:00 UTC
        seller = self._make_seller(hour=9, minute=0, tz="Asia/Krasnoyarsk")
        sent_tracker = {}
        assert is_seller_digest_due(seller, self._utc("2026-08-21T02:00:00Z"), in_memory_sent_tracker=sent_tracker) is True

    def test_disabled_seller_never_due(self):
        seller = self._make_seller(hour=8, minute=0, tz="Europe/Moscow", enabled=False)
        assert is_seller_digest_due(seller, self._utc("2026-08-21T05:00:00Z")) is False
