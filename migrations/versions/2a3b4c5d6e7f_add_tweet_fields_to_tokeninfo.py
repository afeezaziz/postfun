"""Add tweet fields to TokenInfo

Revision ID: 2a3b4c5d6e7f
Revises: 1a2b3c4d5e81
Create Date: 2025-09-29 12:40:00

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers, used by Alembic.
revision = '2a3b4c5d6e7f'
down_revision = '1a2b3c4d5e7f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add tweet columns to token_infos table
    op.add_column('token_infos', sa.Column('tweet_url', sa.String(512), nullable=True))
    op.add_column('token_infos', sa.Column('tweet_content', sa.Text, nullable=True))
    op.add_column('token_infos', sa.Column('tweet_author', sa.String(255), nullable=True))
    op.add_column('token_infos', sa.Column('tweet_created_at', sa.DateTime, nullable=True))


def downgrade() -> None:
    # Remove tweet columns from token_infos table
    op.drop_column('token_infos', 'tweet_created_at')
    op.drop_column('token_infos', 'tweet_author')
    op.drop_column('token_infos', 'tweet_content')
    op.drop_column('token_infos', 'tweet_url')