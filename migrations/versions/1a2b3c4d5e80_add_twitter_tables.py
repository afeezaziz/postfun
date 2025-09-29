"""Add twitter_users and twitter_posts tables

Revision ID: 1a2b3c4d5e80
Revises: f1a2b3c4d5e6
Create Date: 2025-09-29 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e80'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # twitter_users table
    if 'twitter_users' not in insp.get_table_names():
        op.create_table(
            'twitter_users',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('twitter_user_id', sa.BigInteger(), unique=True, nullable=False),
            sa.Column('username', sa.String(64), nullable=False, index=True),
            sa.Column('display_name', sa.String(128), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('profile_image_url', sa.String(512), nullable=True),
            sa.Column('followers_count', sa.Integer(), nullable=True),
            sa.Column('following_count', sa.Integer(), nullable=True),
            sa.Column('tweet_count', sa.Integer(), nullable=True),
            sa.Column('verified', sa.Boolean(), nullable=True, default=False),
            sa.Column('location', sa.String(256), nullable=True),
            sa.Column('website', sa.String(512), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, default=datetime.utcnow),
            sa.Column('updated_at', sa.DateTime(), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow),
        )

    existing_ix = {ix['name'] for ix in insp.get_indexes('twitter_users')} if 'twitter_users' in insp.get_table_names() else set()
    if 'ix_twitter_users_twitter_user_id' not in existing_ix:
        op.create_index('ix_twitter_users_twitter_user_id', 'twitter_users', ['twitter_user_id'], unique=True)
    if 'ix_twitter_users_username' not in existing_ix:
        op.create_index('ix_twitter_users_username', 'twitter_users', ['username'], unique=False)
    if 'ix_twitter_users_verified' not in existing_ix:
        op.create_index('ix_twitter_users_verified', 'twitter_users', ['verified'], unique=False)
    if 'ix_twitter_users_followers' not in existing_ix:
        op.create_index('ix_twitter_users_followers', 'twitter_users', ['followers_count'], unique=False)

    # twitter_posts table
    if 'twitter_posts' not in insp.get_table_names():
        op.create_table(
            'twitter_posts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('twitter_post_id', sa.BigInteger(), unique=True, nullable=False),
            sa.Column('twitter_user_id', sa.Integer(), sa.ForeignKey('twitter_users.id'), nullable=False, index=True),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('post_type', sa.String(32), nullable=False, default='tweet'),  # tweet, retweet, reply
            sa.Column('reply_to_post_id', sa.BigInteger(), nullable=True),
            sa.Column('retweet_of_post_id', sa.BigInteger(), nullable=True),
            sa.Column('media_urls', sa.Text(), nullable=True),  # JSON array of media URLs
            sa.Column('hashtags', sa.Text(), nullable=True),  # JSON array of hashtags
            sa.Column('mentions', sa.Text(), nullable=True),  # JSON array of mentioned user IDs
            sa.Column('like_count', sa.Integer(), nullable=False, default=0),
            sa.Column('retweet_count', sa.Integer(), nullable=False, default=0),
            sa.Column('reply_count', sa.Integer(), nullable=False, default=0),
            sa.Column('quote_count', sa.Integer(), nullable=False, default=0),
            sa.Column('view_count', sa.Integer(), nullable=True),
            sa.Column('language', sa.String(8), nullable=True),
            sa.Column('posted_at', sa.DateTime(), nullable=False),
            sa.Column('collected_at', sa.DateTime(), nullable=False, default=datetime.utcnow),
            sa.Column('created_at', sa.DateTime(), nullable=False, default=datetime.utcnow),
            sa.Column('updated_at', sa.DateTime(), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow),
        )

    existing_ix = {ix['name'] for ix in insp.get_indexes('twitter_posts')} if 'twitter_posts' in insp.get_table_names() else set()
    if 'ix_twitter_posts_twitter_post_id' not in existing_ix:
        op.create_index('ix_twitter_posts_twitter_post_id', 'twitter_posts', ['twitter_post_id'], unique=True)
    if 'ix_twitter_posts_twitter_user_id' not in existing_ix:
        op.create_index('ix_twitter_posts_twitter_user_id', 'twitter_posts', ['twitter_user_id'], unique=False)
    if 'ix_twitter_posts_posted_at' not in existing_ix:
        op.create_index('ix_twitter_posts_posted_at', 'twitter_posts', ['posted_at'], unique=False)
    if 'ix_twitter_posts_collected_at' not in existing_ix:
        op.create_index('ix_twitter_posts_collected_at', 'twitter_posts', ['collected_at'], unique=False)
    if 'ix_twitter_posts_post_type' not in existing_ix:
        op.create_index('ix_twitter_posts_post_type', 'twitter_posts', ['post_type'], unique=False)
    if 'ix_twitter_posts_engagement' not in existing_ix:
        op.create_index('ix_twitter_posts_engagement', 'twitter_posts', ['like_count', 'retweet_count'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_twitter_posts_engagement', table_name='twitter_posts')
    op.drop_index('ix_twitter_posts_post_type', table_name='twitter_posts')
    op.drop_index('ix_twitter_posts_collected_at', table_name='twitter_posts')
    op.drop_index('ix_twitter_posts_posted_at', table_name='twitter_posts')
    op.drop_index('ix_twitter_posts_twitter_user_id', table_name='twitter_posts')
    op.drop_index('ix_twitter_posts_twitter_post_id', table_name='twitter_posts')
    op.drop_table('twitter_posts')

    op.drop_index('ix_twitter_users_followers', table_name='twitter_users')
    op.drop_index('ix_twitter_users_verified', table_name='twitter_users')
    op.drop_index('ix_twitter_users_username', table_name='twitter_users')
    op.drop_index('ix_twitter_users_twitter_user_id', table_name='twitter_users')
    op.drop_table('twitter_users')