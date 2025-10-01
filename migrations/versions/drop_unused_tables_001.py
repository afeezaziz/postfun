"""drop_unused_tables

Revision ID: drop_unused_tables_001
Revises: 57a3b01a7ba2
Create Date: 2025-10-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'drop_unused_tables_001'
down_revision = '57a3b01a7ba2'
branch_labels = None
depends_on = None


def upgrade():
    # Drop unused tables
    op.drop_table('watchlist_items')
    op.drop_table('alert_events')
    op.drop_table('alert_rules')
    op.drop_table('account_balances')
    op.drop_table('burn_events')
    op.drop_table('creator_follows')
    op.drop_table('feature_flags')
    op.drop_table('fee_distribution_rules')
    op.drop_table('fee_payouts')
    op.drop_table('ohlc_candles')


def downgrade():
    # Recreate tables if needed
    # Note: This is a simplified recreation - adjust column definitions as needed
    op.create_table('watchlist_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'token_id', name='uq_watchlist_user_token')
    )

    op.create_table('alert_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('condition', sa.String(length=32), nullable=False),
        sa.Column('threshold', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'token_id', 'condition', 'threshold', name='uq_alert_unique')
    )

    op.create_table('alert_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rule_id', sa.Integer(), nullable=False),
        sa.Column('triggered_at', sa.DateTime(), nullable=False),
        sa.Column('price', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.ForeignKeyConstraint(['rule_id'], ['alert_rules.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('account_balances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('asset', sa.String(length=16), nullable=False),
        sa.Column('balance_sats', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'asset', name='uq_balance_user_asset')
    )

    op.create_table('burn_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('stage', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=30, scale=18), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['pool_id'], ['swap_pools.id'], ),
        sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('creator_follows',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('follower_user_id', sa.Integer(), nullable=False),
        sa.Column('creator_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['creator_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['follower_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('follower_user_id', 'creator_user_id', name='uq_creator_follow')
    )

    op.create_table('feature_flags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=255), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key')
    )

    op.create_table('fee_distribution_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('creator_user_id', sa.Integer(), nullable=True),
        sa.Column('minter_user_id', sa.Integer(), nullable=True),
        sa.Column('treasury_account', sa.String(length=120), nullable=True),
        sa.Column('bps_creator', sa.Integer(), nullable=False),
        sa.Column('bps_minter', sa.Integer(), nullable=False),
        sa.Column('bps_treasury', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['creator_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['minter_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['pool_id'], ['swap_pools.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pool_id')
    )

    op.create_table('fee_payouts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('entity', sa.String(length=16), nullable=False),
        sa.Column('asset', sa.String(length=1), nullable=False),
        sa.Column('amount', sa.Numeric(precision=30, scale=18), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['pool_id'], ['swap_pools.id'], ),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('ohlc_candles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('interval', sa.String(length=8), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('o', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('h', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('l', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('c', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('v', sa.Numeric(precision=30, scale=18), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['token_id'], ['tokens.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_id', 'interval', 'ts', name='uq_ohlc_token_interval_ts')
    )