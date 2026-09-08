"""
Unit & Integration Tests for Archive Upload Reminders Agent (Every 2 days)
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.seller import Seller
from app.agents.archive_processor import check_archive_reminders
from app.services.encryption import encrypt


@pytest.fixture
def sync_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_archive_reminder_never_uploaded(sync_db, monkeypatch):
    """If seller has never uploaded an archive, a reminder should be sent."""
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    seller = Seller(
        id="seller-1",
        name="Test Seller 1",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        last_archive_uploaded_at=None,
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        check_archive_reminders(ignore_time_window=True)

        assert mock_send.called
        sync_db.refresh(seller)
        assert seller.last_archive_reminder_sent_at is not None


def test_archive_reminder_uploaded_recently_skipped(sync_db, monkeypatch):
    """If archive was uploaded 1 day ago (less than 2 days), reminder must NOT be sent."""
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    now = datetime.now(timezone.utc)
    seller = Seller(
        id="seller-2",
        name="Test Seller 2",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        last_archive_uploaded_at=now - timedelta(days=1),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        check_archive_reminders(ignore_time_window=True)
        assert not mock_send.called


def test_archive_reminder_uploaded_3_days_ago_triggers(sync_db, monkeypatch):
    """If archive was uploaded 3 days ago (>= 2 days), reminder MUST be sent."""
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    now = datetime.now(timezone.utc)
    seller = Seller(
        id="seller-3",
        name="Test Seller 3",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        last_archive_uploaded_at=now - timedelta(days=3),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        check_archive_reminders(ignore_time_window=True)

        assert mock_send.called
        sync_db.refresh(seller)
        assert seller.last_archive_reminder_sent_at is not None


def test_archive_reminder_disabled_skipped(sync_db, monkeypatch):
    """If archive_reminder_enabled is False, reminder must NOT be sent."""
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    now = datetime.now(timezone.utc)
    seller = Seller(
        id="seller-4",
        name="Test Seller 4",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=False,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        last_archive_uploaded_at=now - timedelta(days=5),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        check_archive_reminders(ignore_time_window=True)
        assert not mock_send.called


def test_archive_reminder_time_window_exact_14_00(sync_db, monkeypatch):
    """When current time is exactly 14:00 Moscow time, reminder should be sent."""
    import zoneinfo
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    msk_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    # Set simulated now to 14:00 Moscow time
    fake_now = datetime(2026, 9, 8, 14, 0, 0, tzinfo=msk_tz).astimezone(timezone.utc)

    seller = Seller(
        id="seller-time-1",
        name="Test Seller Time 14:00",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        timezone="Europe/Moscow",
        last_archive_uploaded_at=fake_now - timedelta(days=2),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        with patch("app.agents.archive_processor.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            check_archive_reminders(ignore_time_window=False)

        assert mock_send.called
        sync_db.refresh(seller)
        assert seller.last_archive_reminder_sent_at is not None


def test_archive_reminder_time_window_before_14_00_skipped(sync_db, monkeypatch):
    """When current time is 11:30 (before 14:00), reminder must NOT be sent."""
    import zoneinfo
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    msk_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    fake_now = datetime(2026, 9, 8, 11, 30, 0, tzinfo=msk_tz).astimezone(timezone.utc)

    seller = Seller(
        id="seller-time-2",
        name="Test Seller Before 14:00",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        timezone="Europe/Moscow",
        last_archive_uploaded_at=fake_now - timedelta(days=3),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        with patch("app.agents.archive_processor.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            check_archive_reminders(ignore_time_window=False)

        assert not mock_send.called


def test_archive_reminder_already_sent_today_skipped(sync_db, monkeypatch):
    """If reminder was already sent today at 14:00, second check at 14:05 must NOT send again."""
    import zoneinfo
    monkeypatch.setattr("app.agents.archive_processor.sync_engine", sync_db.bind)

    msk_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    fake_now = datetime(2026, 9, 8, 14, 5, 0, tzinfo=msk_tz).astimezone(timezone.utc)
    earlier_today = datetime(2026, 9, 8, 14, 0, 0, tzinfo=msk_tz).astimezone(timezone.utc)

    seller = Seller(
        id="seller-time-3",
        name="Test Seller Already Sent Today",
        wb_api_token_encrypted=encrypt("wb-token"),
        telegram_bot_token_encrypted=encrypt("tg-token-123"),
        telegram_chat_ids=["123456789"],
        is_active=True,
        archive_reminder_enabled=True,
        archive_reminder_days=2,
        archive_reminder_hour=14,
        archive_reminder_minute=0,
        timezone="Europe/Moscow",
        last_archive_uploaded_at=fake_now - timedelta(days=5),
        last_archive_reminder_sent_at=earlier_today,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        with patch("app.agents.archive_processor.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            check_archive_reminders(ignore_time_window=False)

        assert not mock_send.called

