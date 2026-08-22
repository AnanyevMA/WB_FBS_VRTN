"""add_last_polled_at_to_sellers

Revision ID: 9c0a1b2c3d4e
Revises: 8b9f02c3d4e5
Create Date: 2026-08-22 11:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9c0a1b2c3d4e'
down_revision: Union[str, None] = '8b9f02c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "sellers" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "last_polled_at" not in columns:
            op.add_column(
                "sellers",
                sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "sellers" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "last_polled_at" in columns:
            op.drop_column("sellers", "last_polled_at")
