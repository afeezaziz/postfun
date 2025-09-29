from __future__ import annotations

from functools import wraps
from typing import Optional
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
import json

from flask import render_template, request, g, redirect, url_for, abort, flash, current_app
from sqlalchemy import case, func

from ...utils.jwt_utils import verify_jwt
from ...extensions import db
from ...models import (
    User,
    Token,
    SwapPool,
    SwapTrade,
    TokenBalance,
    WatchlistItem,
    TokenInfo,
    FeeDistributionRule,
    FeePayout,
)
from ...services.amm import execute_swap, quote_swap

from . import trading_bp

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
            return redirect(url_for("web.main.home"))
        g.jwt_payload = payload
        return f(*args, **kwargs)

    return wrapper


def _get_gusd_token() -> Optional[Token]:
    return Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()


def _amm_price_for_token(token: Token) -> Optional[float]:
    """Compute AMM price for token against gUSD if such a pool exists."""
    gusd = _get_gusd_token()
    if not gusd:
        return None
    pool = (
        SwapPool.query.filter(
            ((SwapPool.token_a_id == token.id) & (SwapPool.token_b_id == gusd.id))
            | ((SwapPool.token_b_id == token.id) & (SwapPool.token_a_id == gusd.id))
        ).first()
    )
    if not pool or not pool.reserve_a or not pool.reserve_b:
        return None
    try:
        if pool.token_b_id == gusd.id:
            pr = (pool.reserve_b / pool.reserve_a)
        elif pool.token_a_id == gusd.id:
            pr = (pool.reserve_a / pool.reserve_b)
        else:
            pr = None
        return float(pr) if pr is not None else None
    except Exception:
        return None


# Pool detail page
@trading_bp.route("/pool/<symbol>")
def pool(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)

    # Find preferred pool paired with gUSD
    gusd = _get_gusd_token()
    pool = None
    if gusd:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == token.id) & (SwapPool.token_b_id == gusd.id))
            | ((SwapPool.token_b_id == token.id) & (SwapPool.token_a_id == gusd.id))
        ).first()
    if not pool:
        pool = SwapPool.query.filter((SwapPool.token_a_id == token.id) | (SwapPool.token_b_id == token.id)).first()

    # Compute current price in gUSD per token based on reserves if possible
    price = None
    if pool and pool.reserve_a and pool.reserve_b:
        if gusd and pool.token_b_id == gusd.id:
            price = (pool.reserve_b / pool.reserve_a) if pool.reserve_a else None
        elif gusd and pool.token_a_id == gusd.id:
            price = (pool.reserve_a / pool.reserve_b) if pool.reserve_b else None

    # Recent trades -> build a small series (time, price)
    trades = []
    series = []
    if pool:
        rows = (
            SwapTrade.query.filter_by(pool_id=pool.id)
            .order_by(SwapTrade.created_at.desc())
            .limit(50)
            .all()
        )
        rows = list(reversed(rows))  # chronological
        for t in rows:
            # price in gUSD per token
            if gusd and pool.token_b_id == gusd.id:
                if t.side == "AtoB":
                    pr = (t.amount_out / t.amount_in) if t.amount_in and t.amount_out else None
                else:
                    pr = (t.amount_in / t.amount_out) if t.amount_in and t.amount_out else None
            else:
                # gUSD is token_a
                if t.side == "AtoB":
                    pr = (t.amount_in / t.amount_out) if t.amount_in and t.amount_out else None
                else:
                    pr = (t.amount_out / t.amount_in) if t.amount_in and t.amount_out else None
            pt = {
                "id": t.id,
                "side": t.side,
                "amount_in": float(t.amount_in or 0),
                "amount_out": float(t.amount_out or 0),
                "fee": float(t.fee_paid or 0),
                "stage": int(t.stage or 1),
                "created_at": t.created_at,
                "price": float(pr) if pr is not None else None,
            }
            trades.append(pt)
            if pr is not None:
                series.append({"t": t.created_at.isoformat() + "Z", "price": float(pr)})

    # Top holders for this token
    holders = []
    rows = (
        TokenBalance.query.filter(TokenBalance.token_id == token.id, TokenBalance.amount > 0)
        .order_by(TokenBalance.amount.desc())
        .limit(10)
        .all()
    )
    for idx, r in enumerate(rows, start=1):
        u = db.session.get(User, r.user_id)
        address = (u.npub if (u and u.npub) else (u.pubkey_hex if u else f"user:{r.user_id}"))
        holders.append({"rank": idx, "address": address, "amount": float(r.amount or 0)})

    # Watchlist status for current user
    watchlisted = False
    payload = get_jwt_from_cookie()
    if payload:
        uid = payload.get("uid")
        user = db.session.get(User, uid) if isinstance(uid, int) else None
        if user:
            watchlisted = (
                WatchlistItem.query.filter_by(user_id=user.id, token_id=token.id).first() is not None
            )

    # Creator (launcher) for this token
    launcher = None
    info = TokenInfo.query.filter_by(token_id=token.id).first()
    if info and info.launch_user_id:
        launcher = db.session.get(User, int(info.launch_user_id))

    # Fee summary for this pool (if exists)
    summary = None
    if pool:
        summary = _fee_summary_for_pool_cached(pool.id)

    # Default slippage tolerance (bps) for UI
    default_slippage_bps = int(current_app.config.get("AMM_DEFAULT_MAX_SLIPPAGE_BPS", 500))

    return render_template(
        "pool.html",
        token=token,
        pool=pool,
        price=price,
        series=series,
        holders=holders,
        trades=trades,
        watchlisted=watchlisted,
        confirm_trade_preview=False,
        trade_form=None,
        fee_summary=summary,
        launcher=launcher,
        default_slippage_bps=default_slippage_bps,
    )


# Trade execution
@trading_bp.route("/pool/<symbol>/trade", methods=["POST"])
@require_auth_web
def pool_trade(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    gusd = _get_gusd_token()
    pool = None
    if gusd:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == token.id) & (SwapPool.token_b_id == gusd.id))
            | ((SwapPool.token_b_id == token.id) & (SwapPool.token_a_id == gusd.id))
        ).first()
    if not pool:
        flash("No pool available for this token", "error")
        return redirect(url_for("web.trading.pool", symbol=symbol))

    pay_asset = (request.form.get("pay_asset") or "").strip()
    form_side = (request.form.get("side") or "").strip().lower()
    amount_s = (request.form.get("amount") or "").strip()
    confirm_flag = request.form.get("confirm") == "yes"
    errors = []
    try:
        amt = Decimal(amount_s)
        if amt <= 0:
            errors.append("Amount must be > 0")
    except (InvalidOperation, ValueError):
        amt = None
        errors.append("Invalid amount")

    # Determine side relative to pool orientation
    token_is_a = pool.token_a_id == token.id
    gusd_is_a = gusd and pool.token_a_id == gusd.id
    side = None
    if form_side in {"buy", "sell"}:
        # buy => pay gUSD, receive TOKEN; sell => pay TOKEN, receive gUSD
        if form_side == "buy":
            side = "AtoB" if gusd_is_a else "BtoA"
        else:
            side = "AtoB" if token_is_a else "BtoA"
    else:
        if pay_asset.upper() in {"GUSD", "USD", "G-USD"}:
            side = "AtoB" if gusd_is_a else "BtoA"
        else:
            side = "AtoB" if token_is_a else "BtoA"

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("web.trading.pool", symbol=symbol))

    # For now, execute directly after basic validation

    # Confirmed: execute
    payload = g.jwt_payload
    uid = payload.get("uid") if payload else None
    if not isinstance(uid, int):
        flash("Not authenticated", "error")
        return redirect(url_for("web.main.home"))

    try:
        # Optional slippage/min-out constraints from form
        min_amount_out = None
        max_slippage_bps = request.form.get("max_slippage_bps")
        min_amount_out_s = request.form.get("min_amount_out")
        if min_amount_out_s:
            try:
                min_amount_out = Decimal(min_amount_out_s)
            except Exception:
                min_amount_out = None
        max_slippage = None
        try:
            if max_slippage_bps is not None:
                max_slippage = int(max_slippage_bps)
        except Exception:
            max_slippage = None

        # Multi-pool routing: evaluate candidate pools to maximize output
        tok = token
        candidates = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).all()
        chosen = pool
        chosen_side = side
        best_out = None
        if candidates:
            for p in candidates:
                try:
                    # Re-derive side based on desired action (buy/sell relative to token)
                    if form_side == "buy":
                        side_p = "BtoA" if p.token_a_id == tok.id else "AtoB"
                    else:
                        side_p = "AtoB" if p.token_a_id == tok.id else "BtoA"
                    qtest = quote_swap(p, side_p, amt)
                    if best_out is None or qtest.amount_out > best_out:
                        best_out = qtest.amount_out
                        chosen = p
                        chosen_side = side_p
                except Exception:
                    continue
        pool = chosen
        side = chosen_side

        trade, q, pool = execute_swap(
            db.session,
            pool.id,
            uid,
            side,
            amt,
            min_amount_out=min_amount_out,
            max_slippage_bps=max_slippage,
        )
        db.session.commit()
        # Invalidate cached homepage sections affected by trades
        try:
            from ...extensions import cache
            cache.delete_memoized(_cached_trending_items)
            cache.delete_memoized(_cached_stats)
        except Exception:
            pass
        flash("Trade executed", "success")
    except ValueError as ve:
        db.session.rollback()
        code = str(ve)
        mapping = {
            "insufficient_balance": "Insufficient balance",
            "insufficient_liquidity": "Insufficient liquidity or pool too small",
            "pool_exhausted": "Pool exhausted for this trade size",
            "slippage_too_high": "Slippage too high: expected output below your minimum",
            "price_impact_too_high": "Price impact too high",
            "invalid_side": "Invalid trade side",
            "pool_not_found": "Pool not found",
        }
        flash(mapping.get(code, code or "Trade failed"), "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Trade failed: {e}", "error")
    return redirect(url_for("web.trading.pool", symbol=symbol))


# Helper functions
def _fee_summary_for_pool_cached(pool_id: int):
    from decimal import Decimal as _D
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return None
    rule = FeeDistributionRule.query.filter_by(pool_id=pool.id).first()
    if rule:
        bps_c = int(rule.bps_creator)
        bps_m = int(rule.bps_minter)
        bps_t = int(rule.bps_treasury)
    else:
        bps_c = 5000
        bps_m = 3000
        bps_t = 2000
    fa = _D(pool.fee_accum_a or 0)
    fb = _D(pool.fee_accum_b or 0)
    def _allocs(bps: int):
        return {"A": (fa * _D(bps) / _D(10000)), "B": (fb * _D(bps) / _D(10000))}
    def _paid(entity: str):
        rows = FeePayout.query.filter_by(pool_id=pool.id, entity=entity).all()
        totA = _D("0"); totB = _D("0")
        for p in rows:
            if p.asset == "A": totA += _D(p.amount or 0)
            elif p.asset == "B": totB += _D(p.amount or 0)
        return {"A": totA, "B": totB}
    summary = {}
    for ent, bps in (("creator", bps_c), ("minter", bps_m), ("treasury", bps_t)):
        a = _allocs(bps); p = _paid(ent)
        summary[ent] = {
            "alloc": {"A": float(a["A"]), "B": float(a["B"])},
            "paid": {"A": float(p["A"]), "B": float(p["B"])},
            "pending": {"A": float(max(_D("0"), a["A"] - p["A"])), "B": float(max(_D("0"), a["B"] - p["B"]))},
        }
    return summary


def _cached_trending_items():
    from datetime import timedelta as _td
    since = datetime.utcnow() - _td(days=1)
    gusd = _get_gusd_token()
    pools = SwapPool.query.order_by(SwapPool.id.asc()).all()
    trending = []
    for p in pools:
        if not gusd or (p.token_a_id != gusd.id and p.token_b_id != gusd.id):
            continue
        vol = (
            db.session.query(SwapTrade)
            .filter(SwapTrade.pool_id == p.id, SwapTrade.created_at >= since)
            .with_entities(db.func.coalesce(db.func.sum(SwapTrade.amount_in), 0))
            .scalar()
        )
        token_id = p.token_a_id if p.token_b_id == gusd.id else p.token_b_id
        tok = db.session.get(Token, token_id)
        if not tok:
            continue
        if p.token_b_id == gusd.id:
            if p.reserve_a and p.reserve_b:
                price = p.reserve_b / p.reserve_a
            else:
                price = None
        else:
            if p.reserve_a and p.reserve_b:
                price = p.reserve_a / p.reserve_b
            else:
                price = None
        trending.append({
            "symbol": tok.symbol,
            "name": tok.name,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol or 0),
        })
    trending.sort(key=lambda x: x["volume_24h"], reverse=True)
    return trending


def _cached_stats():
    from datetime import timedelta
    tokens_count = Token.query.count()
    pools_count = SwapPool.query.count()
    creators_count = (
        db.session.query(db.func.count(db.func.distinct(TokenInfo.launch_user_id)))
        .filter(TokenInfo.launch_user_id != None)  # noqa: E711
        .scalar()
    ) or 0
    since_24h = datetime.utcnow() - timedelta(days=1)
    trades_24h = 0
    volume_24h_gusd = 0.0
    gusd = _get_gusd_token()
    if gusd:
        pools_gusd = SwapPool.query.filter(
            (SwapPool.token_a_id == gusd.id) | (SwapPool.token_b_id == gusd.id)
        ).all()
        pool_ids = [p.id for p in pools_gusd]
        if pool_ids:
            rows = (
                SwapTrade.query
                .filter(SwapTrade.pool_id.in_(pool_ids), SwapTrade.created_at >= since_24h)
                .order_by(SwapTrade.created_at.desc())
                .all()
            )
            trades_24h = len(rows)
            for t in rows:
                pool = next((p for p in pools_gusd if p.id == t.pool_id), None)
                if not pool:
                    continue
                if pool.token_b_id == gusd.id:
                    if t.side == "AtoB":
                        if t.amount_out:
                            volume_24h_gusd += float(t.amount_out)
                    else:
                        if t.amount_in:
                            volume_24h_gusd += float(t.amount_in)
                elif pool.token_a_id == gusd.id:
                    if t.side == "AtoB":
                        if t.amount_in:
                            volume_24h_gusd += float(t.amount_in)
                    else:
                        if t.amount_out:
                            volume_24h_gusd += float(t.amount_out)
    else:
        trades_24h = SwapTrade.query.filter(SwapTrade.created_at >= since_24h).count()
    from ...models import WatchlistItem
    watchlists_count = WatchlistItem.query.count()
    # Build stats dictionary
    stats_data = {
        "tokens": int(tokens_count or 0),
        "pools": int(pools_count or 0),
        "creators": int(creators_count or 0),
        "trades_24h": int(trades_24h or 0),
        "volume_24h": float(volume_24h_gusd or 0.0),
        "watchlists": int(watchlists_count or 0),
    }
    return stats_data