"""add_archive_reminder_time_to_sellers

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-08 21:35:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "archive_reminder_hour" not in columns:
            op.add_column(
                "sellers",
                sa.Column("archive_reminder_hour", sa.Integer(), nullable=False, server_default="14")
            )
        if "archive_reminder_minute" not in columns:
            op.add_column(
                "sellers",
                sa.Column("archive_reminder_minute", sa.Integer(), nullable=False, server_default="0")
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "archive_reminder_minute" in columns:
            op.drop_column("sellers", "archive_reminder_minute")
        if "archive_reminder_hour" in columns:
            op.drop_column("sellers", "archive_reminder_hour")
