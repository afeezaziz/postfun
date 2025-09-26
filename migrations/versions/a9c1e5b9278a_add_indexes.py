"""add indexes for tokens, swap_trades, token_balances

Revision ID: a9c1e5b9278a
Revises: 
Create Date: 2025-09-26 17:25:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a9c1e5b9278a'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tokens
    try:
        op.create_index('ix_tokens_market_cap', 'tokens', ['market_cap'], unique=False)
    except Exception:
        pass
    try:
        op.create_index('ix_tokens_change_24h', 'tokens', ['change_24h'], unique=False)
    except Exception:
        pass

    # swap_trades
    try:
        op.create_index('ix_swap_trades_pool_created', 'swap_trades', ['pool_id', 'created_at'], unique=False)
    except Exception:
        pass
    try:
        op.create_index('ix_swap_trades_created', 'swap_trades', ['created_at'], unique=False)
    except Exception:
        pass

    # token_balances
    try:
        op.create_index('ix_token_balances_token_user', 'token_balances', ['token_id', 'user_id'], unique=False)
    except Exception:
        pass


def downgrade() -> None:
    # token_balances
    try:
        op.drop_index('ix_token_balances_token_user', table_name='token_balances')
    except Exception:
        pass

    # swap_trades
    try:
        op.drop_index('ix_swap_trades_created', table_name='swap_trades')
    except Exception:
        pass
    try:
        op.drop_index('ix_swap_trades_pool_created', table_name='swap_trades')
    except Exception:
        pass

    # tokens
    try:
        op.drop_index('ix_tokens_change_24h', table_name='tokens')
    except Exception:
        pass
    try:
        op.drop_index('ix_tokens_market_cap', table_name='tokens')
    except Exception:
        pass
