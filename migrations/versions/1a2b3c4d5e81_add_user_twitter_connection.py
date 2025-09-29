"""Add user_twitter_connection table

Revision ID: 1a2b3c4d5e81
Revises: 1a2b3c4d5e80
Create Date: 2025-09-29 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e81'
down_revision = '1a2b3c4d5e80'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # user_twitter_connections table
    if 'user_twitter_connections' not in insp.get_table_names():
        op.create_table(
            'user_twitter_connections',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, unique=True, index=True),
            sa.Column('twitter_user_id', sa.Integer(), sa.ForeignKey('twitter_users.id'), nullable=False, unique=True, index=True),
            sa.Column('connected_at', sa.DateTime(), nullable=False, default=datetime.utcnow),
            sa.Column('verified', sa.Boolean(), nullable=False, default=False),
            sa.Column('display_preference', sa.String(32), nullable=False, default='npub'),  # 'npub' or 'twitter'
        )

    existing_ix = {ix['name'] for ix in insp.get_indexes('user_twitter_connections')} if 'user_twitter_connections' in insp.get_table_names() else set()
    if 'ix_user_twitter_connections_user' not in existing_ix:
        op.create_index('ix_user_twitter_connections_user', 'user_twitter_connections', ['user_id'], unique=True)
    if 'ix_user_twitter_connections_twitter_user' not in existing_ix:
        op.create_index('ix_user_twitter_connections_twitter_user', 'user_twitter_connections', ['twitter_user_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_user_twitter_connections_twitter_user', table_name='user_twitter_connections')
    op.drop_index('ix_user_twitter_connections_user', table_name='user_twitter_connections')
    op.drop_table('user_twitter_connections')