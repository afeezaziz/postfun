"""add token flags, moderation fields, and feature_flags table

Revision ID: 0f1e2d3c4b5a
Revises: f1a2b3c4d5e6
Create Date: 2025-09-27 05:49:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0f1e2d3c4b5a'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # tokens: hidden, frozen
    if 'tokens' in insp.get_table_names():
        with op.batch_alter_table('tokens') as batch:
            cols = {c['name'] for c in insp.get_columns('tokens')}
            if 'hidden' not in cols:
                batch.add_column(sa.Column('hidden', sa.Boolean(), nullable=False, server_default=sa.text('0')))
            if 'frozen' not in cols:
                batch.add_column(sa.Column('frozen', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        # drop server_default to keep model-controlled default
        with op.batch_alter_table('tokens') as batch:
            batch.alter_column('hidden', server_default=None)
            batch.alter_column('frozen', server_default=None)

    # token_infos: moderation_status, moderation_notes
    if 'token_infos' in insp.get_table_names():
        with op.batch_alter_table('token_infos') as batch:
            cols = {c['name'] for c in insp.get_columns('token_infos')}
            if 'moderation_status' not in cols:
                batch.add_column(sa.Column('moderation_status', sa.String(length=16), nullable=False, server_default=sa.text("'visible'")))
            if 'moderation_notes' not in cols:
                batch.add_column(sa.Column('moderation_notes', sa.Text(), nullable=True))
        with op.batch_alter_table('token_infos') as batch:
            batch.alter_column('moderation_status', server_default=None)

    # feature_flags
    if 'feature_flags' not in insp.get_table_names():
        op.create_table(
            'feature_flags',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key', sa.String(length=64), nullable=False),
            sa.Column('value', sa.String(length=255), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_feature_flags_key', 'feature_flags', ['key'], unique=True)


def downgrade() -> None:
    try:
        op.drop_index('ix_feature_flags_key', table_name='feature_flags')
        op.drop_table('feature_flags')
    except Exception:
        pass

    try:
        with op.batch_alter_table('token_infos') as batch:
            batch.drop_column('moderation_notes')
            batch.drop_column('moderation_status')
    except Exception:
        pass

    try:
        with op.batch_alter_table('tokens') as batch:
            batch.drop_column('frozen')
            batch.drop_column('hidden')
    except Exception:
        pass
