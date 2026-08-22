"""
Tests: polling interval + digest settings — seller schema validation.

Covers:
- polling_interval_minutes range constraints (1-60)
- digest timezone IANA validation
- DigestSettings nested object
- SellerResponse.polling_interval_minutes computed from seconds
"""
import pytest
from pydantic import ValidationError

from app.schemas.seller import (
    SellerCreate,
    SellerUpdate,
    DigestSettings,
)


# ---------------------------------------------------------------------------
# DigestSettings
# ---------------------------------------------------------------------------

class TestDigestSettings:
    def test_defaults(self):
        d = DigestSettings()
        assert d.enabled is True
        assert d.hour == 8
        assert d.minute == 0
        assert d.timezone == "Europe/Moscow"

    def test_valid_russian_timezones(self):
        valid_tzs = [
            "Europe/Moscow",
            "Europe/Kaliningrad",
            "Asia/Yekaterinburg",
            "Asia/Novosibirsk",
            "Asia/Krasnoyarsk",
            "Asia/Irkutsk",
            "Asia/Yakutsk",
            "Asia/Vladivostok",
            "Asia/Magadan",
            "Asia/Kamchatka",
            "UTC",
        ]
        for tz in valid_tzs:
            d = DigestSettings(timezone=tz)
            assert d.timezone == tz

    def test_invalid_timezone_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DigestSettings(timezone="Russia/Nowhere")
        assert "часовой пояс" in str(exc_info.value).lower() or "timezone" in str(exc_info.value).lower()

    def test_hour_bounds(self):
        DigestSettings(hour=0)
        DigestSettings(hour=23)
        with pytest.raises(ValidationError):
            DigestSettings(hour=24)
        with pytest.raises(ValidationError):
            DigestSettings(hour=-1)

    def test_minute_bounds(self):
        DigestSettings(minute=0)
        DigestSettings(minute=59)
        with pytest.raises(ValidationError):
            DigestSettings(minute=60)
        with pytest.raises(ValidationError):
            DigestSettings(minute=-1)


# ---------------------------------------------------------------------------
# SellerCreate — polling_interval_minutes
# ---------------------------------------------------------------------------

class TestSellerCreatePollingInterval:
    def _base(self, **kwargs):
        return dict(name="Test Shop", wb_api_token="tok", **kwargs)

    def test_default_no_interval(self):
        s = SellerCreate(**self._base())
        assert s.polling_interval_minutes is None

    def test_valid_interval_1(self):
        s = SellerCreate(**self._base(polling_interval_minutes=1))
        assert s.polling_interval_minutes == 1

    def test_valid_interval_60(self):
        s = SellerCreate(**self._base(polling_interval_minutes=60))
        assert s.polling_interval_minutes == 60

    def test_interval_below_min_raises(self):
        with pytest.raises(ValidationError):
            SellerCreate(**self._base(polling_interval_minutes=0))

    def test_interval_above_max_raises(self):
        with pytest.raises(ValidationError):
            SellerCreate(**self._base(polling_interval_minutes=61))

    def test_nested_digest_object(self):
        s = SellerCreate(**self._base(
            digest=DigestSettings(hour=9, minute=30, timezone="Asia/Yekaterinburg")
        ))
        assert s.digest.hour == 9
        assert s.digest.minute == 30
        assert s.digest.timezone == "Asia/Yekaterinburg"


# ---------------------------------------------------------------------------
# SellerUpdate — flat digest fields + timezone validation
# ---------------------------------------------------------------------------

class TestSellerUpdateDigest:
    def test_flat_digest_fields_valid(self):
        u = SellerUpdate(
            digest_enabled=True,
            digest_hour=7,
            digest_minute=30,
            digest_timezone="Asia/Vladivostok",
        )
        assert u.digest_enabled is True
        assert u.digest_hour == 7
        assert u.digest_minute == 30
        assert u.digest_timezone == "Asia/Vladivostok"

    def test_flat_digest_timezone_invalid_raises(self):
        with pytest.raises(ValidationError):
            SellerUpdate(digest_timezone="Fake/Zone")

    def test_flat_polling_interval_valid(self):
        u = SellerUpdate(polling_interval_minutes=5)
        assert u.polling_interval_minutes == 5

    def test_flat_polling_interval_bounds(self):
        with pytest.raises(ValidationError):
            SellerUpdate(polling_interval_minutes=0)
        with pytest.raises(ValidationError):
            SellerUpdate(polling_interval_minutes=61)

    def test_empty_update_is_valid(self):
        u = SellerUpdate()
        assert u.polling_interval_minutes is None
        assert u.digest_enabled is None


# ---------------------------------------------------------------------------
# SellerResponse — polling_interval_minutes computed from seconds
# ---------------------------------------------------------------------------

class TestSellerResponseComputedInterval:
    def _make_seller_obj(self, secs: int):
        """Minimal object simulating an ORM Seller row."""
        import types
        from datetime import datetime, timezone
        obj = types.SimpleNamespace(
            id="seller-1",
            name="Test Shop",
            wb_supplier_id=None,
            cz_inn=None,
            mod_fias=None,
            mod_kpp=None,
            telegram_chat_ids=None,
            is_active=True,
            polling_enabled=True,
            polling_interval_seconds=secs,
            digest_enabled=True,
            digest_hour=8,
            digest_minute=0,
            digest_timezone="Europe/Moscow",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return obj

    def test_60_seconds_gives_1_minute(self):
        from app.schemas.seller import SellerResponse
        r = SellerResponse.model_validate(self._make_seller_obj(60))
        assert r.polling_interval_minutes == 1

    def test_300_seconds_gives_5_minutes(self):
        from app.schemas.seller import SellerResponse
        r = SellerResponse.model_validate(self._make_seller_obj(300))
        assert r.polling_interval_minutes == 5

    def test_zero_seconds_gives_1_minute_minimum(self):
        from app.schemas.seller import SellerResponse
        r = SellerResponse.model_validate(self._make_seller_obj(0))
        assert r.polling_interval_minutes >= 1

    def test_last_polled_at_field(self):
        from app.schemas.seller import SellerResponse
        from datetime import datetime, timezone
        obj = self._make_seller_obj(600)
        obj.last_polled_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        r = SellerResponse.model_validate(obj)
        assert r.last_polled_at is not None
        assert r.polling_interval_minutes == 10
