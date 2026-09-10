"""add_product_cards_table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-09 21:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "product_cards" not in tables:
        op.create_table(
            "product_cards",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("seller_id", sa.String(length=36), sa.ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("good_id", sa.BigInteger(), nullable=True),
            sa.Column("gtin", sa.String(length=14), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("brand", sa.String(length=255), nullable=True),
            sa.Column("tnved", sa.String(length=20), nullable=True),
            sa.Column("category_id", sa.Integer(), nullable=True),
            sa.Column("category_name", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
            sa.Column("good_mark_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("good_turn_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_tech_gtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_set", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("feed_id", sa.BigInteger(), nullable=True),
            sa.Column("feed_status", sa.String(length=50), nullable=True),
            sa.Column("attributes", sa.JSON(), nullable=True, server_default="[]"),
            sa.Column("images", sa.JSON(), nullable=True, server_default="[]"),
            sa.Column("error_details", sa.JSON(), nullable=True),
            sa.Column("raw_xml", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_product_cards_id", "product_cards", ["id"])
        op.create_index("ix_product_cards_seller_id", "product_cards", ["seller_id"])
        op.create_index("ix_product_cards_good_id", "product_cards", ["good_id"])
        op.create_index("ix_product_cards_gtin", "product_cards", ["gtin"])
        op.create_index("ix_product_cards_status", "product_cards", ["status"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "product_cards" in tables:
        op.drop_index("ix_product_cards_status", table_name="product_cards")
        op.drop_index("ix_product_cards_gtin", table_name="product_cards")
        op.drop_index("ix_product_cards_good_id", table_name="product_cards")
        op.drop_index("ix_product_cards_seller_id", table_name="product_cards")
        op.drop_index("ix_product_cards_id", table_name="product_cards")
        op.drop_table("product_cards")
