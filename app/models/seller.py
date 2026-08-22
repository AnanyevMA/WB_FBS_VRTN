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

    # Morning digest settings
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    digest_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    digest_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    digest_timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", server_default="'Europe/Moscow'")

    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="seller", cascade="all, delete-orphan")
    supplies: Mapped[list["Supply"]] = relationship("Supply", back_populates="seller", cascade="all, delete-orphan")
    kiz_operations: Mapped[list["KizOperation"]] = relationship("KizOperation", back_populates="seller", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="seller", cascade="all, delete-orphan")