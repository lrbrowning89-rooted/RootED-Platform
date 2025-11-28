"""add is_active to users table

Revision ID: 047f7d0be488
Revises: c9b91d8710a9
Create Date: 2025-11-12 17:58:38.775368

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '047f7d0be488'
down_revision: Union[str, Sequence[str], None] = 'c9b91d8710a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # For SQLite, it's simplest to use raw SQL for this change
    op.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")


def downgrade():
    # SQLite can't easily drop columns; we'll leave this as a no-op.
    # If you ever really need to downgrade, you'd rebuild the table without this column.
    pass
