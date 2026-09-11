import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, JSON, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[str] = mapped_column(
        String(36), 
        primary_key=True, 
        default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    wb_api_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    wb_supplier_id: Mapped[Optional[str]] = mapped_column(String(255))
    cz_inn: Mapped[Optional[str]] = mapped_column(String(50))
    cz_token_encrypted: Mapped[Optional[str]] = mapped_column(String)
    cz_cert_path: Mapped[Optional[str]] = mapped_column(String(500))
    cz_oms_id: Mapped[Optional[str]] = mapped_column(String(255))
    cryptopro_cert_thumbprint: Mapped[Optional[str]] = mapped_column(String(255))
    mod_fias: Mapped[Optional[str]] = mapped_column(String(255))
    mod_kpp: Mapped[Optional[str]] = mapped_column(String(255))
    telegram_bot_token_encrypted: Mapped[Optional[str]] = mapped_column(String)
    telegram_chat_ids: Mapped[Optional[list]] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    polling_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    polling_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")

    # Notification schedule settings
    notification_mode: Mapped[str] = mapped_column(String(32), default="instant", server_default="instant", nullable=False)
    notification_schedule: Mapped[list] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", server_default="Europe/Moscow", nullable=False)

    # Morning digest settings
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    digest_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    digest_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    digest_timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", server_default="'Europe/Moscow'")

    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Archive reminder settings
    archive_reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    archive_reminder_days: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    archive_reminder_hour: Mapped[int] = mapped_column(Integer, default=14, server_default="14")
    archive_reminder_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_archive_uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_archive_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Auto KIZ queue settings (daily automated batch creation)
    auto_kiz_queue_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    auto_kiz_queue_hour: Mapped[int] = mapped_column(Integer, default=17, server_default="17")
    auto_kiz_queue_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    auto_kiz_auto_sign_server: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    auto_kiz_manager_chat_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_auto_kiz_queue_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="seller", cascade="all, delete-orphan")
    supplies: Mapped[list["Supply"]] = relationship("Supply", back_populates="seller", cascade="all, delete-orphan")
    kiz_operations: Mapped[list["KizOperation"]] = relationship("KizOperation", back_populates="seller", cascade="all, delete-orphan")
    signature_batches: Mapped[list["KizSignatureBatch"]] = relationship("KizSignatureBatch", back_populates="seller", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="seller", cascade="all, delete-orphan")
    kiz_product_info: Mapped[list["KizProductInfo"]] = relationship("KizProductInfo", back_populates="seller", cascade="all, delete-orphan")

    @property
    def has_wb_token(self) -> bool:
        return bool(self.wb_api_token_encrypted)

    @property
    def wb_token_expires_at(self) -> Optional[datetime]:
        if not self.wb_api_token_encrypted:
            return None
        try:
            from app.services.encryption import decrypt
            from app.services.wb_client import parse_wb_token_expiration
            raw_token = decrypt(self.wb_api_token_encrypted)
            return parse_wb_token_expiration(raw_token)
        except Exception:
            return None

    @property
    def wb_token_status(self) -> str:
        """Returns 'missing', 'expired', 'expiring_soon', or 'valid'."""
        if not self.wb_api_token_encrypted:
            return "missing"
        try:
            from app.services.encryption import decrypt
            from app.services.wb_client import get_wb_token_status
            raw_token = decrypt(self.wb_api_token_encrypted)
            st, _, _ = get_wb_token_status(raw_token)
            return st
        except Exception:
            return "valid"

    @property
    def has_cz_token(self) -> bool:
        return bool(self.cz_token_encrypted)

    @property
    def has_telegram_token(self) -> bool:
        return bool(self.telegram_bot_token_encrypted)

    @property
    def cz_token_preview(self) -> Optional[str]:
        if not self.cz_token_encrypted:
            return None
        try:
            from app.services.encryption import decrypt
            dec = decrypt(self.cz_token_encrypted)
            if dec and len(dec) > 14:
                return f"{dec[:8]}...{dec[-6:]}"
            elif dec:
                return "активен"
        except Exception:
            return "сохранен"
        return None