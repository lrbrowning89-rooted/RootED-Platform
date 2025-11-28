"""add is_active to users table

Revision ID: c9b91d8710a9
Revises: 52e37f22079c
Create Date: 2025-11-12 17:53:07.700425

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9b91d8710a9'
down_revision: Union[str, Sequence[str], None] = '52e37f22079c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
