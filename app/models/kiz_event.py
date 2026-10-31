"""
KIZ Event Log — WB FBS Manager
Журнал событий о КИЗ из источников WB: продажа или возврат.
Фоновые сборщики только пополняют журнал, решения по нему принимает единая сверка.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base

EVENT_SALE = "SALE"
EVENT_RETURN = "RETURN"

SOURCE_FINANCE = "wb_finance"
SOURCE_EXCISE = "wb_excise"
SOURCE_ITEM_RETURNS = "wb_item_returns"
SOURCE_ORDERS = "fbs_orders"
SOURCE_ARCHIVE_FILE = "archive_file"


class KizEvent(Base):
    __tablename__ = "kiz_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id: Mapped[str] = mapped_column(String(36), ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False)

    clean_cis: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_kiz: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    event_type: Mapped[str] = mapped_column(String(10), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # A repeated poll finds the same (seller_id, source, external_id) and adds nothing
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)

    srid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    article: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    receipt_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fn_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    receipt_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("seller_id", "source", "external_id", name="uq_kiz_events_source_external"),
        Index("ix_kiz_events_seller_cis_time", "seller_id", "clean_cis", "occurred_at"),
    )
