"""add is_active to users

Revision ID: 52e37f22079c
Revises: fbf7d74c2666
Create Date: 2025-11-11 21:19:35.079287

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '52e37f22079c'
down_revision: Union[str, Sequence[str], None] = 'fbf7d74c2666'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from alembic import op
import sqlalchemy as sa

def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"))
    # Optional cleanup: remove the default after backfilling so new inserts must set a value explicitly
    op.execute("UPDATE users SET is_active=1 WHERE is_active IS NULL")

def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("is_active")
