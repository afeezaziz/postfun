from __future__ import annotations

from functools import wraps
from typing import Optional
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
import time
import json

from flask import Blueprint, render_template, request, g, redirect, url_for, abort, flash, Response
from urllib.parse import urlsplit

from ..utils.jwt_utils import verify_jwt
from ..extensions import db, cache
from ..models import User, Token, WatchlistItem, AlertRule, AlertEvent, SwapPool, SwapTrade, TokenBalance, TokenInfo
from ..services.amm import execute_swap, quote_swap
from sqlalchemy import case


web_bp = Blueprint("web", __name__)


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
            # redirect to home where user can login
            return redirect(url_for("web.home"))
        g.jwt_payload = payload
        return f(*args, **kwargs)

    return wrapper


@web_bp.app_context_processor
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


# Cached data builders for home page
@cache.memoize(timeout=30)
def _cached_trending_items():
    from datetime import timedelta as _td
    since = datetime.utcnow() - _td(days=1)
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
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
            price = (p.reserve_b / p.reserve_a) if p.reserve_a and p.reserve_b else None
        else:
            price = (p.reserve_a / p.reserve_b) if p.reserve_a and p.reserve_b else None
        # stage progress
        stg = int(p.stage or 1)
        vol_a = float(p.cumulative_volume_a or 0)
        thr1 = float(p.stage1_threshold) if getattr(p, "stage1_threshold", None) is not None else None
        thr2 = float(p.stage2_threshold) if getattr(p, "stage2_threshold", None) is not None else None
        thr3 = float(p.stage3_threshold) if getattr(p, "stage3_threshold", None) is not None else None
        next_thr = None
        if stg < 2:
            next_thr = thr1
        elif stg < 3:
            next_thr = thr2
        elif stg < 4:
            next_thr = thr3
        progress_pct = 100 if not next_thr else max(0, min(100, int(round((vol_a / float(next_thr)) * 100))))
        trending.append({
            "symbol": tok.symbol,
            "name": tok.name,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol or 0),
            "stage": int(p.stage or 1),
            "fee_bps": p.current_fee_bps(),
            "next_stage": (stg + 1) if next_thr else None,
            "progress_pct": progress_pct,
            "remaining_to_next": (float(next_thr) - vol_a) if next_thr else 0.0,
        })
    trending.sort(key=lambda x: x["volume_24h"], reverse=True)
    return trending


@cache.memoize(timeout=60)
def _cached_recent_launches():
    recent_launches = []
    infos = (
        TokenInfo.query.order_by(TokenInfo.launch_at.desc()).limit(12).all()
    )
    for info in infos:
        tok = db.session.get(Token, info.token_id)
        if not tok:
            continue
        recent_launches.append({
            "symbol": tok.symbol,
            "name": tok.name,
            "logo_url": info.logo_url,
            "launch_at": info.launch_at.isoformat() + "Z" if info.launch_at else None,
        })
    return recent_launches


@cache.memoize(timeout=120)
def _cached_top_creators():
    top_creators = []
    agg = (
        db.session.query(TokenInfo.launch_user_id, db.func.count(TokenInfo.id).label("cnt"))
        .filter(TokenInfo.launch_user_id != None)  # noqa: E711
        .group_by(TokenInfo.launch_user_id)
        .order_by(db.text("cnt DESC"))
        .limit(5)
        .all()
    )
    for uid, cnt in agg:
        u = db.session.get(User, uid)
        if not u:
            continue
        top_creators.append({
            "user_id": u.id,
            "npub": u.npub or u.pubkey_hex,
            "count": int(cnt or 0),
        })
    return top_creators


@cache.memoize(timeout=30)
def _cached_stats():
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
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
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
    watchlists_count = WatchlistItem.query.count()
    return {
        "tokens": int(tokens_count or 0),
        "pools": int(pools_count or 0),
        "creators": int(creators_count or 0),
        "trades_24h": int(trades_24h or 0),
        "volume_24h": float(volume_24h_gusd or 0.0),
        "watchlists": int(watchlists_count or 0),
    }

@web_bp.route("/")
def home():
    # Cached trending list
    trending = _cached_trending_items()

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
        gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
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
    recent_launches = _cached_recent_launches()

    # Top creators (cached)
    top_creators = _cached_top_creators()

    # Marketplace stats (cached)
    stats = _cached_stats()

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
            price_by_symbol[t.symbol] = _amm_price_for_token(t) or (float(t.price or 0) if t.price is not None else None)
    for t in movers_gainers + movers_losers:
        if t and t.symbol and t.symbol not in price_by_symbol:
            price_by_symbol[t.symbol] = _amm_price_for_token(t) or (float(t.price or 0) if t.price is not None else None)
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
    )


@web_bp.route("/token/<symbol>")
def token_detail(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    # Check watchlist status for current user if logged in
    watchlisted = False
    payload = get_jwt_from_cookie()
    if payload:
        uid = payload.get("uid")
        user = None
        if isinstance(uid, int):
            user = db.session.get(User, uid)
        if user:
            watchlisted = (
                WatchlistItem.query.filter_by(user_id=user.id, token_id=token.id).first() is not None
            )
    # Compute AMM price for display
    price = _amm_price_for_token(token) or float(token.price or 0)

    # Token info (for SEO)
    info = TokenInfo.query.filter_by(token_id=token.id).first()
    meta_title = f"{token.symbol} – {token.name} | Postfun"
    meta_description = (info.description if info and info.description else "From posts to markets. Turn vibes into value on Postfun.")
    meta_image = info.logo_url if (info and info.logo_url) else None
    meta_url = url_for("web.token_detail", symbol=token.symbol, _external=True)

    # JSON-LD structured data (Product)
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"{token.name} ({token.symbol})",
        "url": meta_url,
        "brand": {"@type": "Brand", "name": "Postfun"},
    }
    if meta_image:
        jsonld["image"] = meta_image
    try:
        pr = float(price or 0)
        if pr > 0:
            jsonld["offers"] = {
                "@type": "Offer",
                "price": f"{pr:.6f}",
                "priceCurrency": "USD",
                "url": meta_url,
                "availability": "https://schema.org/InStock",
            }
    except Exception:
        pass

    return render_template(
        "token_detail.html",
        token=token,
        watchlisted=watchlisted,
        price=price,
        meta_title=meta_title,
        meta_description=meta_description,
        meta_image=meta_image,
        meta_url=meta_url,
        jsonld=jsonld,
    )


@web_bp.route("/dashboard")
@require_auth_web
def dashboard():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    # Counts
    wl_count = 0
    alerts_count = 0
    if user:
        wl_count = WatchlistItem.query.filter_by(user_id=user.id).count()
        alerts_count = AlertRule.query.filter_by(user_id=user.id).count()

    # Trending by AMM 24h volume (gUSD pairs)
    from datetime import timedelta as _td

    trending = []
    since = datetime.utcnow() - _td(days=1)
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
    pools = SwapPool.query.order_by(SwapPool.id.asc()).all()
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
            price = (p.reserve_b / p.reserve_a) if p.reserve_a and p.reserve_b else None
        else:
            price = (p.reserve_a / p.reserve_b) if p.reserve_a and p.reserve_b else None
        trending.append({
            "symbol": tok.symbol,
            "name": tok.name,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol or 0),
        })
    trending.sort(key=lambda x: x["volume_24h"], reverse=True)
    trending = trending[:6]

    return render_template("dashboard.html", user=user, wl_count=wl_count, alerts_count=alerts_count, trending=trending)


@web_bp.route("/tokens")
def tokens_list():
    # Simple list with search/sort/pagination
    q = request.args.get("q", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    page = request.args.get("page", default=1, type=int)
    per = request.args.get("per", default=12, type=int)

    qry = Token.query
    if q:
        like = f"%{q}%"
        qry = qry.filter((Token.symbol.ilike(like)) | (Token.name.ilike(like)))

    sort_col = {
        "market_cap": Token.market_cap,
        "price": Token.price,
        "change_24h": Token.change_24h,
        "symbol": Token.symbol,
        "name": Token.name,
    }.get(sort, Token.market_cap)

    if order == "asc":
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.asc(),
        )
    else:
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.desc(),
        )

    total = qry.count()
    if page < 1:
        page = 1
    if per < 1:
        per = 12
    tokens = qry.limit(per).offset((page - 1) * per).all()
    # AMM prices for page tokens
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}
    pages = (total + per - 1) // per if per else 1

    return render_template(
        "tokens.html",
        tokens=tokens,
        q=q or "",
        sort=sort,
        order=order,
        page=page,
        per=per,
        total=total,
        pages=pages,
        price_by_symbol=price_by_symbol,
    )


@web_bp.route("/explore")
def explore():
    # Filters: q (search), filter (gainers|losers|all), sort (market_cap|price|change_24h), order (desc|asc)
    # Ranges: price_min, price_max, change_min, change_max; Pagination: page, per
    q = request.args.get("q", type=str)
    filt = request.args.get("filter", default="all", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    page = request.args.get("page", default=1, type=int)
    per = request.args.get("per", default=12, type=int)
    price_min_s = request.args.get("price_min", default=None, type=str)
    price_max_s = request.args.get("price_max", default=None, type=str)
    change_min_s = request.args.get("change_min", default=None, type=str)
    change_max_s = request.args.get("change_max", default=None, type=str)

    def parse_dec(val: Optional[str]) -> Optional[Decimal]:
        if val is None or val == "":
            return None
        try:
            return Decimal(val)
        except (InvalidOperation, ValueError):
            return None

    price_min = parse_dec(price_min_s)
    price_max = parse_dec(price_max_s)
    change_min = parse_dec(change_min_s)
    change_max = parse_dec(change_max_s)

    qry = Token.query
    if q:
        like = f"%{q}%"
        qry = qry.filter((Token.symbol.ilike(like)) | (Token.name.ilike(like)))
    if filt == "gainers":
        qry = qry.filter(Token.change_24h != None, Token.change_24h > 0)  # noqa: E711
    elif filt == "losers":
        qry = qry.filter(Token.change_24h != None, Token.change_24h < 0)  # noqa: E711
    if price_min is not None:
        qry = qry.filter(Token.price >= price_min)
    if price_max is not None:
        qry = qry.filter(Token.price <= price_max)
    if change_min is not None:
        qry = qry.filter(Token.change_24h != None, Token.change_24h >= change_min)  # noqa: E711
    if change_max is not None:
        qry = qry.filter(Token.change_24h != None, Token.change_24h <= change_max)  # noqa: E711

    sort_col = {
        "market_cap": Token.market_cap,
        "price": Token.price,
        "change_24h": Token.change_24h,
    }.get(sort, Token.market_cap)

    if order == "asc":
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.asc(),
        )
    else:
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.desc(),
        )

    total = qry.count()
    if page < 1:
        page = 1
    if per < 1:
        per = 12
    tokens = qry.limit(per).offset((page - 1) * per).all()
    pages = (total + per - 1) // per if per else 1

    return render_template(
        "explore.html",
        tokens=tokens,
        q=q or "",
        filt=filt,
        sort=sort,
        order=order,
        page=page,
        per=per,
        total=total,
        pages=pages,
        price_min=price_min_s or "",
        price_max=price_max_s or "",
        change_min=change_min_s or "",
        change_max=change_max_s or "",
        price_by_symbol=price_by_symbol,
    )


@web_bp.route("/launchpad", methods=["GET", "POST"])
@require_auth_web
def launchpad():
    form = {
        "symbol": "",
        "name": "",
        "price": "",
        "market_cap": "",
    }
    errors = {}
    confirm_preview = False

    # Prefill from query param q on GET
    if request.method == "GET":
        q = request.args.get("q", type=str)
        if q:
            q = q.strip()
            sym = None
            name = None
            # If URL, derive from last path segment
            try:
                parts = urlsplit(q)
                if parts.scheme and parts.netloc:
                    # use last non-empty path segment
                    segs = [s for s in parts.path.split("/") if s]
                    base = segs[-1] if segs else parts.netloc.split(".")[0]
                    cand = ''.join([c for c in base if c.isalnum()])
                    sym = cand[:12].upper() if cand else None
                    name = base.replace('-', ' ').replace('_', ' ').title()
            except Exception:
                pass
            if not sym:
                # Treat as name/symbol suggestion
                base = ''.join([c for c in q if c.isalnum() or c == ' ']).strip()
                if base:
                    name = name or base.title()
                    letters = ''.join([c for c in base if c.isalnum()])
                    if letters:
                        sym = letters[:12].upper()
            if sym:
                form["symbol"] = sym
            if name:
                form["name"] = name

    if request.method == "POST":
        form["symbol"] = (request.form.get("symbol", "").strip() or "").upper()
        form["name"] = (request.form.get("name", "").strip() or "").title()
        form["price"] = (request.form.get("price") or "").strip()
        form["market_cap"] = (request.form.get("market_cap") or "").strip()
        confirm_flag = request.form.get("confirm") == "yes"

        # Field validations
        if not form["symbol"] or len(form["symbol"]) > 32:
            errors["symbol"] = "Symbol is required (max 32)"
        if not form["name"]:
            errors["name"] = "Name is required"

        price_val = None
        mcap_val = None
        if form["price"]:
            try:
                price_val = Decimal(form["price"])
                if price_val < 0:
                    errors["price"] = "Price must be >= 0"
            except (InvalidOperation, ValueError):
                errors["price"] = "Invalid price"
        if form["market_cap"]:
            try:
                mcap_val = Decimal(form["market_cap"])
                if mcap_val < 0:
                    errors["market_cap"] = "Market cap must be >= 0"
            except (InvalidOperation, ValueError):
                errors["market_cap"] = "Invalid market cap"

        if errors:
            for msg in errors.values():
                flash(msg, "error")
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=False), 400

        # If not confirmed yet, show preview to confirm
        if not confirm_flag:
            confirm_preview = True
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=confirm_preview), 200

        # Confirmed: create or update token
        symbol = form["symbol"]
        name = form["name"]
        token = Token.query.filter_by(symbol=symbol).first()
        if token is None:
            token = Token(symbol=symbol, name=name)
            db.session.add(token)
        token.name = name
        if price_val is not None:
            token.price = price_val
        if mcap_val is not None:
            token.market_cap = mcap_val
        try:
            db.session.commit()
            flash("Token saved", "success")
            return redirect(url_for("web.token_detail", symbol=symbol, launched=1))
        except Exception:
            db.session.rollback()
            flash("Failed to save token", "error")
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=False), 500

    return render_template("launchpad.html", form=form, errors=errors, confirm_preview=confirm_preview)


def _compute_token_metrics(t: Token):
    """Compute mock scanner metrics deterministically from token fields."""
    # Basic deterministic seed from symbol
    s = sum(ord(c) for c in (t.symbol or "")) or 1
    price = float(t.price or 0) or 0.0
    mcap = float(t.market_cap or 0) or 0.0
    ch = float(t.change_24h or 0) or 0.0
    twitter_score = int((s * 7 + int(price * 3)) % 100)
    mentions = int((s * 13 + int(mcap) // 1000) % 1000)
    sentiment = round(((s % 200) - 100) / 100.0, 2)  # -1.00 .. 1.00
    risk = "low" if ch >= 0 else ("medium" if ch > -2 else "high")
    trending = (s % 2) == 0 or ch > 2
    vol_24h = round((mcap * (abs(ch) / 100.0)) if mcap > 0 else (price * 1000), 2)
    return {
        "token": t,
        "twitterScore": twitter_score,
        "mentions": mentions,
        "sentiment": sentiment,
        "risk": risk,
        "trending": trending,
        "vol_24h": vol_24h,
    }


@web_bp.route("/pro")
def pro():
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    risk_filter = request.args.get("risk", default="all", type=str)
    trending_only = request.args.get("trending", default="0", type=str) == "1"

    tokens = (
        Token.query.order_by(
            case((Token.market_cap == None, 1), else_=0),  # noqa: E711
            Token.market_cap.desc(),
        ).all()
    )
    items = [_compute_token_metrics(t) for t in tokens]
    # AMM prices for display
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}

    # Filter
    if trending_only:
        items = [it for it in items if it["trending"]]
    if risk_filter in {"low", "medium", "high"}:
        items = [it for it in items if it["risk"] == risk_filter]

    # Sort map
    def mcap_or_zero(it):
        v = it["token"].market_cap
        return float(v) if v is not None else 0.0

    def price_or_zero(it):
        v = it["token"].price
        return float(v) if v is not None else 0.0

    def change_or_zero(it):
        v = it["token"].change_24h
        return float(v) if v is not None else 0.0

    risk_rank = {"high": 1, "medium": 2, "low": 3}
    key_map = {
        "market_cap": mcap_or_zero,
        "price": price_or_zero,
        "change_24h": change_or_zero,
        "twitterScore": lambda it: it["twitterScore"],
        "mentions": lambda it: it["mentions"],
        "sentiment": lambda it: it["sentiment"],
        "vol_24h": lambda it: it["vol_24h"],
        "risk": lambda it: risk_rank.get(it["risk"], 0),
        "trending": lambda it: 1 if it["trending"] else 0,
    }
    key_fn = key_map.get(sort, mcap_or_zero)
    reverse = order != "asc"
    items.sort(key=key_fn, reverse=reverse)

    return render_template(
        "pro.html",
        items=items,
        sort=sort,
        order=order,
        risk=risk_filter,
        trending="1" if trending_only else "0",
        price_by_symbol=price_by_symbol,
    )


@web_bp.route("/portfolio")
@require_auth_web
def portfolio():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    tokens = (
        Token.query.order_by(
            case((Token.market_cap == None, 1), else_=0),  # noqa: E711
            Token.market_cap.desc(),
        ).limit(4).all()
    )
    holdings = [{"token": t, "amount": 0.0, "value": 0.0} for t in tokens]
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}
    return render_template("portfolio.html", user=user, holdings=holdings, price_by_symbol=price_by_symbol)


# Phase 6: Watchlist
@web_bp.route("/watchlist")
@require_auth_web
def watchlist():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    q = request.args.get("q", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    qry = (
        WatchlistItem.query.filter_by(user_id=user.id)
        .join(Token, WatchlistItem.token_id == Token.id)
    )
    if q:
        like = f"%{q}%"
        qry = qry.filter((Token.symbol.ilike(like)) | (Token.name.ilike(like)))
    sort_col = {
        "market_cap": Token.market_cap,
        "price": Token.price,
        "change_24h": Token.change_24h,
        "symbol": Token.symbol,
        "name": Token.name,
    }.get(sort, Token.market_cap)
    if order == "asc":
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.asc(),
        )
    else:
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.desc(),
        )
    items = qry.all()
    # Extract tokens from items for price map
    tokens = []
    for it in items:
        try:
            if it.token:
                tokens.append(it.token)
        except Exception:
            pass
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}
    return render_template("watchlist.html", items=items, user=user, q=q or "", sort=sort, order=order, price_by_symbol=price_by_symbol)


@web_bp.route("/watchlist/add/<symbol>", methods=["POST"])
@require_auth_web
def watchlist_add(symbol: str):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    exists = WatchlistItem.query.filter_by(user_id=user.id, token_id=token.id).first()
    if not exists:
        db.session.add(WatchlistItem(user_id=user.id, token_id=token.id))
        try:
            db.session.commit()
            flash(f"Added {symbol} to your watchlist", "success")
        except Exception:
            db.session.rollback()
            flash("Could not add to watchlist", "error")
    next_url = request.args.get("next") or url_for("web.token_detail", symbol=symbol)
    return redirect(next_url)


@web_bp.route("/watchlist/remove/<symbol>", methods=["POST"])
@require_auth_web
def watchlist_remove(symbol: str):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    item = WatchlistItem.query.filter_by(user_id=user.id, token_id=token.id).first()
    if item:
        try:
            db.session.delete(item)
            db.session.commit()
            flash(f"Removed {symbol} from your watchlist", "success")
        except Exception:
            db.session.rollback()
            flash("Could not remove from watchlist", "error")
    next_url = request.args.get("next") or url_for("web.token_detail", symbol=symbol)
    return redirect(next_url)


# Phase 6: Alerts
@web_bp.route("/alerts")
@require_auth_web
def alerts():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    rules = (
        AlertRule.query.filter_by(user_id=user.id)
        .join(Token, AlertRule.token_id == Token.id)
        .order_by(AlertRule.created_at.desc())
        .all()
    )
    # recent events (limit 50)
    events = (
        AlertEvent.query.join(AlertRule, AlertEvent.rule_id == AlertRule.id)
        .filter(AlertRule.user_id == user.id)
        .order_by(AlertEvent.triggered_at.desc())
        .limit(50)
        .all()
    )
    # tokens for convenience in a select control
    tokens = Token.query.order_by(Token.symbol.asc()).all()
    return render_template("alerts.html", user=user, rules=rules, events=events, tokens=tokens)


@web_bp.route("/alerts/create", methods=["POST"])
@require_auth_web
def alerts_create():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    symbol = (request.form.get("symbol") or "").strip()
    condition = (request.form.get("condition") or "").strip()
    threshold_s = (request.form.get("threshold") or "").strip()
    token = Token.query.filter_by(symbol=symbol).first()
    errors = []
    if not token:
        errors.append("Invalid token symbol")
    if condition not in {"price_above", "price_below", "market_cap_above", "market_cap_below", "pct_change_above", "pct_change_below"}:
        errors.append("Invalid condition")
    try:
        threshold = Decimal(threshold_s)
    except Exception:
        threshold = None
        errors.append("Invalid threshold")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("web.alerts"))
    # Create rule
    rule = AlertRule(user_id=user.id, token_id=token.id, condition=condition, threshold=threshold)
    try:
        db.session.add(rule)
        db.session.commit()
        flash("Alert created", "success")
    except Exception:
        db.session.rollback()
        flash("Could not create alert (maybe duplicate)", "error")
    return redirect(url_for("web.alerts"))


@web_bp.route("/alerts/delete/<int:rule_id>", methods=["POST"])
@require_auth_web
def alerts_delete(rule_id: int):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))
    rule = AlertRule.query.filter_by(id=rule_id, user_id=user.id).first()
    if not rule:
        flash("Alert not found", "error")
        return redirect(url_for("web.alerts"))
    try:
        db.session.delete(rule)
        db.session.commit()
        flash("Alert deleted", "success")
    except Exception:
        db.session.rollback()
        flash("Could not delete alert", "error")
    return redirect(url_for("web.alerts"))


def _mock_series(token: Token, points: int = 30):
    """Generate a simple time/price series."""
    base_price = float(token.price or 1.0) or 1.0
    seed = sum(ord(c) for c in token.symbol)
    now = datetime.utcnow()
    series = []
    p = base_price
    for i in range(points):
        # small deterministic drift
        delta = ((seed + i * 3) % 7 - 3) * 0.001
        p = max(0.0001, p * (1 + delta))
        t = now - timedelta(minutes=(points - i) * 15)
        series.append({"t": t.isoformat() + "Z", "price": round(p, 6)})
    return series


def _mock_holders(token: Token, n: int = 8):
    seed = sum(ord(c) for c in token.symbol)
    holders = []
    for i in range(1, n + 1):
        amt = ((seed * i) % 1000) / 10 + 10
        holders.append({"rank": i, "address": f"npub1...{seed % 9999:04d}{i:02d}", "amount": round(amt, 4)})
    return holders


def _mock_swaps(token: Token, n: int = 10):
    seed = sum(ord(c) for c in token.symbol)
    now = datetime.utcnow()
    swaps = []
    for i in range(n):
        side = "buy" if ((seed + i) % 2 == 0) else "sell"
        amount = ((seed + i * 7) % 500) / 10 + 1
        price = float(token.price or 1.0) * (1 + (((seed + i) % 9) - 4) * 0.005)
        ts = now - timedelta(minutes=i * 7)
        swaps.append({
            "side": side,
            "amount": round(amount, 4),
            "price": round(price, 6),
            "time": ts.isoformat() + "Z",
        })
    return swaps


@web_bp.route("/pool/<symbol>")
def pool(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)

    # Find preferred pool paired with gUSD
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
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
    )


@web_bp.route("/pool/<symbol>/trade", methods=["POST"])
@require_auth_web
def pool_trade(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
    pool = None
    if gusd:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == token.id) & (SwapPool.token_b_id == gusd.id))
            | ((SwapPool.token_b_id == token.id) & (SwapPool.token_a_id == gusd.id))
        ).first()
    if not pool:
        flash("No pool available for this token", "error")
        return redirect(url_for("web.token_detail", symbol=symbol))

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
        return redirect(url_for("web.pool", symbol=symbol))

    # For now, execute directly after basic validation

    # Confirmed: execute
    payload = g.jwt_payload
    uid = payload.get("uid") if payload else None
    if not isinstance(uid, int):
        flash("Not authenticated", "error")
        return redirect(url_for("web.home"))

    try:
        execute_swap(db.session, pool.id, uid, side, amt)
        db.session.commit()
        # Invalidate cached homepage sections affected by trades
        try:
            cache.delete_memoized(_cached_trending_items)
            cache.delete_memoized(_cached_stats)
        except Exception:
            pass
        flash("Trade executed", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Trade failed: {e}", "error")
    return redirect(url_for("web.pool", symbol=symbol))


@web_bp.route("/about")
def about():
    return render_template("about.html")


@web_bp.route("/faq")
def faq():
    return render_template("faq.html")


@web_bp.route("/download")
def download():
    return render_template("download.html")


@web_bp.route("/export/tokens.csv")
def export_tokens_csv():
    # Export basic token data as CSV
    tokens = Token.query.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all()
    rows = ["symbol,name,price,market_cap,change_24h"]
    for t in tokens:
        rows.append(
            f"{t.symbol},{t.name},{float(t.price or 0):.8f},{float(t.market_cap or 0):.2f},{float(t.change_24h or 0):.4f}"
        )
    csv_data = "\n".join(rows) + "\n"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=tokens.csv",
            "Cache-Control": "public, max-age=300",
        },
    )


@web_bp.route("/robots.txt")
def robots_txt():
    content = """
User-agent: *
Allow: /
Disallow: /dashboard
Disallow: /portfolio
Sitemap: {sitemap}
""".strip().format(sitemap=url_for("web.sitemap_xml", _external=True))
    return Response(content, mimetype="text/plain", headers={"Cache-Control": "public, max-age=3600"})


@web_bp.route("/sitemap.xml")
def sitemap_xml():
    # Basic sitemap
    urls = [
        url_for("web.home", _external=True),
        url_for("web.tokens_list", _external=True),
        url_for("web.explore", _external=True),
        url_for("web.pro", _external=True),
        url_for("web.stats", _external=True),
        url_for("web.about", _external=True),
        url_for("web.faq", _external=True),
        url_for("web.download", _external=True),
    ]
    # Token-specific pages
    for t in Token.query.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all():
        urls.append(url_for("web.token_detail", symbol=t.symbol, _external=True))
        urls.append(url_for("web.pool", symbol=t.symbol, _external=True))
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{items}</urlset>"
    return Response(xml, mimetype="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@web_bp.route("/sse/prices")
def sse_prices():
    symbol = request.args.get("symbol", type=str)
    if not symbol:
        abort(400)
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)

    def event_stream(sym: str):
        while True:
            t = Token.query.filter_by(symbol=sym).first()
            # Use AMM-computed price when available for consistency
            amm_price = _amm_price_for_token(t) if t else None
            price = float(amm_price) if amm_price is not None else (float(t.price or 0) if t and t.price is not None else 0.0)
            data = json.dumps({"symbol": sym, "price": price})
            yield f"data: {data}\n\n"
            time.sleep(5)

    return Response(event_stream(symbol), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@web_bp.route("/sse/trades")
def sse_trades():
    """Stream recent trades for the homepage ticker."""
    def event_stream():
        last_ts = datetime.utcnow() - timedelta(minutes=10)
        while True:
            rows = (
                SwapTrade.query
                .filter(SwapTrade.created_at > last_ts)
                .order_by(SwapTrade.created_at.asc())
                .limit(100)
                .all()
            )
            if rows:
                for t in rows:
                    last_ts = max(last_ts, t.created_at)
                    pool = db.session.get(SwapPool, t.pool_id)
                    if not pool:
                        continue
                    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
                    token_id = None
                    if gusd:
                        token_id = pool.token_a_id if pool.token_b_id == gusd.id else pool.token_b_id
                    tok = db.session.get(Token, token_id) if token_id else None
                    if not tok:
                        tok = db.session.get(Token, pool.token_a_id)
                    recv_token_id = pool.token_b_id if t.side == "AtoB" else pool.token_a_id
                    kind = "buy" if (tok and recv_token_id == tok.id) else "sell"
                    pr = None
                    if gusd:
                        if pool.token_b_id == gusd.id:
                            pr = (t.amount_out / t.amount_in) if (t.side == "AtoB" and t.amount_in and t.amount_out) else ((t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None)
                        elif pool.token_a_id == gusd.id:
                            pr = (t.amount_in / t.amount_out) if (t.side == "AtoB" and t.amount_in and t.amount_out) else ((t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None)
                    data = json.dumps({
                        "symbol": tok.symbol if tok else "?",
                        "side": kind,
                        "price": float(pr) if pr is not None else None,
                        "time": t.created_at.isoformat() + "Z",
                    })
                    yield f"data: {data}\n\n"
            else:
                # Heartbeat to keep connection alive
                yield ": keep-alive\n\n"
            time.sleep(5)

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

@web_bp.route("/sse/alerts")
def sse_alerts():
    payload = get_jwt_from_cookie()
    if not payload:
        abort(401)
    uid = payload.get("uid")
    if not isinstance(uid, int):
        abort(401)

    def event_stream(user_id: int):
        last_ts = datetime.utcnow() - timedelta(minutes=5)
        while True:
            # stream recent events for this user
            evs = (
                AlertEvent.query.join(AlertRule, AlertEvent.rule_id == AlertRule.id)
                .join(Token, AlertRule.token_id == Token.id)
                .filter(AlertRule.user_id == user_id, AlertEvent.triggered_at > last_ts)
                .order_by(AlertEvent.triggered_at.asc())
                .limit(20)
                .all()
            )
            if evs:
                for ev in evs:
                    last_ts = max(last_ts, ev.triggered_at)
                    data = json.dumps({
                        "symbol": ev.rule.token.symbol,
                        "name": ev.rule.token.name,
                        "condition": ev.rule.condition,
                        "threshold": float(ev.rule.threshold or 0),
                        "price": float(ev.price or 0),
                        "time": ev.triggered_at.isoformat() + "Z",
                    })
                    yield f"data: {data}\n\n"
            else:
                yield ": keep-alive\n\n"
            time.sleep(5)

    return Response(event_stream(uid), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@web_bp.route("/export/explore.csv")
def export_explore_csv():
    # Mirror explore filters to export current view
    q = request.args.get("q", type=str)
    filt = request.args.get("filter", default="all", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    price_min_s = request.args.get("price_min", default=None, type=str)
    price_max_s = request.args.get("price_max", default=None, type=str)
    change_min_s = request.args.get("change_min", default=None, type=str)
    change_max_s = request.args.get("change_max", default=None, type=str)

    def parse_dec(val):
        if val is None or val == "":
            return None
        try:
            return Decimal(val)
        except Exception:
            return None

    price_min = parse_dec(price_min_s)
    price_max = parse_dec(price_max_s)
    change_min = parse_dec(change_min_s)
    change_max = parse_dec(change_max_s)

    qry = Token.query
    if q:
        like = f"%{q}%"
        qry = qry.filter((Token.symbol.ilike(like)) | (Token.name.ilike(like)))
    if filt == "gainers":
        qry = qry.filter(Token.change_24h != None, Token.change_24h > 0)  # noqa: E711
    elif filt == "losers":
        qry = qry.filter(Token.change_24h != None, Token.change_24h < 0)  # noqa: E711
    if price_min is not None:
        qry = qry.filter(Token.price >= price_min)
    if price_max is not None:
        qry = qry.filter(Token.price <= price_max)
    if change_min is not None:
        qry = qry.filter(Token.change_24h != None, Token.change_24h >= change_min)  # noqa: E711
    if change_max is not None:
        qry = qry.filter(Token.change_24h != None, Token.change_24h <= change_max)  # noqa: E711

    sort_col = {
        "market_cap": Token.market_cap,
        "price": Token.price,
        "change_24h": Token.change_24h,
    }.get(sort, Token.market_cap)
    if order == "asc":
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.asc(),
        )
    else:
        qry = qry.order_by(
            case((sort_col == None, 1), else_=0),  # noqa: E711
            sort_col.desc(),
        )

    tokens = qry.all()
    rows = ["symbol,name,price,market_cap,change_24h"]
    for t in tokens:
        rows.append(
            f"{t.symbol},{t.name},{float(t.price or 0):.8f},{float(t.market_cap or 0):.2f},{float(t.change_24h or 0):.4f}"
        )
    csv_data = "\n".join(rows) + "\n"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=explore.csv",
            "Cache-Control": "public, max-age=120",
        },
    )


@web_bp.route("/export/pro.csv")
def export_pro_csv():
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    risk_filter = request.args.get("risk", default="all", type=str)
    trending_only = request.args.get("trending", default="0", type=str) == "1"

    tokens = Token.query.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all()
    items = [_compute_token_metrics(t) for t in tokens]
    if trending_only:
        items = [it for it in items if it["trending"]]
    if risk_filter in {"low", "medium", "high"}:
        items = [it for it in items if it["risk"] == risk_filter]

    def mcap_or_zero(it):
        v = it["token"].market_cap
        return float(v) if v is not None else 0.0

    def price_or_zero(it):
        v = it["token"].price
        return float(v) if v is not None else 0.0

    def change_or_zero(it):
        v = it["token"].change_24h
        return float(v) if v is not None else 0.0

    risk_rank = {"high": 1, "medium": 2, "low": 3}
    key_map = {
        "market_cap": mcap_or_zero,
        "price": price_or_zero,
        "change_24h": change_or_zero,
        "twitterScore": lambda it: it["twitterScore"],
        "mentions": lambda it: it["mentions"],
        "sentiment": lambda it: it["sentiment"],
        "vol_24h": lambda it: it["vol_24h"],
        "risk": lambda it: risk_rank.get(it["risk"], 0),
        "trending": lambda it: 1 if it["trending"] else 0,
    }
    key_fn = key_map.get(sort, mcap_or_zero)
    reverse = order != "asc"
    items.sort(key=key_fn, reverse=reverse)

    rows = [
        "symbol,name,price,market_cap,change_24h,twitterScore,mentions,sentiment,risk,trending,vol_24h",
    ]
    for it in items:
        t = it["token"]
        rows.append(
            f"{t.symbol},{t.name},{float(t.price or 0):.8f},{float(t.market_cap or 0):.2f},{float(t.change_24h or 0):.4f},{it['twitterScore']},{it['mentions']},{it['sentiment']},{it['risk']},{1 if it['trending'] else 0},{it['vol_24h']}"
        )
    csv_data = "\n".join(rows) + "\n"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=pro.csv",
            "Cache-Control": "public, max-age=120",
        },
    )


 


@web_bp.route("/stats")
def stats():
    tokens = Token.query.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all()
    num_tokens = len(tokens)
    prices = [float(t.price) for t in tokens if t.price is not None]
    mcaps = [float(t.market_cap) for t in tokens if t.market_cap is not None]
    avg_price = sum(prices) / len(prices) if prices else 0.0
    avg_mcap = sum(mcaps) / len(mcaps) if mcaps else 0.0

    top_by_mcap = tokens[:5]
    gainers = sorted(tokens, key=lambda t: float(t.change_24h or 0.0), reverse=True)[:5]
    losers = sorted(tokens, key=lambda t: float(t.change_24h or 0.0))[:5]

    return render_template(
        "stats.html",
        num_tokens=num_tokens,
        avg_price=avg_price,
        avg_mcap=avg_mcap,
        top_by_mcap=top_by_mcap,
        gainers=gainers,
        losers=losers,
    )


@web_bp.route("/user/<int:user_id>")
def user_profile(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    return render_template("user.html", user=user)
