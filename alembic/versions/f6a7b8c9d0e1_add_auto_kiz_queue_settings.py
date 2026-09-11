"""add_auto_kiz_queue_settings

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-11 22:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "auto_kiz_queue_enabled" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_queue_enabled", sa.Boolean(), nullable=False, server_default="true")
            )
        if "auto_kiz_queue_hour" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_queue_hour", sa.Integer(), nullable=False, server_default="17")
            )
        if "auto_kiz_queue_minute" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_queue_minute", sa.Integer(), nullable=False, server_default="0")
            )
        if "auto_kiz_auto_sign_server" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_auto_sign_server", sa.Boolean(), nullable=False, server_default="false")
            )
        if "auto_kiz_manager_chat_id" not in columns:
            op.add_column(
                "sellers",
                sa.Column("auto_kiz_manager_chat_id", sa.String(length=100), nullable=True)
            )
        if "last_auto_kiz_queue_at" not in columns:
            op.add_column(
                "sellers",
                sa.Column("last_auto_kiz_queue_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        for col in [
            "last_auto_kiz_queue_at",
            "auto_kiz_manager_chat_id",
            "auto_kiz_auto_sign_server",
            "auto_kiz_queue_minute",
            "auto_kiz_queue_hour",
            "auto_kiz_queue_enabled",
        ]:
            if col in columns:
                op.drop_column("sellers", col)
