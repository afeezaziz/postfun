from datetime import datetime
import uuid
from .extensions import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    pubkey_hex = db.Column(db.String(64), unique=True, nullable=False, index=True)
    npub = db.Column(db.String(120), unique=True, nullable=True, index=True)
    display_name = db.Column(db.String(120), nullable=True)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

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
