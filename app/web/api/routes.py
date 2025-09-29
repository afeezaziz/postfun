from __future__ import annotations

from functools import wraps
from typing import Optional
from datetime import datetime, timedelta
import json

from flask import render_template, request, g, redirect, url_for, abort, flash, Response
from urllib.parse import urlsplit

from ...utils.jwt_utils import verify_jwt
from ...extensions import db
from ...models import (
    User,
    Token,
    TokenInfo,
    LightningInvoice,
    LightningWithdrawal,
)
from sqlalchemy import case

from . import api_bp

# Helper: decode JWT from cookie for templates
COOKIE_NAME = "pf_jwt"


def get_jwt_from_cookie() -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    ok, payload = verify_jwt(token)
    if not ok:
        return None
    return payload


def require_auth_web(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        payload = get_jwt_from_cookie()
        if not payload:
            return redirect(url_for("web.home"))
        g.jwt_payload = payload
        return f(*args, **kwargs)

    return wrapper


# API authentication check
@api_bp.route("/api/auth/check")
def api_auth_check():
    import sys
    print("[DEBUG] Auth check endpoint called", file=sys.stderr)
    payload = get_jwt_from_cookie()
    print(f"[DEBUG] JWT payload: {payload}", file=sys.stderr)
    if payload:
        print(f"[DEBUG] User authenticated: {payload.get('uid')}", file=sys.stderr)
        return {"authenticated": True, "user_id": payload.get("uid")}
    print("[DEBUG] User not authenticated", file=sys.stderr)
    return {"authenticated": False}


# Lightning API endpoints
@api_bp.route("/api/lightning/invoice", methods=["POST"])
@require_auth_web
def api_lightning_invoice():
    """Create a lightning invoice for receiving payments."""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return {"error": "User not found"}, 404

    try:
        data = request.get_json()
        amount_sats = int(data.get("amount", 0))
        memo = data.get("memo", "")

        if amount_sats < 100:
            return {"error": "Minimum amount is 100 sats"}, 400

        # Import lightning service
        from ...services.lightning import LNBitsClient

        client = LNBitsClient()
        result = client.create_invoice(amount_sats, memo)

        if result:
            # Create invoice record
            invoice = LightningInvoice(
                user_id=user.id,
                amount_sats=amount_sats,
                memo=memo,
                payment_request=result["payment_request"],
                payment_hash=result["payment_hash"],
                checking_id=result.get("checking_id"),
                status="pending",
                expires_at=datetime.utcnow() + timedelta(minutes=30)
            )
            db.session.add(invoice)
            db.session.commit()

            return {
                "id": invoice.id,
                "payment_request": invoice.payment_request,
                "payment_hash": invoice.payment_hash,
                "amount_sats": invoice.amount_sats,
                "memo": invoice.memo,
                "status": invoice.status,
                "expires_at": invoice.expires_at.isoformat() + "Z" if invoice.expires_at else None
            }
        else:
            return {"error": "Failed to create invoice"}, 500

    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.route("/api/lightning/pay", methods=["POST"])
@require_auth_web
def api_lightning_pay():
    """Pay a lightning invoice."""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return {"error": "User not found"}, 404

    try:
        data = request.get_json()
        bolt11 = data.get("invoice", "")

        if not bolt11 or not bolt11.startswith("lnbc"):
            return {"error": "Invalid lightning invoice"}, 400

        # Import lightning service
        from ...services.lightning import LNBitsClient

        client = LNBitsClient()
        result = client.pay_invoice(bolt11)

        if result:
            # Create withdrawal record
            withdrawal = LightningWithdrawal(
                user_id=user.id,
                amount_sats=result.get("amount_sats", 0),
                bolt11=bolt11,
                fee_sats=result.get("fee_sats"),
                payment_hash=result.get("payment_hash"),
                checking_id=result.get("checking_id"),
                status="confirmed",
                processed_at=datetime.utcnow()
            )
            db.session.add(withdrawal)
            db.session.commit()

            return {
                "id": withdrawal.id,
                "amount_sats": withdrawal.amount_sats,
                "fee_sats": withdrawal.fee_sats,
                "payment_hash": withdrawal.payment_hash,
                "status": withdrawal.status,
                "processed_at": withdrawal.processed_at.isoformat() + "Z" if withdrawal.processed_at else None
            }
        else:
            return {"error": "Failed to pay invoice"}, 500

    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.route("/api/lightning/invoices", methods=["GET"])
@require_auth_web
def api_lightning_invoices():
    """Get user's lightning invoices."""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return {"error": "User not found"}, 404

    try:
        invoices = (
            LightningInvoice.query
            .filter_by(user_id=user.id)
            .order_by(LightningInvoice.created_at.desc())
            .all()
        )

        return {
            "invoices": [invoice.to_dict() for invoice in invoices]
        }

    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.route("/api/lightning/withdrawals", methods=["GET"])
@require_auth_web
def api_lightning_withdrawals():
    """Get user's lightning withdrawals."""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return {"error": "User not found"}, 404

    try:
        withdrawals = (
            LightningWithdrawal.query
            .filter_by(user_id=user.id)
            .order_by(LightningWithdrawal.created_at.desc())
            .all()
        )

        return {
            "withdrawals": [withdrawal.to_dict() for withdrawal in withdrawals]
        }

    except Exception as e:
        return {"error": str(e)}, 500


# Static pages
@api_bp.route("/about")
def about():
    return render_template(
        "about.html",
        meta_title="About — Postfun",
        meta_description="From posts to markets. Turn vibes into value on Postfun.",
        meta_url=url_for("web.api.about", _external=True),
    )


@api_bp.route("/faq")
def faq():
    return render_template(
        "faq.html",
        meta_title="FAQ — Postfun",
        meta_description="Frequently asked questions about Postfun.",
        meta_url=url_for("web.api.faq", _external=True),
    )


@api_bp.route("/download")
def download():
    return render_template(
        "download.html",
        meta_title="Download — Postfun",
        meta_description="Download resources for Postfun.",
        meta_url=url_for("web.api.download", _external=True),
    )


# Utility routes
@api_bp.route("/robots.txt")
def robots_txt():
    content = """
User-agent: *
Allow: /
Disallow: /dashboard
Disallow: /portfolio
Sitemap: {sitemap}
""".strip().format(sitemap=url_for("web.api.sitemap_xml", _external=True))
    return Response(content, mimetype="text/plain", headers={"Cache-Control": "public, max-age=3600"})


@api_bp.route("/sitemap.xml")
def sitemap_xml():
    # Basic sitemap
    urls = [
        url_for("web.home", _external=True),
        url_for("web.tokens.tokens_list", _external=True),
        url_for("web.tokens.explore", _external=True),
        url_for("web.tokens.pro", _external=True),
        url_for("web.tokens.stats", _external=True),
        url_for("web.api.about", _external=True),
        url_for("web.api.faq", _external=True),
        url_for("web.api.download", _external=True),
    ]
    # Token-specific pages
    for t in Token.query.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all():
        urls.append(url_for("web.tokens.token_detail", symbol=t.symbol, _external=True))
        urls.append(url_for("web.trading.pool", symbol=t.symbol, _external=True))
    # Creator profile pages (based on token launches)
    creator_ids = (
        db.session.query(db.func.distinct(TokenInfo.launch_user_id))
        .filter(TokenInfo.launch_user_id != None)  # noqa: E711
        .all()
    )
    for (cid,) in creator_ids:
        urls.append(url_for("web.users.creator_profile", user_id=int(cid), _external=True))
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{items}</urlset>"
    return Response(xml, mimetype="application/xml", headers={"Cache-Control": "public, max-age=3600"})


# Error handlers
@api_bp.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors."""
    return render_template('404.html'), 404


@api_bp.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return render_template('500.html'), 500


@api_bp.errorhandler(401)
def unauthorized_error(error):
    """Handle 401 errors."""
    return render_template('401.html'), 401


@api_bp.errorhandler(403)
def forbidden_error(error):
    """Handle 403 errors."""
    return render_template('403.html'), 403


@api_bp.errorhandler(429)
def too_many_requests_error(error):
    """Handle 429 errors."""
    return render_template('429.html'), 429