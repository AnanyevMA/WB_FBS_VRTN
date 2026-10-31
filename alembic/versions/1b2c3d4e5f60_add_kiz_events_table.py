"""add_kiz_events_table

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b2c3d4e5f60'
down_revision: Union[str, None] = '0a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "kiz_events" not in inspector.get_table_names():
        op.create_table(
            "kiz_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("seller_id", sa.String(length=36), sa.ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("clean_cis", sa.String(length=100), nullable=False),
            sa.Column("raw_kiz", sa.Text(), nullable=True),
            sa.Column("event_type", sa.String(length=10), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(length=30), nullable=False),
            sa.Column("external_id", sa.String(length=255), nullable=False),
            sa.Column("srid", sa.String(length=100), nullable=True),
            sa.Column("order_id", sa.BigInteger(), nullable=True),
            sa.Column("price", sa.Numeric(12, 2), nullable=True),
            sa.Column("article", sa.String(length=255), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("receipt_number", sa.String(length=64), nullable=True),
            sa.Column("fn_number", sa.String(length=64), nullable=True),
            sa.Column("receipt_date", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("seller_id", "source", "external_id", name="uq_kiz_events_source_external"),
        )
        op.create_index("ix_kiz_events_seller_cis_time", "kiz_events", ["seller_id", "clean_cis", "occurred_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "kiz_events" in inspector.get_table_names():
        op.drop_index("ix_kiz_events_seller_cis_time", table_name="kiz_events")
        op.drop_table("kiz_events")
