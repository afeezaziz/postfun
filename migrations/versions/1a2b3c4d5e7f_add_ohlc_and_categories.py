"""add ohlc_candles table and token_info.categories

Revision ID: 1a2b3c4d5e7f
Revises: 0f1e2d3c4b5a
Create Date: 2025-09-27 08:35:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e7f'
down_revision = '0f1e2d3c4b5a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # token_infos: categories column
    if 'token_infos' in insp.get_table_names():
        cols = {c['name'] for c in insp.get_columns('token_infos')}
        if 'categories' not in cols:
            with op.batch_alter_table('token_infos') as batch:
                batch.add_column(sa.Column('categories', sa.String(length=255), nullable=True))

    # ohlc_candles table
    if 'ohlc_candles' not in insp.get_table_names():
        op.create_table(
            'ohlc_candles',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('token_id', sa.Integer(), nullable=False),
            sa.Column('interval', sa.String(length=8), nullable=False),
            sa.Column('ts', sa.DateTime(), nullable=False),
            sa.Column('o', sa.Numeric(20, 8), nullable=False),
            sa.Column('h', sa.Numeric(20, 8), nullable=False),
            sa.Column('l', sa.Numeric(20, 8), nullable=False),
            sa.Column('c', sa.Numeric(20, 8), nullable=False),
            sa.Column('v', sa.Numeric(30, 18), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        # Simple indexes/constraints matching the model
        op.create_index('ix_ohlc_token_interval_ts', 'ohlc_candles', ['token_id', 'interval', 'ts'], unique=False)
        op.create_unique_constraint('uq_ohlc_token_interval_ts', 'ohlc_candles', ['token_id', 'interval', 'ts'])


def downgrade() -> None:
    try:
        op.drop_constraint('uq_ohlc_token_interval_ts', 'ohlc_candles', type_='unique')
    except Exception:
        pass
    try:
        op.drop_index('ix_ohlc_token_interval_ts', table_name='ohlc_candles')
    except Exception:
        pass
    try:
        op.drop_table('ohlc_candles')
    except Exception:
        pass
    try:
        with op.batch_alter_table('token_infos') as batch:
            batch.drop_column('categories')
    except Exception:
        pass
