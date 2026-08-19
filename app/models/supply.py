import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Enum as SAEnum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class SupplyStatus(enum.Enum):
    CREATED = "CREATED"
    CLOSED = "CLOSED"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    wb_supply_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[SupplyStatus] = mapped_column(SAEnum(SupplyStatus), nullable=False, index=True)
    
    closed_at: Mapped[Optional[datetime]] = mapped_column()
    delivered_at: Mapped[Optional[datetime]] = mapped_column()
    
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    # Relationships
    seller: Mapped["Seller"] = relationship("Seller", back_populates="supplies")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="supply")
