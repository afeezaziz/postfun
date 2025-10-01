from __future__ import annotations

from functools import wraps
from typing import Optional
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
import time
import json

from flask import render_template, request, g, redirect, url_for, abort, flash, Response, current_app
from urllib.parse import urlsplit

from ...utils.jwt_utils import verify_jwt
from ...extensions import db, cache
from ...models import (
    User,
    Token,
    TokenInfo,
    SwapPool,
    SwapTrade,
    TokenBalance,
    TwitterUser,
    UserTwitterConnection,
)
from sqlalchemy import case, exists, or_, func
from ...services.metrics import inc_sse, dec_sse

from . import tokens_bp

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


@tokens_bp.app_context_processor
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


# Tokens list page
@tokens_bp.route("/")
@cache.cached(timeout=60, query_string=True)
def tokens_list():
    # Simple list with search/sort/pagination
    q = request.args.get("q", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    page = request.args.get("page", default=1, type=int)
    per = request.args.get("per", default=12, type=int)
    stage = request.args.get("stage", type=str)
    category = request.args.get("category", type=str)

    qry = Token.query
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
        # Category filter
        if category:
            like_cat = f"%{category.strip()}%"
            qry = qry.filter(TokenInfo.categories.ilike(like_cat))
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
    if q:
        like = f"%{q}%"
        qry = qry.filter((Token.symbol.ilike(like)) | (Token.name.ilike(like)))
    # Stage filter
    if stage in {"1", "2", "3", "4"}:
        s_val = int(stage)
        qry = qry.filter(
            exists().where(
                or_(SwapPool.token_a_id == Token.id, SwapPool.token_b_id == Token.id)
            ).where(SwapPool.stage == s_val)
        )

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
        meta_title="Tokens — Postfun",
        meta_description="Browse tokens by market cap, price and 24h change on Postfun.",
        meta_url=url_for("web.tokens.tokens_list", _external=True),
    )


# Token detail page
@tokens_bp.route("/<symbol>")
def token_detail(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    # Respect hidden/moderation flags
    try:
        info = TokenInfo.query.filter_by(token_id=token.id).first()
        if bool(getattr(token, "hidden", False)) or (info and getattr(info, "moderation_status", None) == "hidden"):
            abort(404)
    except Exception:
        pass
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
    launcher = None
    try:
        if info and info.launch_user_id:
            launcher = db.session.get(User, int(info.launch_user_id))
    except Exception:
        launcher = None
    meta_title = f"{token.symbol} – {token.name} | Postfun"
    meta_description = (info.description if info and info.description else "From posts to markets. Turn vibes into value on Postfun.")
    meta_image = info.logo_url if (info and info.logo_url) else None
    meta_url = url_for("web.tokens.token_detail", symbol=token.symbol, _external=True)

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

    # Follow status (for quick follow/unfollow from token page)
    is_following = False
    if launcher and payload and isinstance(payload.get("uid"), int):
        me = db.session.get(User, int(payload["uid"]))
        if me:
            row = CreatorFollow.query.filter_by(follower_user_id=me.id, creator_user_id=launcher.id).first()
            is_following = row is not None

    # Preferred pool to compute fee summary (gUSD pair if possible)
    fee_summary = None
    try:
        gusd = _get_gusd_token()
        pool = None
        if gusd:
            pool = SwapPool.query.filter(
                ((SwapPool.token_a_id == token.id) & (SwapPool.token_b_id == gusd.id))
                | ((SwapPool.token_b_id == token.id) & (SwapPool.token_a_id == gusd.id))
            ).first()
        if not pool:
            pool = SwapPool.query.filter((SwapPool.token_a_id == token.id) | (SwapPool.token_b_id == token.id)).first()
        if pool:
            fee_summary = _fee_summary_for_pool_cached(pool.id)
    except Exception:
        pass

    # Get token holders
    token_holders = []
    holders_count = 0
    try:
        # Get token balances for this token, ordered by amount descending
        balances = (
            TokenBalance.query
            .join(User, TokenBalance.user_id == User.id)
            .filter(TokenBalance.token_id == token.id, TokenBalance.amount > 0)
            .order_by(TokenBalance.amount.desc())
            .limit(50)
            .all()
        )
        holders_count = TokenBalance.query.filter(TokenBalance.token_id == token.id, TokenBalance.amount > 0).count()

        # Calculate total supply for percentage calculation
        total_supply = float(info.total_supply or 0) if info else 0
        if total_supply == 0:
            # Fallback: sum all balances
            total_supply = sum(float(balance.amount or 0) for balance in balances)

        # Format holders data
        for i, balance in enumerate(balances):
            percentage = (float(balance.amount or 0) / total_supply * 100) if total_supply > 0 else 0
            token_holders.append({
                'rank': i + 1,
                'user': balance.user,
                'address': balance.user.npub or balance.user.pubkey_hex if balance.user else f"address...{balance.user_id}",
                'amount': float(balance.amount or 0),
                'percentage': percentage
            })
    except Exception:
        pass

    # Get recent trades for this token
    recent_trades = []
    try:
        # Find pools that include this token
        pools = SwapPool.query.filter(
            (SwapPool.token_a_id == token.id) | (SwapPool.token_b_id == token.id)
        ).all()

        for pool in pools:
            # Get recent trades for this pool
            trades = (
                SwapTrade.query
                .join(User, SwapTrade.user_id == User.id)
                .filter(SwapTrade.pool_id == pool.id)
                .order_by(SwapTrade.created_at.desc())
                .limit(20)
                .all()
            )

            for trade in trades:
                # Determine if this is a buy or sell for this token
                is_buy = False
                trade_amount = 0
                trade_price = 0

                if pool.token_a_id == token.id:
                    # Token A: AtoB = selling token A, BtoA = buying token A
                    is_buy = trade.side == "BtoA"
                    trade_amount = float(trade.amount_out if is_buy else trade.amount_in)
                    # Calculate price
                    if pool.token_b_id == gusd.id:
                        trade_price = float(trade.amount_out / trade.amount_in) if trade.amount_in else 0
                    else:
                        trade_price = float(trade.amount_in / trade.amount_out) if trade.amount_out else 0
                else:
                    # Token B: AtoB = buying token B, BtoA = selling token B
                    is_buy = trade.side == "AtoB"
                    trade_amount = float(trade.amount_out if is_buy else trade.amount_in)
                    # Calculate price
                    if pool.token_a_id == gusd.id:
                        trade_price = float(trade.amount_in / trade.amount_out) if trade.amount_out else 0
                    else:
                        trade_price = float(trade.amount_out / trade.amount_in) if trade.amount_in else 0

                trade_total = trade_amount * trade_price

                recent_trades.append({
                    'created_at': trade.created_at,
                    'type': 'buy' if is_buy else 'sell',
                    'amount': trade_amount,
                    'price': trade_price,
                    'total': trade_total,
                    'user': trade.user
                })

        # Sort by time and limit to most recent
        recent_trades.sort(key=lambda x: x['created_at'], reverse=True)
        recent_trades = recent_trades[:20]
    except Exception:
        pass

    return render_template(
        "token_detail.html",
        token=token,
        info=info,
        watchlisted=watchlisted,
        price=price,
        launcher=launcher,
        is_following=is_following,
        fee_summary=fee_summary,
        holders_count=holders_count,
        token_holders=token_holders,
        recent_trades=recent_trades,
        meta_title=meta_title,
        meta_description=meta_description,
        meta_image=meta_image,
        meta_url=meta_url,
        jsonld=jsonld,
    )




# Explore page
@tokens_bp.route("/explore")
@cache.cached(timeout=60, query_string=True)
def explore():
    # Filters: q (search), filter (gainers|losers|all), sort (market_cap|price|change_24h), order (desc|asc)
    # Ranges: price_min, price_max, change_min, change_max; Pagination: page, per
    q = request.args.get("q", type=str)
    filt = request.args.get("filter", default="all", type=str)
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    page = request.args.get("page", default=1, type=int)
    per = request.args.get("per", default=12, type=int)
    stage = request.args.get("stage", default="all", type=str)
    category = request.args.get("category", type=str)
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
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
        # Category filter (comma-separated contains match)
        if category:
            like_cat = f"%{category.strip()}%"
            qry = qry.filter(TokenInfo.categories.ilike(like_cat))
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
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

    # Stage filter (1..4)
    if stage in {"1", "2", "3", "4"}:
        s_val = int(stage)
        qry = qry.filter(
            exists().where(
                or_(SwapPool.token_a_id == Token.id, SwapPool.token_b_id == Token.id)
            ).where(SwapPool.stage == s_val)
        )

    if sort == "stage":
        stage_max = (
            db.session.query(func.coalesce(func.max(SwapPool.stage), 0))
            .filter(or_(SwapPool.token_a_id == Token.id, SwapPool.token_b_id == Token.id))
            .correlate(Token)
            .scalar_subquery()
        )
        if order == "asc":
            qry = qry.order_by(stage_max.asc())
        else:
            qry = qry.order_by(stage_max.desc())
    else:
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

    # AMM prices for tokens on this page
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}

    # Quick category chips (top 12 by frequency across all TokenInfo)
    top_categories: list[str] = []
    try:
        cats = []
        for row in TokenInfo.query.with_entities(TokenInfo.categories).all():
            s = row[0]
            if not s:
                continue
            for c in s.split(','):
                c = c.strip()
                if c:
                    cats.append(c)
        from collections import Counter
        cnt = Counter([c.lower() for c in cats])
        # preserve original casing for popular tags if present in cats
        # Build map of lower->first seen original
        first_case = {}
        for c in cats:
            lc = c.lower()
            if lc not in first_case:
                first_case[lc] = c
        top_categories = [first_case[k] for k, _ in cnt.most_common(12)]
    except Exception:
        top_categories = []

    # Most Active 24h (by candle volume)
    most_active_24h = []
    try:
        since_24h = datetime.utcnow() - timedelta(days=1)
        rows = (
            db.session.query(OHLCCandle.token_id, func.coalesce(func.sum(OHLCCandle.v), 0).label("vol"))
            .filter(OHLCCandle.interval == "1m", OHLCCandle.ts >= since_24h)
            .group_by(OHLCCandle.token_id)
            .order_by(func.coalesce(func.sum(OHLCCandle.v), 0).desc())
            .limit(6)
            .all()
        )
        for tid, vol in rows:
            t = db.session.get(Token, tid)
            if t:
                most_active_24h.append({"token": t, "vol_24h": float(vol or 0)})
    except Exception:
        most_active_24h = []

    # Trending items (reuse cached builder from home)
    try:
        trending_items = _cached_trending_items()[:6]
    except Exception:
        trending_items = []

    return render_template(
        "explore.html",
        tokens=tokens,
        q=q or "",
        filt=filt,
        sort=sort,
        order=order,
        stage=stage,
        category=category or "",
        page=page,
        per=per,
        total=total,
        pages=pages,
        price_min=price_min_s or "",
        price_max=price_max_s or "",
        change_min=change_min_s or "",
        change_max=change_max_s or "",
        price_by_symbol=price_by_symbol,
        meta_title="Explore — Postfun",
        meta_description="Explore the Postfun market: filter by gainers, losers, price and more.",
        meta_url=url_for("web.tokens.explore", _external=True),
        top_categories=top_categories,
        most_active_24h=most_active_24h,
        trending_items=trending_items,
    )


import re

def extract_post_id_from_url(url):
    """Extract Twitter post ID from URL including complex URLs with /photo/1."""
    if not url:
        return None

    # Handle various Twitter/X URL formats
    patterns = [
        r'https://(twitter\.com|x\.com)/.*/status/(\d+)',  # Basic status URL
        r'https://(twitter\.com|x\.com)/.*/status/(\d+)/.*',  # Status with additional paths like /photo/1
    ]

    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return match.group(2)

    return None

def generate_token_details_from_post_id(post_id):
    """Generate token symbol and name from Twitter post ID."""
    # Use the post ID directly as both symbol and name
    symbol = post_id
    name = post_id
    return symbol, name

# Launchpad page
@tokens_bp.route("/launchpad", methods=["GET", "POST"])
@require_auth_web
def launchpad():
    form = {
        "symbol": "",
        "name": "",
        "post_url": "",
    }
    errors = {}
    confirm_preview = False

    # Prefill from query param q on GET
    if request.method == "GET":
        q = request.args.get("q", type=str)
        post_url = request.args.get("post_url", type=str)
        if q or post_url:
            url = (q or post_url or "").strip()
            if url:
                post_id = extract_post_id_from_url(url)
                if post_id:
                    symbol, name = generate_token_details_from_post_id(post_id)
                    form["symbol"] = symbol
                    form["name"] = name
                    form["post_url"] = url

    if request.method == "POST":
        form["post_url"] = (request.form.get("post_url", "").strip() or "")
        form["symbol"] = (request.form.get("symbol", "").strip() or "").upper()
        form["name"] = (request.form.get("name", "").strip() or "")
        confirm_flag = request.form.get("confirm") == "yes"

        # Field validations
        if not form["post_url"]:
            errors["post_url"] = "Twitter post URL is required"
        elif not extract_post_id_from_url(form["post_url"]):
            errors["post_url"] = "Invalid Twitter post URL format"

        if not form["symbol"]:
            errors["symbol"] = "Symbol is required"
        elif not form["symbol"].isdigit() or len(form["symbol"]) < 3:
            errors["symbol"] = "Symbol must be a valid post ID (digits only, minimum 3 digits)"
        if not form["name"]:
            errors["name"] = "Name is required"

        if errors:
            for msg in errors.values():
                flash(msg, "error")
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=False), 400

        # If not confirmed yet, show preview to confirm
        if not confirm_flag:
            confirm_preview = True
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=confirm_preview), 200

        # Confirmed: create token with fixed parameters
        symbol = form["symbol"]
        name = form["name"]
        post_url = form["post_url"]
        post_id = extract_post_id_from_url(post_url)

        token = Token.query.filter_by(symbol=symbol).first()
        if token is not None:
            errors["symbol"] = "Token with this symbol already exists"
            for msg in errors.values():
                flash(msg, "error")
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=False), 400

        try:
            # Create token with fixed supply
            token = Token(symbol=symbol, name=name)
            db.session.add(token)
            db.session.flush()  # Get token ID

            # Create token info with Twitter details
            info = TokenInfo(
                token_id=token.id,
                total_supply=Decimal("1000000000"),  # 1 billion tokens
                tweet_url=post_url,
                tweet_author=f"Tweet Author {post_id[:4]}",  # Placeholder
                tweet_content=f"Tokenized tweet {post_id}",  # Placeholder
                tweet_created_at=datetime.utcnow(),
                launch_user_id=g.jwt_payload.get("uid"),
                launch_at=datetime.utcnow(),
            )
            db.session.add(info)

            # Commit the token creation
            db.session.commit()

            # Invalidate caches affected by launches
            try:
                cache.delete_memoized(tokens_list)
                cache.delete_memoized(explore)
                cache.delete_memoized(pro)
                cache.delete_memoized(stats)
                cache.delete_memoized(_cached_recent_launches)
                cache.delete_memoized(_cached_top_creators)
                cache.delete_memoized(_cached_stats)
                cache.delete_memoized(_cached_trending_items)
            except Exception:
                pass

            flash("Token created successfully!", "success")
            return redirect(url_for("web.tokens.token_detail", symbol=symbol, launched=1))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Failed to create token: {e}")
            flash("Failed to create token", "error")
            return render_template("launchpad.html", form=form, errors=errors, confirm_preview=False), 500

    return render_template("launchpad.html", form=form, errors=errors, confirm_preview=confirm_preview)


# Pro scanner page
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


@tokens_bp.route("/pro")
@cache.cached(timeout=60, query_string=True)
def pro():
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    risk_filter = request.args.get("risk", default="all", type=str)
    trending_only = request.args.get("trending", default="0", type=str) == "1"

    qry = Token.query
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
    tokens = qry.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    ).all()
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
        meta_title="Pro Scanner — Postfun",
        meta_description="Deep-dive token scanner with risk, sentiment and trends on Postfun.",
        meta_url=url_for("web.tokens.pro", _external=True),
    )


# Watchlist routes
@tokens_bp.route("/watchlist")
@require_auth_web
def watchlist():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
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
    return render_template(
        "watchlist.html",
        items=items,
        user=user,
        q=q or "",
        sort=sort,
        order=order,
        price_by_symbol=price_by_symbol,
        meta_title="Watchlist — Postfun",
        meta_description="Your watchlist on Postfun.",
        meta_url=url_for("web.tokens.watchlist", _external=True),
    )


@tokens_bp.route("/watchlist/add/<symbol>", methods=["POST"])
@require_auth_web
def watchlist_add(symbol: str):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
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
    next_url = request.args.get("next") or url_for("web.tokens.token_detail", symbol=symbol)
    return redirect(next_url)


@tokens_bp.route("/watchlist/remove/<symbol>", methods=["POST"])
@require_auth_web
def watchlist_remove(symbol: str):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
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
    next_url = request.args.get("next") or url_for("web.tokens.token_detail", symbol=symbol)
    return redirect(next_url)


# Alerts routes
@tokens_bp.route("/alerts")
@require_auth_web
def alerts():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
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
    # Limit available tokens to non-hidden/non-moderated hidden
    t_qry = Token.query
    try:
        t_qry = t_qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        t_qry = t_qry.filter((Token.hidden == False))  # noqa: E712
        t_qry = t_qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
    except Exception:
        t_qry = t_qry.filter((Token.hidden == False))  # noqa: E712
    tokens = t_qry.order_by(Token.symbol.asc()).all()
    return render_template(
        "alerts.html",
        user=user,
        rules=rules,
        events=events,
        tokens=tokens,
        meta_title="Alerts — Postfun",
        meta_description="Price alerts on Postfun.",
        meta_url=url_for("web.tokens.alerts", _external=True),
    )


@tokens_bp.route("/alerts/create", methods=["POST"])
@require_auth_web
def alerts_create():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
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
        return redirect(url_for("web.tokens.alerts"))
    # Create rule
    rule = AlertRule(user_id=user.id, token_id=token.id, condition=condition, threshold=threshold)
    try:
        db.session.add(rule)
        db.session.commit()
        flash("Alert created", "success")
    except Exception:
        db.session.rollback()
        flash("Could not create alert (maybe duplicate)", "error")
    return redirect(url_for("web.tokens.alerts"))


@tokens_bp.route("/alerts/delete/<int:rule_id>", methods=["POST"])
@require_auth_web
def alerts_delete(rule_id: int):
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.main.home"))
    rule = AlertRule.query.filter_by(id=rule_id, user_id=user.id).first()
    if not rule:
        flash("Alert not found", "error")
        return redirect(url_for("web.tokens.alerts"))
    try:
        db.session.delete(rule)
        db.session.commit()
        flash("Alert deleted", "success")
    except Exception:
        db.session.rollback()
        flash("Could not delete alert", "error")
    return redirect(url_for("web.tokens.alerts"))


# Export routes
@tokens_bp.route("/export/tokens.csv")
def export_tokens_csv():
    # Export basic token data as CSV
    qry = Token.query
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
    tokens = qry.order_by(
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


@tokens_bp.route("/export/explore.csv")
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


@tokens_bp.route("/export/pro.csv")
def export_pro_csv():
    sort = request.args.get("sort", default="market_cap", type=str)
    order = request.args.get("order", default="desc", type=str)
    risk_filter = request.args.get("risk", default="all", type=str)
    trending_only = request.args.get("trending", default="0", type=str) == "1"

    qry = Token.query
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
    tokens = qry.order_by(
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


# Stats page
@tokens_bp.route("/stats")
@cache.cached(timeout=120)
def stats():
    qry = Token.query
    # Exclude hidden tokens and those moderated as hidden
    try:
        qry = qry.outerjoin(TokenInfo, TokenInfo.token_id == Token.id)
        qry = qry.filter((Token.hidden == False))  # noqa: E712
        qry = qry.filter((TokenInfo.moderation_status == None) | (TokenInfo.moderation_status != 'hidden'))  # noqa: E711
    except Exception:
        qry = qry.filter((Token.hidden == False))  # noqa: E712
    tokens = qry.order_by(
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

    # Volume leaders (24h): prefer OHLCCandle sums if present, fallback to metrics
    since = datetime.utcnow() - timedelta(days=1)
    vol_rows = (
        db.session.query(OHLCCandle.token_id, func.coalesce(func.sum(OHLCCandle.v), 0).label("vol"))
        .filter(OHLCCandle.interval == "1m", OHLCCandle.ts >= since)
        .group_by(OHLCCandle.token_id)
        .order_by(func.coalesce(func.sum(OHLCCandle.v), 0).desc())
        .limit(5)
        .all()
    )
    token_by_id = {t.id: t for t in tokens}
    volume_leaders = []
    for tid, v in vol_rows:
        t = token_by_id.get(tid)
        if t:
            volume_leaders.append({"token": t, "vol_24h": float(v or 0)})
    if not volume_leaders:
        # fallback using mock metrics
        items = [_compute_token_metrics(t) for t in tokens]
        items.sort(key=lambda it: it["vol_24h"], reverse=True)
        for it in items[:5]:
            volume_leaders.append({"token": it["token"], "vol_24h": it["vol_24h"]})

    # Stage leaders: highest current stage across pools
    pools = SwapPool.query.all()
    max_stage: dict[int, int] = {}
    for p in pools:
        max_stage[p.token_a_id] = max(int(p.stage or 1), max_stage.get(p.token_a_id, 1))
        max_stage[p.token_b_id] = max(int(p.stage or 1), max_stage.get(p.token_b_id, 1))
    stage_pairs = [(token_by_id.get(tid), stg) for tid, stg in max_stage.items() if token_by_id.get(tid)]
    stage_pairs.sort(key=lambda pair: pair[1], reverse=True)
    stage_leaders = [{"token": t, "stage": stg} for t, stg in stage_pairs[:5]]

    return render_template(
        "stats.html",
        num_tokens=num_tokens,
        avg_price=avg_price,
        avg_mcap=avg_mcap,
        top_by_mcap=top_by_mcap,
        gainers=gainers,
        losers=losers,
        volume_leaders=volume_leaders,
        stage_leaders=stage_leaders,
        meta_title="Stats — Postfun",
        meta_description="Market stats across Postfun.",
        meta_url=url_for("web.tokens.stats", _external=True),
    )


# SSE endpoints
@tokens_bp.route("/sse/prices")
def sse_prices():
    symbol = request.args.get("symbol", type=str)
    if not symbol:
        abort(400)
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)

    def event_stream(sym: str):
        inc_sse("prices")
        try:
            while True:
                try:
                    t = Token.query.filter_by(symbol=sym).first()
                    # Use AMM-computed price when available for consistency
                    amm_price = _amm_price_for_token(t) if t else None
                    price = float(amm_price) if amm_price is not None else (float(t.price or 0) if t and t.price is not None else 0.0)
                    data = json.dumps({"symbol": sym, "price": price})
                    yield f"data: {data}\n\n"
                except Exception:
                    # Heartbeat on errors to keep connection alive
                    yield ": keep-alive\n\n"
                time.sleep(5)
        finally:
            dec_sse("prices")

    return Response(event_stream(symbol), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@tokens_bp.route("/sse/trades")
def sse_trades():
    """Stream recent trades for the homepage ticker."""
    def event_stream():
        last_ts = datetime.utcnow() - timedelta(minutes=10)
        inc_sse("trades")
        try:
            while True:
                try:
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
                            gusd = _get_gusd_token()
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
                except Exception:
                    yield ": keep-alive\n\n"
                time.sleep(5)
        finally:
            dec_sse("trades")

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@tokens_bp.route("/sse/alerts")
def sse_alerts():
    payload = get_jwt_from_cookie()
    if not payload:
        abort(401)
    uid = payload.get("uid")
    if not isinstance(uid, int):
        abort(401)

    def event_stream(user_id: int):
        last_ts = datetime.utcnow() - timedelta(minutes=5)
        inc_sse("alerts")
        try:
            while True:
                try:
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
                except Exception:
                    yield ": keep-alive\n\n"
                time.sleep(5)
        finally:
            dec_sse("alerts")

    return Response(event_stream(uid), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@tokens_bp.route("/sse/follow")
def sse_follow():
    payload = get_jwt_from_cookie()
    if not payload or not isinstance(payload.get("uid"), int):
        abort(401)
    uid = int(payload["uid"])

    def event_stream(me_id: int):
        last_ts = datetime.utcnow() - timedelta(minutes=10)
        # Cache followed creators and derived token_ids, refresh periodically to reduce DB load
        followed = []
        token_ids = []
        last_follow_refresh = datetime.utcnow() - timedelta(minutes=10)
        refresh_interval = timedelta(seconds=60)
        inc_sse("follow")
        try:
            while True:
                try:
                    # creators I follow (refresh every 60s)
                    now = datetime.utcnow()
                    if (now - last_follow_refresh) > refresh_interval:
                        followed = [row.creator_user_id for row in CreatorFollow.query.filter_by(follower_user_id=me_id).all()]
                        # Derive token_ids for followed creators (refresh with followed)
                        token_ids = [row[0] for row in db.session.query(TokenInfo.token_id).filter(TokenInfo.launch_user_id.in_(followed)).all()] if followed else []
                        last_follow_refresh = now
                    if not followed:
                        yield ": keep-alive\n\n"
                        time.sleep(5)
                        continue
                    emitted = False
                    # New launches by followed creators
                    launches = (
                        TokenInfo.query
                        .filter(TokenInfo.launch_user_id.in_(followed), TokenInfo.launch_at != None, TokenInfo.launch_at > last_ts)  # noqa: E711
                        .order_by(TokenInfo.launch_at.asc())
                        .limit(50)
                        .all()
                    )
                    for info in launches:
                        t = db.session.get(Token, info.token_id)
                        creator = db.session.get(User, info.launch_user_id) if info.launch_user_id else None
                        data = json.dumps({
                            "type": "launch",
                            "symbol": t.symbol if t else None,
                            "name": t.name if t else None,
                            "time": (info.launch_at.isoformat() + "Z") if info.launch_at else None,
                            "creator_id": info.launch_user_id,
                            "creator": (creator.display_name or creator.npub or creator.pubkey_hex) if creator else None,
                        })
                        yield f"data: {data}\n\n"
                        last_ts = max(last_ts, info.launch_at or last_ts)
                        emitted = True

                    # Stage changes (burn events) for tokens by followed creators
                    if token_ids:
                        from ...models import BurnEvent
                        burns = (
                            db.session.query(BurnEvent, SwapPool)
                            .join(SwapPool, BurnEvent.pool_id == SwapPool.id)
                            .filter(BurnEvent.created_at > last_ts)
                            .filter((SwapPool.token_a_id.in_(token_ids)) | (SwapPool.token_b_id.in_(token_ids)))
                            .order_by(BurnEvent.created_at.asc())
                            .limit(50)
                            .all()
                        )
                        for ev, pool in burns:
                            # Determine display token (non-gUSD where possible)
                            gusd = _get_gusd_token()
                            tokA = db.session.get(Token, pool.token_a_id)
                            tokB = db.session.get(Token, pool.token_b_id)
                            disp = tokA
                            if gusd and tokA and tokA.id == gusd.id:
                                disp = tokB
                            elif gusd and tokB and tokB.id == gusd.id:
                                disp = tokA
                            data = json.dumps({
                                "type": "stage",
                                "symbol": disp.symbol if disp else (tokA.symbol if tokA else None),
                                "stage": int(ev.stage),
                                "time": ev.created_at.isoformat() + "Z",
                            })
                            yield f"data: {data}\n\n"
                            last_ts = max(last_ts, ev.created_at or last_ts)
                            emitted = True

                    if not emitted:
                        yield ": keep-alive\n\n"
                except Exception:
                    yield ": keep-alive\n\n"
                time.sleep(5)
        finally:
            dec_sse("follow")

    return Response(event_stream(uid), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# Helper functions for caching (need to be accessible across the module)
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
    watchlists_count = WatchlistItem.query.count()
    return {
        "tokens": int(tokens_count or 0),
        "pools": int(pools_count or 0),
        "creators": int(creators_count or 0),
        "trades_24h": int(trades_24h or 0),
        "volume_24h": float(volume_24h_gusd or 0.0),
        "watchlists": int(watchlists_count or 0),
    }


# Short-cache fee summary builder for a pool (used on pool and token pages)
@cache.memoize(timeout=5)
def _fee_summary_for_pool_cached(pool_id: int):
    from decimal import Decimal as _D
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return None
    from ...models import FeeDistributionRule, FeePayout
    rule = FeeDistributionRule.query.filter_by(pool_id=pool.id).first()
    bps_c = int(rule.bps_creator if rule else 5000)
    bps_m = int(rule.bps_minter if rule else 3000)
    bps_t = int(rule.bps_treasury if rule else 2000)
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


# Mock data generators
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
        swap_data = {
            "side": side,
            "amount": round(amount, 4),
            "price": round(price, 6),
            "time": ts.isoformat() + "Z",
        }
        swaps.append(swap_data)
    return swaps