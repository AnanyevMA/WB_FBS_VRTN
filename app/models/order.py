import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Enum as SAEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class OrderStatus(enum.Enum):
    NEW = "NEW"
    ASSEMBLING = "ASSEMBLING"
    ASSEMBLED = "ASSEMBLED"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    SORTED = "SORTED"


class KizStatus(enum.Enum):
    PENDING = "PENDING"
    ATTACHED = "ATTACHED"
    VALIDATED = "VALIDATED"
    WITHDRAWN = "WITHDRAWN"
    RETURNED = "RETURNED"
    ERROR = "ERROR"
    NOT_REQUIRED = "NOT_REQUIRED"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    supply_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("supplies.id"), index=True)
    
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), nullable=False, index=True)
    wb_status: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    supplier_status: Mapped[Optional[str]] = mapped_column(String(50))
    
    wb_created_at: Mapped[datetime] = mapped_column(nullable=False)
    wb_supply_id: Mapped[Optional[str]] = mapped_column(String(255))
    
    chrt_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    nm_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    article: Mapped[Optional[str]] = mapped_column(String(255))
    brand: Mapped[Optional[str]] = mapped_column(String(255))
    subject: Mapped[Optional[str]] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(String(500))
    tech_size: Mapped[Optional[str]] = mapped_column(String(100))
    wb_size: Mapped[Optional[str]] = mapped_column(String(100))
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    
    sticker_id: Mapped[Optional[str]] = mapped_column(String(255))
    sticker_base64: Mapped[Optional[str]] = mapped_column(Text)
    
    kiz_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    kiz_code: Mapped[Optional[str]] = mapped_column(String(255))
    kiz_status: Mapped[KizStatus] = mapped_column(SAEnum(KizStatus), default=KizStatus.PENDING, index=True)
    kiz_attached_at: Mapped[Optional[datetime]] = mapped_column()
    kiz_cz_status: Mapped[Optional[str]] = mapped_column(String(100))
    kiz_cz_status_updated_at: Mapped[Optional[datetime]] = mapped_column()
    
    cz_withdrawal_doc_id: Mapped[Optional[str]] = mapped_column(String(255))
    cz_return_doc_id: Mapped[Optional[str]] = mapped_column(String(255))
    
    deadline_at: Mapped[Optional[datetime]] = mapped_column()
    notified_at: Mapped[Optional[datetime]] = mapped_column()
    
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    # Relationships
    seller: Mapped["Seller"] = relationship("Seller", back_populates="orders")
    supply: Mapped[Optional["Supply"]] = relationship("Supply", back_populates="orders")
