"""add_cz_rejection_and_doc_status_to_orders

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-04 22:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "orders" in tables:
        columns = [c["name"] for c in inspector.get_columns("orders")]
        if "cz_rejection_reason" not in columns:
            op.add_column(
                "orders",
                sa.Column("cz_rejection_reason", sa.Text(), nullable=True)
            )
        if "cz_doc_status" not in columns:
            op.add_column(
                "orders",
                sa.Column("cz_doc_status", sa.String(length=100), nullable=True)
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "orders" in tables:
        columns = [c["name"] for c in inspector.get_columns("orders")]
        if "cz_doc_status" in columns:
            op.drop_column("orders", "cz_doc_status")
        if "cz_rejection_reason" in columns:
            op.drop_column("orders", "cz_rejection_reason")
