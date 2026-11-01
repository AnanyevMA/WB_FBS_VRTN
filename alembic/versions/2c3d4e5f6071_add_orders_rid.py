"""add_orders_rid

Revision ID: 2c3d4e5f6071
Revises: 1b2c3d4e5f60
Create Date: 2026-11-01 04:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c3d4e5f6071'
down_revision: Union[str, None] = '1b2c3d4e5f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "rid" not in {c["name"] for c in inspector.get_columns("orders")}:
        op.add_column("orders", sa.Column("rid", sa.String(length=255), nullable=True))
    if "ix_orders_rid" not in {i["name"] for i in inspector.get_indexes("orders")}:
        op.create_index("ix_orders_rid", "orders", ["rid"])


def downgrade() -> None:
    op.drop_index("ix_orders_rid", table_name="orders")
    op.drop_column("orders", "rid")
