import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), 
        primary_key=True, 
        default=lambda: str(uuid.uuid4())
    )
    seller_id: Mapped[Optional[str]] = mapped_column(ForeignKey("sellers.id"), index=True)
    
    agent: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    
    entity_type: Mapped[Optional[str]] = mapped_column(String(100))
    entity_id: Mapped[Optional[str]] = mapped_column(String(255))
    
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)
    trace_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)

    # Relationships
    seller: Mapped[Optional["Seller"]] = relationship("Seller", back_populates="audit_logs")
