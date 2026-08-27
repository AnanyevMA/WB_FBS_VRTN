import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class KizOperationType(enum.Enum):
    ATTACH = "ATTACH"
    VALIDATE = "VALIDATE"
    WITHDRAWAL = "WITHDRAWAL"
    RETURN = "RETURN"


class KizOperation(Base):
    __tablename__ = "kiz_operations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,
    )
    seller_id: Mapped[str] = mapped_column(String(36), ForeignKey("sellers.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), index=True)
    
    kiz_code: Mapped[str] = mapped_column(String(255), index=True)
    gtin: Mapped[Optional[str]] = mapped_column(String(255))
    
    operation: Mapped[KizOperationType] = mapped_column(SAEnum(KizOperationType), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), index=True)
    
    cz_doc_id: Mapped[Optional[str]] = mapped_column(String(255))
    cz_doc_status: Mapped[Optional[str]] = mapped_column(String(100))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    
    retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    seller: Mapped["Seller"] = relationship("Seller", back_populates="kiz_operations")
    order: Mapped[Optional["Order"]] = relationship("Order")


class KizProductInfo(Base):
    """
    Информация о товаре, полученная по коду маркировки (КИЗ / SGTIN / DataMatrix).
    """
    __tablename__ = "kiz_product_info"

    id: Mapped[str] = mapped_column(
        String(36), 
        primary_key=True, 
        default=lambda: str(uuid.uuid4())
    )
    kiz_code: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    gtin: Mapped[str] = mapped_column(String(20), index=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    clean_cis: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # Характеристики товара
    product_name: Mapped[Optional[str]] = mapped_column(String(500))
    brand: Mapped[Optional[str]] = mapped_column(String(255))
    article: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    tech_size: Mapped[Optional[str]] = mapped_column(String(100))
    wb_size: Mapped[Optional[str]] = mapped_column(String(100))
    tnved: Mapped[Optional[str]] = mapped_column(String(20))
    product_group: Mapped[Optional[str]] = mapped_column(String(50), default="lp")

    # Данные из Честного Знака (ГИС МТ True API)
    cz_status: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    cz_status_ex: Mapped[Optional[str]] = mapped_column(String(255))
    cz_owner_inn: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    cz_owner_name: Mapped[Optional[str]] = mapped_column(String(255))
    cz_producer_inn: Mapped[Optional[str]] = mapped_column(String(20))
    cz_producer_name: Mapped[Optional[str]] = mapped_column(String(255))
    cz_emission_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cz_intro_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Связи
    seller_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("sellers.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), index=True)

    # Результаты проверок
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_cz_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    seller: Mapped[Optional["Seller"]] = relationship("Seller")
    order: Mapped[Optional["Order"]] = relationship("Order")


class BatchStatus(enum.Enum):
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class KizSignatureBatch(Base):
    """
    Пакет операций с маркировкой (КИЗ) из отчёта WB, ожидающий подписания ЭЦП и отправки в ГИС МТ.
    """
    __tablename__ = "kiz_signature_batches"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )
    seller_id: Mapped[str] = mapped_column(String(36), ForeignKey("sellers.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), default="archive.xlsx")
    source: Mapped[str] = mapped_column(String(50), default="telegram")  # "telegram", "web_upload", "auto"
    status: Mapped[BatchStatus] = mapped_column(SAEnum(BatchStatus), default=BatchStatus.PENDING_SIGNATURE, index=True)

    # Статистика
    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    returns_count: Mapped[int] = mapped_column(Integer, default=0)
    already_withdrawn_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)

    # Структурированные данные строк отчета (withdrawals, returns, summary)
    data_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Результаты отправки в ГИС МТ
    submission_results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    seller: Mapped["Seller"] = relationship("Seller", back_populates="signature_batches")