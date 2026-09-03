"""add_notification_schedule_to_sellers

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-03 16:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "notification_mode" not in columns:
            op.add_column(
                "sellers",
                sa.Column("notification_mode", sa.String(length=32), server_default="instant", nullable=False)
            )
        if "notification_schedule" not in columns:
            op.add_column(
                "sellers",
                sa.Column("notification_schedule", sa.JSON(), server_default="[]", nullable=False)
            )
        if "timezone" not in columns:
            op.add_column(
                "sellers",
                sa.Column("timezone", sa.String(length=64), server_default="Europe/Moscow", nullable=False)
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "timezone" in columns:
            op.drop_column("sellers", "timezone")
        if "notification_schedule" in columns:
            op.drop_column("sellers", "notification_schedule")
        if "notification_mode" in columns:
            op.drop_column("sellers", "notification_mode")
