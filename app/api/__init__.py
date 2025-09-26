from __future__ import annotations

from flask import Blueprint, jsonify, abort
from ..models import Token

api_bp = Blueprint("api", __name__)


@api_bp.get("/tokens")
def list_tokens():
    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).all()
    return jsonify({"items": [t.to_dict() for t in tokens]})


@api_bp.get("/tokens/<symbol>")
def get_token(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    return jsonify(token.to_dict())
