import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class ProductCard(Base):
    """
    Карточка товара в Национальном каталоге товаров (НКТ / ГИС МТ Честный Знак).
    Изолированная модель для управления описанием товаров продавца.
    """
    __tablename__ = "product_cards"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )
    seller_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sellers.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )
    good_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    gtin: Mapped[Optional[str]] = mapped_column(String(14), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tnved: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    category_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Статусы в НКТ: draft, moderation, notsigned, published, errors, archived
    status: Mapped[str] = mapped_column(String(50), default="draft", server_default="draft", index=True)
    good_mark_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    good_turn_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_tech_gtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_set: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Идентификатор последнего фида и статус обработки
    feed_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    feed_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Структурированные атрибуты: [{"attr_id": 2478, "attr_value": "...", "attr_value_type": "...", ...}]
    attributes: Mapped[Optional[list]] = mapped_column(JSON, default=list, server_default="[]")
    # Изображения: [{"photo_type": "default", "photo_url": "..."}]
    images: Mapped[Optional[list]] = mapped_column(JSON, default=list, server_default="[]")
    # Список ошибок модератора
    error_details: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Эталонный XML документа для подписи УКЭП
    raw_xml: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    seller: Mapped["Seller"] = relationship("Seller")
