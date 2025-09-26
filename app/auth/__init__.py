from __future__ import annotations

import json
import secrets
import os
from datetime import datetime, timedelta
from typing import Any, Dict

from flask import Blueprint, jsonify, request, current_app, g, make_response

from ..extensions import db, limiter
from ..models import User, AuthChallenge
from ..utils.nostr import validate_login_event, hex_to_npub
from ..utils.jwt_utils import create_jwt, require_auth


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/challenge", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_AUTH", "10 per minute"))
def create_challenge():
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    # Optional pre-bind to a pubkey
    user_pubkey_hex: str | None = None
    npub = data.get("npub")
    pubkey_hex = data.get("pubkey")
    if isinstance(pubkey_hex, str):
        user_pubkey_hex = pubkey_hex.lower()
    # we deliberately avoid converting npub here to keep this endpoint simple

    challenge_str = secrets.token_urlsafe(32)
    ttl = int(current_app.config.get("AUTH_CHALLENGE_TTL", 600))
    expires_at = datetime.utcnow() + timedelta(seconds=ttl)

    row = AuthChallenge(
        challenge=challenge_str,
        user_pubkey_hex=user_pubkey_hex,
        expires_at=expires_at,
    )
    db.session.add(row)
    db.session.commit()

    return jsonify(
        {
            "challenge_id": row.id,
            "challenge": challenge_str,
            "expires_at": expires_at.isoformat() + "Z",
            "ttl_seconds": ttl,
        }
    ), 201


@auth_bp.route("/verify", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_AUTH", "10 per minute"))
def verify_login():
    body: Dict[str, Any] = request.get_json(force=True)
    event = body.get("event")
    if not isinstance(event, dict):
        return jsonify({"error": "invalid_event"}), 400

    # Extract challenge_id from event content first
    try:
        content_obj = json.loads(event.get("content", "{}"))
    except Exception:
        return jsonify({"error": "invalid_event_content"}), 400

    challenge_id = content_obj.get("challenge_id") or body.get("challenge_id")
    if not isinstance(challenge_id, str):
        return jsonify({"error": "missing_challenge_id"}), 400

    row = db.session.get(AuthChallenge, challenge_id)
    if not row:
        return jsonify({"error": "challenge_not_found"}), 404
    if row.is_expired:
        return jsonify({"error": "challenge_expired"}), 400
    if row.is_consumed:
        return jsonify({"error": "challenge_already_used"}), 400

    ok, pub_hex, content = validate_login_event(event, expected_challenge_id=row.id, expected_challenge=row.challenge)
    if not ok or not pub_hex:
        return jsonify({"error": "invalid_signature_or_payload"}), 400

    # Upsert user
    user = User.query.filter_by(pubkey_hex=pub_hex.lower()).first()
    if not user:
        try:
            try:
                computed_npub = hex_to_npub(pub_hex)
            except Exception:
                computed_npub = None
            user = User(pubkey_hex=pub_hex.lower(), npub=computed_npub)
            db.session.add(user)
            db.session.flush()  # get user.id
        except Exception:
            db.session.rollback()
            # Try refetch (race)
            user = User.query.filter_by(pubkey_hex=pub_hex.lower()).first()
            if not user:
                return jsonify({"error": "failed_to_create_user"}), 500

    # Consume challenge
    row.consumed_at = datetime.utcnow()
    db.session.add(row)
    db.session.commit()

    token = create_jwt({"sub": pub_hex.lower(), "uid": user.id, "npub": user.npub})
    payload = {
        "token": token,
        "token_type": "Bearer",
        "expires_in": int(current_app.config.get("JWT_EXPIRES_DELTA", 24 * 3600)),
        "user": user.to_dict(),
    }
    resp = make_response(jsonify(payload))
    # Set HttpOnly cookie for server-rendered pages
    cookie_name = "pf_jwt"
    max_age = int(current_app.config.get("JWT_EXPIRES_DELTA", 24 * 3600))
    secure = os.getenv("JWT_COOKIE_SECURE", "0") in ("1", "true", "True")
    resp.set_cookie(
        cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        samesite="Lax",
        secure=secure,
        path="/",
    )
    return resp


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    payload = g.jwt_payload
    uid = payload.get("uid")
    sub = payload.get("sub")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    if not user and isinstance(sub, str):
        user = User.query.filter_by(pubkey_hex=sub.lower()).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    return jsonify({"user": user.to_dict()})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    # Clear cookie
    resp.set_cookie("pf_jwt", "", expires=0, path="/")
    return resp
