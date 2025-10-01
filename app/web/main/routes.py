from __future__ import annotations

from typing import Optional
from datetime import datetime, timedelta
import json

from flask import render_template, request, g, redirect, url_for, Response

from ...utils.jwt_utils import verify_jwt
from ...extensions import db, cache
from ...models import (
    User,
    Token,
    TokenInfo,
    SwapPool,
    SwapTrade,
)
from sqlalchemy import case, exists, or_, func

from . import main_bp
from ..utils import get_gusd_token, amm_price_for_token, cached_trending_items, cached_recent_launches, cached_top_creators, cached_stats

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


@main_bp.app_context_processor
def inject_user():
    """Make current user (if any) available to templates as `current_user`."""
    payload = get_jwt_from_cookie()
    user = None
    if payload:
        uid = payload.get("uid")
        sub = payload.get("sub")
        if isinstance(uid, int):
            user = db.session.get(User, uid)
        if not user and isinstance(sub, str):
            user = User.query.filter_by(pubkey_hex=sub.lower()).first()
    return {"current_user": user}


# Home page
@main_bp.route("/")
def home():
    # Cached trending list
    trending = cached_trending_items()

    # Meme Heat: promote memes (crypto culture). Heuristic keyword match on symbol or name.
    meme_keywords = [
        "PEPE", "DOGE", "SHIB", "WIF", "FLOKI", "BONK", "MEME", "MOON", "PUMP", "MOASS", "DEGEN", "WAGMI",
        "APE", "CAT", "FROG", "LORD", "BOBO", "GME", "AMC"
    ]
    def _is_meme(it):
        s = (it["symbol"] or "").upper()
        n = (it["name"] or "").upper()
        return any(k in s or k in n for k in meme_keywords)
    meme_hot = [it for it in trending if _is_meme(it)][:8]

    # Fair Launch Radar: tokens closest to next stage
    fair_radar = [it for it in trending if it.get("next_stage")]
    fair_radar.sort(key=lambda x: x.get("progress_pct", 0), reverse=True)
    fair_radar = fair_radar[:8]

    # Live trades ticker (latest 30 trades across all pools)
    live_trades = []
    rows = (
        SwapTrade.query.order_by(SwapTrade.created_at.desc()).limit(30).all()
    )
    for t in rows:
        pool = db.session.get(SwapPool, t.pool_id)
        if not pool:
            continue
        # Determine which token (non-gUSD) this trade refers to
        gusd = get_gusd_token()
        token_id = None
        if gusd:
            token_id = pool.token_a_id if pool.token_b_id == gusd.id else pool.token_b_id
        tok = db.session.get(Token, token_id) if token_id else None
        if not tok:
            # fallback: pick token_a as primary if no gUSD
            tok = db.session.get(Token, pool.token_a_id)
        # Determine if this was a buy or sell of tok: receiving tok == buy
        recv_token_id = pool.token_b_id if t.side == "AtoB" else pool.token_a_id
        kind = "buy" if (tok and recv_token_id == tok.id) else "sell"
        # Compute price in gUSD per token if possible
        pr = None
        if gusd:
            if pool.token_b_id == gusd.id:
                pr = (t.amount_in and t.amount_out and (t.amount_out / t.amount_in)) if t.side == "AtoB" else ((t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None)
            elif pool.token_a_id == gusd.id:
                pr = (t.amount_in and t.amount_out and (t.amount_in / t.amount_out)) if t.side == "AtoB" else ((t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None)
        live_trades.append({
            "symbol": tok.symbol if tok else "?",
            "side": kind,
            "amount_in": float(t.amount_in or 0),
            "amount_out": float(t.amount_out or 0),
            "price": float(pr) if pr is not None else None,
            "time": t.created_at.isoformat() + "Z",
        })

    # Recent launches (cached)
    recent_launches = cached_recent_launches()

    # Top creators (cached)
    top_creators = cached_top_creators()

    # Marketplace stats (cached)
    stats = cached_stats()

    tokens = (
        Token.query
        .order_by(
            case((Token.market_cap == None, 1), else_=0),  # noqa: E711
            Token.market_cap.desc(),
        )
        .limit(8)
        .all()
    )
    # Top movers by 24h change
    all_tokens = Token.query.filter(Token.change_24h != None).all()  # noqa: E711
    movers_gainers = sorted(all_tokens, key=lambda t: float(t.change_24h or 0), reverse=True)[:6]
    movers_losers = sorted(all_tokens, key=lambda t: float(t.change_24h or 0))[:6]
    # Compute AMM prices for tokens displayed on this page
    price_by_symbol: dict[str, Optional[float]] = {}
    for t in tokens:
        if t and t.symbol:
            price_by_symbol[t.symbol] = amm_price_for_token(t) or (float(t.price or 0) if t.price is not None else None)
    for t in movers_gainers + movers_losers:
        if t and t.symbol and t.symbol not in price_by_symbol:
            price_by_symbol[t.symbol] = amm_price_for_token(t) or (float(t.price or 0) if t.price is not None else None)
    return render_template(
        "home.html",
        tokens=tokens,
        trending=trending,
        live_trades=live_trades,
        recent_launches=recent_launches,
        top_creators=top_creators,
        meme_hot=meme_hot,
        fair_radar=fair_radar,
        movers_gainers=movers_gainers,
        movers_losers=movers_losers,
        stats=stats,
        price_by_symbol=price_by_symbol,
        meta_title="Postfun — From posts to markets.",
        meta_description="From posts to markets. Turn vibes into value on Postfun.",
        meta_url=url_for("web.main.home", _external=True),
    )


