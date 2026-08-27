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
        last_archive_uploaded_at=None,
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        check_archive_reminders()

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
        last_archive_uploaded_at=now - timedelta(days=1),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        check_archive_reminders()
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
        last_archive_uploaded_at=now - timedelta(days=3),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        check_archive_reminders()

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
        last_archive_uploaded_at=now - timedelta(days=5),
        last_archive_reminder_sent_at=None,
    )
    sync_db.add(seller)
    sync_db.commit()

    with patch("app.services.telegram_service.TelegramService.send_archive_reminder", new_callable=AsyncMock) as mock_send:
        check_archive_reminders()
        assert not mock_send.called
