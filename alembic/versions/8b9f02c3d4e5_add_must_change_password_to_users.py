"""add_must_change_password_to_users

Revision ID: 8b9f02c3d4e5
Revises: 7a8e91b2c3d4
Create Date: 2026-08-20 18:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8b9f02c3d4e5'
down_revision: Union[str, None] = '7a8e91b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "users" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("users")]
        if "must_change_password" not in columns:
            op.add_column(
                "users",
                sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default="false")
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "users" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("users")]
        if "must_change_password" in columns:
            op.drop_column("users", "must_change_password")
