"""add_kiz_signature_batches_and_reminders

Revision ID: a1b2c3d4e5f6
Revises: 9c0a1b2c3d4e
Create Date: 2026-08-27 15:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9c0a1b2c3d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # 1. Update sellers table
    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "archive_reminder_enabled" not in columns:
            op.add_column("sellers", sa.Column("archive_reminder_enabled", sa.Boolean(), server_default="true", nullable=False))
        if "archive_reminder_days" not in columns:
            op.add_column("sellers", sa.Column("archive_reminder_days", sa.Integer(), server_default="2", nullable=False))
        if "last_archive_uploaded_at" not in columns:
            op.add_column("sellers", sa.Column("last_archive_uploaded_at", sa.DateTime(timezone=True), nullable=True))
        if "last_archive_reminder_sent_at" not in columns:
            op.add_column("sellers", sa.Column("last_archive_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))

    # 2. Create kiz_signature_batches table if not exists
    if "kiz_signature_batches" not in tables:
        op.create_table(
            "kiz_signature_batches",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("seller_id", sa.String(length=36), sa.ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("filename", sa.String(length=255), nullable=False, server_default="archive.xlsx"),
            sa.Column("source", sa.String(length=50), nullable=False, server_default="telegram"),
            sa.Column("status", sa.Enum("PENDING_SIGNATURE", "PROCESSING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", name="batchstatus"), nullable=False, server_default="PENDING_SIGNATURE", index=True),
            sa.Column("sales_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("returns_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("already_withdrawn_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("data_payload", sa.JSON(), nullable=False),
            sa.Column("submission_results", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("signed_by", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, index=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "kiz_signature_batches" in tables:
        op.drop_table("kiz_signature_batches")

    if "sellers" in tables:
        columns = [c["name"] for c in inspector.get_columns("sellers")]
        if "last_archive_reminder_sent_at" in columns:
            op.drop_column("sellers", "last_archive_reminder_sent_at")
        if "last_archive_uploaded_at" in columns:
            op.drop_column("sellers", "last_archive_uploaded_at")
        if "archive_reminder_days" in columns:
            op.drop_column("sellers", "archive_reminder_days")
        if "archive_reminder_enabled" in columns:
            op.drop_column("sellers", "archive_reminder_enabled")
