"""add_auto_kiz_queue_interval_hours

Revision ID: 0a1b2c3d4e5f
Revises: f6a7b8c9d0e1
Create Date: 2026-10-15 11:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0a1b2c3d4e5f'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "auto_kiz_queue_interval_hours" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_queue_interval_hours", sa.Integer(), nullable=False, server_default="6")
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "auto_kiz_queue_interval_hours" in columns:
            op.drop_column("sellers", "auto_kiz_queue_interval_hours")
