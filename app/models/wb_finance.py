"""
WB Finance Data Models — WB FBS Manager
Хранение строк еженедельного детального отчета реализации Wildberries (POST /api/finance/v1/sales-reports/detailed).
Включает продажи, возвраты, логистику, маркировку КИЗ и статус принадлежности в Честном Знаке.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class WbSalesReportRow(Base):
    """Строка детального финансового отчета реализации WB."""
    __tablename__ = "wb_sales_report_rows"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    seller_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sellers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    rrd_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    report_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    gi_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    subject_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    nm_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    brand_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    vendor_code: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tech_size: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    barcode: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    doc_type_name: Mapped[Optional[str]] = mapped_column(String(50), index=True, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retail_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    retail_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    sale_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), default=Decimal("0.00"))
    commission_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), default=Decimal("0.00"))

    office_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    seller_oper_name: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)

    order_dt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sale_dt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rr_date: Mapped[Optional[date]] = mapped_column(Date, index=True, nullable=True)
    shk_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    retail_price_with_disc: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    delivery_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    return_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    delivery_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    gi_box_type_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    ppvz_sales_commission: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    for_pay: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    ppvz_reward: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))

    ppvz_office_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ppvz_supplier_inn: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Маркировка КИЗ
    kiz: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    clean_cis: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    srid: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    delivery_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    date_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Верификация в Честном Знаке (True API)
    cz_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    cz_owner_inn: Mapped[Optional[str]] = mapped_column(String(50), index=True, nullable=True)
    cz_owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_seller_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    cz_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    seller = relationship("Seller", backref="sales_report_rows")

    __table_args__ = (
        Index("uq_wb_report_seller_rrd", "seller_id", "rrd_id", unique=True),
    )
