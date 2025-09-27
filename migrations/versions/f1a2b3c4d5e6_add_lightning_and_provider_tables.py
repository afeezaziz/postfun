"""add lightning/account/ledger/idempotency/provider tables and withdraw_frozen

Revision ID: f1a2b3c4d5e6
Revises: e5302935e8ee
Create Date: 2025-09-27 04:45:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e5302935e8ee'
branch_labels = None
depends_on = None


def table_exists(insp, name: str) -> bool:
    try:
        return name in insp.get_table_names()
    except Exception:
        return False


def column_exists(insp, table: str, column: str) -> bool:
    try:
        cols = [c['name'] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def index_exists(insp, table: str, index_name: str) -> bool:
    try:
        idx = {ix['name'] for ix in insp.get_indexes(table)}
        return index_name in idx
    except Exception:
        return False


def unique_exists(insp, table: str, name: str) -> bool:
    try:
        uqs = {uc['name'] for uc in insp.get_unique_constraints(table)}
        return name in uqs
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # users: withdraw_frozen
    if not column_exists(insp, 'users', 'withdraw_frozen'):
        with op.batch_alter_table('users') as batch:
            batch.add_column(sa.Column('withdraw_frozen', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        # remove server_default to keep model-controlled default
        with op.batch_alter_table('users') as batch:
            batch.alter_column('withdraw_frozen', server_default=None)

    # account_balances
    if not table_exists(insp, 'account_balances'):
        op.create_table(
            'account_balances',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('asset', sa.String(length=16), nullable=False),
            sa.Column('balance_sats', sa.BigInteger(), nullable=False, server_default=text('0')),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_unique_constraint('uq_balance_user_asset', 'account_balances', ['user_id', 'asset'])
        op.create_index('ix_account_balances_user', 'account_balances', ['user_id'], unique=False)

    # ledger_entries
    if not table_exists(insp, 'ledger_entries'):
        op.create_table(
            'ledger_entries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('entry_type', sa.String(length=32), nullable=False),
            sa.Column('delta_sats', sa.BigInteger(), nullable=False),
            sa.Column('ref_type', sa.String(length=32), nullable=True),
            sa.Column('ref_id', sa.String(length=64), nullable=True),
            sa.Column('meta', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_ledger_entries_user_created', 'ledger_entries', ['user_id', 'created_at'], unique=False)

    # lightning_invoices
    if not table_exists(insp, 'lightning_invoices'):
        op.create_table(
            'lightning_invoices',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('amount_sats', sa.BigInteger(), nullable=False),
            sa.Column('memo', sa.String(length=255), nullable=True),
            sa.Column('payment_request', sa.Text(), nullable=False),
            sa.Column('payment_hash', sa.String(length=128), nullable=False),
            sa.Column('checking_id', sa.String(length=128), nullable=True),
            sa.Column('provider', sa.String(length=16), nullable=False, server_default=text("'lnbits'")),
            sa.Column('status', sa.String(length=16), nullable=False, server_default=text("'pending'")),
            sa.Column('credited', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('paid_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('payment_hash', name='uq_lightning_invoices_payment_hash'),
            sa.UniqueConstraint('checking_id', name='uq_lightning_invoices_checking_id'),
        )
        op.create_index('ix_lightning_invoices_user', 'lightning_invoices', ['user_id'], unique=False)
        op.create_index('ix_lightning_invoices_payment_hash', 'lightning_invoices', ['payment_hash'], unique=True)

    # lightning_withdrawals
    if not table_exists(insp, 'lightning_withdrawals'):
        op.create_table(
            'lightning_withdrawals',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('amount_sats', sa.BigInteger(), nullable=False),
            sa.Column('bolt11', sa.Text(), nullable=False),
            sa.Column('fee_sats', sa.BigInteger(), nullable=True),
            sa.Column('payment_hash', sa.String(length=128), nullable=True),
            sa.Column('checking_id', sa.String(length=128), nullable=True),
            sa.Column('provider', sa.String(length=16), nullable=False, server_default=text("'lnbits'")),
            sa.Column('status', sa.String(length=16), nullable=False, server_default=text("'pending'")),
            sa.Column('processed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('payment_hash', name='uq_lightning_withdrawals_payment_hash'),
            sa.UniqueConstraint('checking_id', name='uq_lightning_withdrawals_checking_id'),
        )
        op.create_index('ix_lightning_withdrawals_user', 'lightning_withdrawals', ['user_id'], unique=False)
        op.create_index('ix_lightning_withdrawals_payment_hash', 'lightning_withdrawals', ['payment_hash'], unique=True)

    # idempotency_keys
    if not table_exists(insp, 'idempotency_keys'):
        op.create_table(
            'idempotency_keys',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('scope', sa.String(length=64), nullable=False),
            sa.Column('key', sa.String(length=128), nullable=False),
            sa.Column('ref_type', sa.String(length=32), nullable=True),
            sa.Column('ref_id', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_unique_constraint('uq_idempo_user_scope_key', 'idempotency_keys', ['user_id', 'scope', 'key'])
        op.create_index('ix_idempo_scope_key', 'idempotency_keys', ['scope', 'key'], unique=False)

    # provider_logs
    if not table_exists(insp, 'provider_logs'):
        op.create_table(
            'provider_logs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('provider', sa.String(length=32), nullable=False),
            sa.Column('action', sa.String(length=64), nullable=False),
            sa.Column('request_payload', sa.Text(), nullable=True),
            sa.Column('response_status', sa.Integer(), nullable=True),
            sa.Column('response_payload', sa.Text(), nullable=True),
            sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('ref_type', sa.String(length=32), nullable=True),
            sa.Column('ref_id', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_provider_logs_action_created', 'provider_logs', ['action', 'created_at'], unique=False)
        op.create_index('ix_provider_logs_ref', 'provider_logs', ['ref_type', 'ref_id'], unique=False)


def downgrade() -> None:
    # drop in reverse order
    try:
        op.drop_index('ix_provider_logs_ref', table_name='provider_logs')
        op.drop_index('ix_provider_logs_action_created', table_name='provider_logs')
        op.drop_table('provider_logs')
    except Exception:
        pass

    try:
        op.drop_index('ix_idempo_scope_key', table_name='idempotency_keys')
        op.drop_constraint('uq_idempo_user_scope_key', 'idempotency_keys', type_='unique')
        op.drop_table('idempotency_keys')
    except Exception:
        pass

    try:
        op.drop_index('ix_lightning_withdrawals_payment_hash', table_name='lightning_withdrawals')
        op.drop_index('ix_lightning_withdrawals_user', table_name='lightning_withdrawals')
        op.drop_table('lightning_withdrawals')
    except Exception:
        pass

    try:
        op.drop_index('ix_lightning_invoices_payment_hash', table_name='lightning_invoices')
        op.drop_index('ix_lightning_invoices_user', table_name='lightning_invoices')
        op.drop_table('lightning_invoices')
    except Exception:
        pass

    try:
        op.drop_index('ix_ledger_entries_user_created', table_name='ledger_entries')
        op.drop_table('ledger_entries')
    except Exception:
        pass

    try:
        op.drop_index('ix_account_balances_user', table_name='account_balances')
        op.drop_constraint('uq_balance_user_asset', 'account_balances', type_='unique')
        op.drop_table('account_balances')
    except Exception:
        pass

    try:
        with op.batch_alter_table('users') as batch:
            batch.drop_column('withdraw_frozen')
    except Exception:
        pass
