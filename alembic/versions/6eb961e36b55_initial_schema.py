"""initial_schema

Revision ID: 6eb961e36b55
Revises: 
Create Date: 2026-08-19 20:14:56.410545

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6eb961e36b55'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tables are created and ensured by app.database.init_db()
    pass


def downgrade() -> None:
    pass