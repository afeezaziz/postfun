from datetime import datetime
import uuid
from decimal import Decimal
from .extensions import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    pubkey_hex = db.Column(db.String(64), unique=True, nullable=False, index=True)
    npub = db.Column(db.String(120), unique=True, nullable=True, index=True)
    display_name = db.Column(db.String(120), nullable=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    withdraw_frozen = db.Column(db.Boolean, nullable=False, default=False)
    sats = db.Column(db.BigInteger, nullable=False, default=0)  # Balance in millisats
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "pubkey": self.pubkey_hex,
            "npub": self.npub,
            "display_name": self.display_name,
            "created_at": self.created_at.isoformat() + "Z",
        }






class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(120), nullable=False)
    meta = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")


class AuthChallenge(db.Model):
    __tablename__ = "auth_challenges"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    challenge = db.Column(db.String(128), nullable=False)
    user_pubkey_hex = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "challenge": self.challenge,
            "created_at": self.created_at.isoformat() + "Z",
            "expires_at": self.expires_at.isoformat() + "Z",
            "consumed_at": self.consumed_at.isoformat() + "Z" if self.consumed_at else None,
        }

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None


class Token(db.Model):
    __tablename__ = "tokens"

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Numeric(20, 8), nullable=False, default=0)
    market_cap = db.Column(db.Numeric(20, 2), nullable=True)
    change_24h = db.Column(db.Numeric(10, 4), nullable=True)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    frozen = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('ix_tokens_market_cap', 'market_cap'),
        db.Index('ix_tokens_change_24h', 'change_24h'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "name": self.name,
            "price": float(self.price) if self.price is not None else None,
            "market_cap": float(self.market_cap) if self.market_cap is not None else None,
            "change_24h": float(self.change_24h) if self.change_24h is not None else None,
            "created_at": self.created_at.isoformat() + "Z",
        }


class TokenInfo(db.Model):
    __tablename__ = "token_infos"

    id = db.Column(db.Integer, primary_key=True)
    token_id = db.Column(db.Integer, db.ForeignKey("tokens.id"), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=True)
    logo_url = db.Column(db.String(512), nullable=True)
    website = db.Column(db.String(512), nullable=True)
    twitter = db.Column(db.String(512), nullable=True)
    telegram = db.Column(db.String(512), nullable=True)
    discord = db.Column(db.String(512), nullable=True)
    total_supply = db.Column(db.Numeric(30, 18), nullable=True)
    launch_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    launch_at = db.Column(db.DateTime, nullable=True)
    moderation_status = db.Column(db.String(16), nullable=False, default="visible")  # visible|hidden|flagged
    moderation_notes = db.Column(db.Text, nullable=True)
    # Comma-separated categories/tags, e.g. "meme,ai,gaming"
    categories = db.Column(db.String(255), nullable=True)
    # Tweet details for tokens based on tweets
    tweet_url = db.Column(db.String(512), nullable=True)
    tweet_content = db.Column(db.Text, nullable=True)
    tweet_author = db.Column(db.String(255), nullable=True)
    tweet_created_at = db.Column(db.DateTime, nullable=True)

    token = db.relationship("Token")
    launcher = db.relationship("User")

    __table_args__ = (
        db.Index('ix_token_infos_launch_user', 'launch_user_id'),
        db.Index('ix_token_infos_launch_at', 'launch_at'),
    )

    def to_dict(self):
        return {
            "token_id": self.token_id,
            "description": self.description,
            "logo_url": self.logo_url,
            "website": self.website,
            "twitter": self.twitter,
            "telegram": self.telegram,
            "discord": self.discord,
            "total_supply": float(self.total_supply) if self.total_supply is not None else None,
            "launch_user_id": self.launch_user_id,
            "launch_at": self.launch_at.isoformat() + "Z" if self.launch_at else None,
            "moderation_status": self.moderation_status,
            "categories": self.categories,
            "tweet_url": self.tweet_url,
            "tweet_content": self.tweet_content,
            "tweet_author": self.tweet_author,
            "tweet_created_at": self.tweet_created_at.isoformat() + "Z" if self.tweet_created_at else None,
        }




class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    entry_type = db.Column(db.String(32), nullable=False)  # deposit, withdrawal, fee, adjustment
    delta_sats = db.Column(db.BigInteger, nullable=False)  # positive or negative
    ref_type = db.Column(db.String(32), nullable=True)  # invoice, withdrawal
    ref_id = db.Column(db.String(64), nullable=True)
    meta = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    # Add unique constraint to prevent duplicate ledger entries for the same reference
    __table_args__ = (
        db.UniqueConstraint('user_id', 'ref_type', 'ref_id', name='uq_ledger_reference'),
    )


class LightningInvoice(db.Model):
    __tablename__ = "lightning_invoices"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount_sats = db.Column(db.BigInteger, nullable=False)
    memo = db.Column(db.String(255), nullable=True)
    payment_request = db.Column(db.Text, nullable=False)
    payment_hash = db.Column(db.String(128), nullable=False, unique=True, index=True)
    checking_id = db.Column(db.String(128), nullable=True, unique=True)
    provider = db.Column(db.String(16), nullable=False, default="lnbits")
    status = db.Column(db.String(16), nullable=False, default="pending")  # pending, paid, expired, cancelled
    credited = db.Column(db.Boolean, nullable=False, default=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount_sats": int(self.amount_sats),
            "memo": self.memo,
            "payment_request": self.payment_request,
            "payment_hash": self.payment_hash,
            "status": self.status,
            "credited": self.credited,
            "expires_at": self.expires_at.isoformat() + "Z" if self.expires_at else None,
            "paid_at": self.paid_at.isoformat() + "Z" if self.paid_at else None,
            "created_at": self.created_at.isoformat() + "Z",
        }


class LightningWithdrawal(db.Model):
    __tablename__ = "lightning_withdrawals"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount_sats = db.Column(db.BigInteger, nullable=False)
    bolt11 = db.Column(db.Text, nullable=False)
    fee_sats = db.Column(db.BigInteger, nullable=True)
    payment_hash = db.Column(db.String(128), nullable=True, unique=True, index=True)
    checking_id = db.Column(db.String(128), nullable=True, unique=True)
    provider = db.Column(db.String(16), nullable=False, default="lnbits")
    status = db.Column(db.String(16), nullable=False, default="pending")  # pending, confirmed, failed
    processed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount_sats": int(self.amount_sats),
            "fee_sats": int(self.fee_sats) if self.fee_sats is not None else None,
            "status": self.status,
            "processed_at": self.processed_at.isoformat() + "Z" if self.processed_at else None,
            "created_at": self.created_at.isoformat() + "Z",
        }


# ---- AMM: Token balances, virtual pools, trades, burns ----


class TokenBalance(db.Model):
    __tablename__ = "token_balances"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_id = db.Column(db.Integer, db.ForeignKey("tokens.id"), nullable=False, index=True)
    amount = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User")
    token = db.relationship("Token")

    __table_args__ = (
        db.UniqueConstraint("user_id", "token_id", name="uq_token_balance_user_token"),
        db.Index('ix_token_balances_token_user', 'token_id', 'user_id'),
    )

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "token_id": self.token_id,
            "amount": float(self.amount or 0),
            "updated_at": self.updated_at.isoformat() + "Z",
        }


class SwapPool(db.Model):
    __tablename__ = "swap_pools"

    id = db.Column(db.Integer, primary_key=True)
    token_a_id = db.Column(db.Integer, db.ForeignKey("tokens.id"), nullable=False, index=True)
    token_b_id = db.Column(db.Integer, db.ForeignKey("tokens.id"), nullable=False, index=True)
    reserve_a = db.Column(db.Numeric(30, 18), nullable=False, default=0)  # virtual reserve
    reserve_b = db.Column(db.Numeric(30, 18), nullable=False, default=0)  # virtual reserve
    fee_bps_base = db.Column(db.Integer, nullable=False, default=30)  # base fee in bps (e.g., 30 = 0.30%)
    stage = db.Column(db.Integer, nullable=False, default=1)  # 1..4
    # Stage thresholds are cumulative trading volume denominated in token_a (for simplicity)
    stage1_threshold = db.Column(db.Numeric(30, 18), nullable=True)
    stage2_threshold = db.Column(db.Numeric(30, 18), nullable=True)
    stage3_threshold = db.Column(db.Numeric(30, 18), nullable=True)
    cumulative_volume_a = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    cumulative_volume_b = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    burn_token_id = db.Column(db.Integer, db.ForeignKey("tokens.id"), nullable=True)
    burn_stage1_amount = db.Column(db.Numeric(30, 18), nullable=True)
    burn_stage2_amount = db.Column(db.Numeric(30, 18), nullable=True)
    burn_stage3_amount = db.Column(db.Numeric(30, 18), nullable=True)
    burn_stage4_amount = db.Column(db.Numeric(30, 18), nullable=True)
    # Accumulated protocol fees (not part of reserves)
    fee_accum_a = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    fee_accum_b = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    token_a = db.relationship("Token", foreign_keys=[token_a_id])
    token_b = db.relationship("Token", foreign_keys=[token_b_id])
    burn_token = db.relationship("Token", foreign_keys=[burn_token_id])

    def current_fee_bps(self) -> int:
        # Halves at each stage: stage 1: base, 2: base/2, 3: base/4, 4: base/8
        divisor = 2 ** max(0, int(self.stage or 1) - 1)
        return max(1, int(self.fee_bps_base) // int(divisor))

    def to_dict(self):
        return {
            "id": self.id,
            "token_a_id": self.token_a_id,
            "token_b_id": self.token_b_id,
            "reserve_a": float(self.reserve_a or 0),
            "reserve_b": float(self.reserve_b or 0),
            "fee_bps": self.current_fee_bps(),
            "stage": int(self.stage or 1),
            "cumulative_volume_a": float(self.cumulative_volume_a or 0),
            "cumulative_volume_b": float(self.cumulative_volume_b or 0),
        }


class SwapTrade(db.Model):
    __tablename__ = "swap_trades"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pool_id = db.Column(db.Integer, db.ForeignKey("swap_pools.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    side = db.Column(db.String(8), nullable=False)  # 'AtoB' or 'BtoA'
    amount_in = db.Column(db.Numeric(30, 18), nullable=False)
    amount_out = db.Column(db.Numeric(30, 18), nullable=False)
    fee_paid = db.Column(db.Numeric(30, 18), nullable=False, default=0)
    stage = db.Column(db.Integer, nullable=False)
    burn_amount = db.Column(db.Numeric(30, 18), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    pool = db.relationship("SwapPool")
    user = db.relationship("User")

    __table_args__ = (
        db.Index('ix_swap_trades_pool_created', 'pool_id', 'created_at'),
        db.Index('ix_swap_trades_created', 'created_at'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "pool_id": self.pool_id,
            "user_id": self.user_id,
            "side": self.side,
            "amount_in": float(self.amount_in),
            "amount_out": float(self.amount_out),
            "fee_paid": float(self.fee_paid),
            "stage": int(self.stage),
            "burn_amount": float(self.burn_amount) if self.burn_amount is not None else None,
            "created_at": self.created_at.isoformat() + "Z",
        }




class IdempotencyKey(db.Model):
    __tablename__ = "idempotency_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    scope = db.Column(db.String(64), nullable=False)  # e.g., 'lightning_deposit', 'lightning_withdraw'
    key = db.Column(db.String(128), nullable=False)
    ref_type = db.Column(db.String(32), nullable=True)  # 'invoice' | 'withdrawal'
    ref_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("user_id", "scope", "key", name="uq_idempo_user_scope_key"),
        db.Index('ix_idempo_scope_key', 'scope', 'key'),
    )


class ProviderLog(db.Model):
    __tablename__ = "provider_logs"

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False, default="lnbits")
    action = db.Column(db.String(64), nullable=False)  # 'create_invoice' | 'get_status' | 'pay_invoice'
    request_payload = db.Column(db.Text, nullable=True)
    response_status = db.Column(db.Integer, nullable=True)
    response_payload = db.Column(db.Text, nullable=True)
    success = db.Column(db.Boolean, nullable=False, default=False)
    ref_type = db.Column(db.String(32), nullable=True)  # 'invoice' | 'withdrawal'
    ref_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('ix_provider_logs_action_created', 'action', 'created_at'),
        db.Index('ix_provider_logs_ref', 'ref_type', 'ref_id'),
    )


class TwitterUser(db.Model):
    __tablename__ = "twitter_users"

    id = db.Column(db.Integer, primary_key=True)
    twitter_user_id = db.Column(db.BigInteger, unique=True, nullable=False, index=True)
    username = db.Column(db.String(64), nullable=False, index=True)
    display_name = db.Column(db.String(128), nullable=True)
    description = db.Column(db.Text, nullable=True)
    profile_image_url = db.Column(db.String(512), nullable=True)
    followers_count = db.Column(db.Integer, nullable=True)
    following_count = db.Column(db.Integer, nullable=True)
    tweet_count = db.Column(db.Integer, nullable=True)
    verified = db.Column(db.Boolean, nullable=True, default=False)
    location = db.Column(db.String(256), nullable=True)
    website = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    posts = db.relationship("TwitterPost", backref=db.backref("user", lazy=True), cascade="all, delete-orphan")

    __table_args__ = (
        db.Index('ix_twitter_users_verified', 'verified'),
        db.Index('ix_twitter_users_followers', 'followers_count'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "twitter_user_id": int(self.twitter_user_id),
            "username": self.username,
            "display_name": self.display_name,
            "description": self.description,
            "profile_image_url": self.profile_image_url,
            "followers_count": self.followers_count,
            "following_count": self.following_count,
            "tweet_count": self.tweet_count,
            "verified": self.verified,
            "location": self.location,
            "website": self.website,
            "created_at": self.created_at.isoformat() + "Z",
        }


class TwitterPost(db.Model):
    __tablename__ = "twitter_posts"

    id = db.Column(db.Integer, primary_key=True)
    twitter_post_id = db.Column(db.BigInteger, unique=True, nullable=False, index=True)
    twitter_user_id = db.Column(db.Integer, db.ForeignKey("twitter_users.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    post_type = db.Column(db.String(32), nullable=False, default="tweet")  # tweet, retweet, reply
    reply_to_post_id = db.Column(db.BigInteger, nullable=True)
    retweet_of_post_id = db.Column(db.BigInteger, nullable=True)
    media_urls = db.Column(db.Text, nullable=True)  # JSON array of media URLs
    hashtags = db.Column(db.Text, nullable=True)  # JSON array of hashtags
    mentions = db.Column(db.Text, nullable=True)  # JSON array of mentioned user IDs
    like_count = db.Column(db.Integer, nullable=False, default=0)
    retweet_count = db.Column(db.Integer, nullable=False, default=0)
    reply_count = db.Column(db.Integer, nullable=False, default=0)
    quote_count = db.Column(db.Integer, nullable=False, default=0)
    view_count = db.Column(db.Integer, nullable=True)
    language = db.Column(db.String(8), nullable=True)
    posted_at = db.Column(db.DateTime, nullable=False)
    collected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('ix_twitter_posts_posted_at', 'posted_at'),
        db.Index('ix_twitter_posts_collected_at', 'collected_at'),
        db.Index('ix_twitter_posts_post_type', 'post_type'),
        db.Index('ix_twitter_posts_engagement', 'like_count', 'retweet_count'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "twitter_post_id": int(self.twitter_post_id),
            "twitter_user_id": self.twitter_user_id,
            "content": self.content,
            "post_type": self.post_type,
            "reply_to_post_id": int(self.reply_to_post_id) if self.reply_to_post_id else None,
            "retweet_of_post_id": int(self.retweet_of_post_id) if self.retweet_of_post_id else None,
            "media_urls": self.media_urls,
            "hashtags": self.hashtags,
            "mentions": self.mentions,
            "like_count": self.like_count,
            "retweet_count": self.retweet_count,
            "reply_count": self.reply_count,
            "quote_count": self.quote_count,
            "view_count": self.view_count,
            "language": self.language,
            "posted_at": self.posted_at.isoformat() + "Z",
            "collected_at": self.collected_at.isoformat() + "Z",
            "created_at": self.created_at.isoformat() + "Z",
        }


class UserTwitterConnection(db.Model):
    __tablename__ = "user_twitter_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    twitter_user_id = db.Column(db.Integer, db.ForeignKey("twitter_users.id"), nullable=False, unique=True, index=True)
    connected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    verified = db.Column(db.Boolean, nullable=False, default=False)
    display_preference = db.Column(db.String(32), nullable=False, default="npub")  # 'npub' or 'twitter'

    user = db.relationship("User", backref=db.backref("twitter_connection", uselist=False, cascade="all, delete-orphan"))
    twitter_user = db.relationship("TwitterUser", backref=db.backref("user_connection", uselist=False, cascade="all, delete-orphan"))

    __table_args__ = (
        db.Index('ix_user_twitter_connections_user', 'user_id'),
        db.Index('ix_user_twitter_connections_twitter_user', 'twitter_user_id'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "twitter_user_id": self.twitter_user_id,
            "connected_at": self.connected_at.isoformat() + "Z",
            "verified": self.verified,
            "display_preference": self.display_preference,
        }


