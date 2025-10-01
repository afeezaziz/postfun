"""add_sats_column_to_users

Revision ID: 164f3c9c8e79
Revises: 57a3b01a7ba2
Create Date: 2025-10-01 12:28:47.858250

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '164f3c9c8e79'
down_revision = '57a3b01a7ba2'
branch_labels = None
depends_on = None


def upgrade():
    # Add sats column to users table
    op.add_column('users', sa.Column('sats', sa.BigInteger(), nullable=False, server_default='0'))


def downgrade():
    # Remove sats column from users table
    op.drop_column('users', 'sats')
