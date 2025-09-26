"""creator follow and fee distribution tables

Revision ID: b4dcafee1234
Revises: a9c1e5b9278a
Create Date: 2025-09-26 17:52:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b4dcafee1234'
down_revision = 'a9c1e5b9278a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # creator_follows
    if 'creator_follows' not in insp.get_table_names():
        op.create_table(
            'creator_follows',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('follower_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('creator_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
    existing_ix = {ix['name'] for ix in insp.get_indexes('creator_follows')} if 'creator_follows' in insp.get_table_names() else set()
    existing_uc = {uc['name'] for uc in insp.get_unique_constraints('creator_follows')} if 'creator_follows' in insp.get_table_names() else set()
    if 'ix_creator_follows_follower' not in existing_ix:
        op.create_index('ix_creator_follows_follower', 'creator_follows', ['follower_user_id'], unique=False)
    if 'ix_creator_follows_creator' not in existing_ix:
        op.create_index('ix_creator_follows_creator', 'creator_follows', ['creator_user_id'], unique=False)
    if 'uq_creator_follow' not in existing_uc:
        op.create_unique_constraint('uq_creator_follow', 'creator_follows', ['follower_user_id', 'creator_user_id'])

    # fee_distribution_rules
    if 'fee_distribution_rules' not in insp.get_table_names():
        op.create_table(
            'fee_distribution_rules',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('pool_id', sa.Integer(), sa.ForeignKey('swap_pools.id'), nullable=False),
            sa.Column('creator_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('minter_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('treasury_account', sa.String(length=120), nullable=True),
            sa.Column('bps_creator', sa.Integer(), nullable=False, server_default=sa.text('5000')),
            sa.Column('bps_minter', sa.Integer(), nullable=False, server_default=sa.text('3000')),
            sa.Column('bps_treasury', sa.Integer(), nullable=False, server_default=sa.text('2000')),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    existing_ix = {ix['name'] for ix in insp.get_indexes('fee_distribution_rules')} if 'fee_distribution_rules' in insp.get_table_names() else set()
    if 'ix_fee_rules_pool' not in existing_ix:
        op.create_index('ix_fee_rules_pool', 'fee_distribution_rules', ['pool_id'], unique=True)
    if 'ix_fee_rules_creator' not in existing_ix:
        op.create_index('ix_fee_rules_creator', 'fee_distribution_rules', ['creator_user_id'], unique=False)
    if 'ix_fee_rules_minter' not in existing_ix:
        op.create_index('ix_fee_rules_minter', 'fee_distribution_rules', ['minter_user_id'], unique=False)

    # fee_payouts
    if 'fee_payouts' not in insp.get_table_names():
        op.create_table(
            'fee_payouts',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('pool_id', sa.Integer(), sa.ForeignKey('swap_pools.id'), nullable=False),
            sa.Column('entity', sa.String(length=16), nullable=False),
            sa.Column('asset', sa.String(length=1), nullable=False),
            sa.Column('amount', sa.Numeric(30, 18), nullable=False),
            sa.Column('note', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
    existing_ix = {ix['name'] for ix in insp.get_indexes('fee_payouts')} if 'fee_payouts' in insp.get_table_names() else set()
    if 'ix_fee_payouts_pool' not in existing_ix:
        op.create_index('ix_fee_payouts_pool', 'fee_payouts', ['pool_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_fee_payouts_pool', table_name='fee_payouts')
    op.drop_table('fee_payouts')

    op.drop_index('ix_fee_rules_minter', table_name='fee_distribution_rules')
    op.drop_index('ix_fee_rules_creator', table_name='fee_distribution_rules')
    op.drop_index('ix_fee_rules_pool', table_name='fee_distribution_rules')
    op.drop_table('fee_distribution_rules')

    op.drop_constraint('uq_creator_follow', 'creator_follows', type_='unique')
    op.drop_index('ix_creator_follows_creator', table_name='creator_follows')
    op.drop_index('ix_creator_follows_follower', table_name='creator_follows')
    op.drop_table('creator_follows')
