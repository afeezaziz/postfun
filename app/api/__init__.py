from __future__ import annotations

from flask import Blueprint, jsonify, abort, request, g, current_app
from ..models import (
    Token,
    TokenInfo,
    WatchlistItem,
    AlertRule,
    AlertEvent,
    AccountBalance,
    LedgerEntry,
    LightningInvoice,
    LightningWithdrawal,
    User,
    TokenBalance,
    SwapPool,
    SwapTrade,
    BurnEvent,
    FeeDistributionRule,
    FeePayout,
    IdempotencyKey,
    OHLCCandle,
)
from ..extensions import cache, db, limiter, csrf
from ..utils.jwt_utils import require_auth
from ..services.lightning import LNBitsClient
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
import re
from urllib.parse import urlparse
from decimal import Decimal
from ..services.amm import quote_swap, execute_swap
from sqlalchemy import case, func
from ..services.reconcile import reconcile_invoices_once, reconcile_withdrawals_once, _get_or_create_balance

api_bp = Blueprint("api", __name__)


def _validate_nostr_auth(auth_header: str) -> dict | None:
    """Validate Nostr signature from Authorization header."""
    try:
        # Extract base64 encoded event from Authorization header
        if not auth_header.startswith("Nostr "):
            return None

        event_b64 = auth_header[6:]  # Remove "Nostr " prefix

        # Decode base64
        import base64
        event_json = base64.b64decode(event_b64).decode('utf-8')

        # Parse JSON
        import json
        event = json.loads(event_json)

        # Validate required fields
        required_fields = ["id", "pubkey", "created_at", "kind", "tags", "content", "sig"]
        if not all(field in event for field in required_fields):
            return None

        # Check if event is recent (within 5 minutes)
        import time
        if time.time() - event["created_at"] > 300:  # 5 minutes
            return None

        # Verify signature using pynostr
        try:
            from pynostr.event import Event

            # Create Event object and verify signature
            evt = Event()
            evt.id = event["id"]
            evt.pubkey = event["pubkey"]
            evt.created_at = event["created_at"]
            evt.kind = event["kind"]
            evt.tags = event["tags"]
            evt.content = event["content"]
            evt.sig = event["sig"]

            if not evt.verify():
                return None

            return event

        except Exception:
            return None

    except Exception:
        return None


@api_bp.get("/auth/check")
def api_auth_check():
    """API authentication check endpoint"""
    from flask import current_app
    import sys

    current_app.logger.info("[DEBUG] API auth check endpoint called")

    # Try to get JWT from Authorization header (bearer token)
    from flask import request
    auth_header = request.headers.get('Authorization')

    if auth_header and auth_header.startswith('Bearer '):
        from ..utils.jwt_utils import verify_jwt
        token = auth_header[7:]  # Remove 'Bearer ' prefix
        ok, payload = verify_jwt(token)
        if ok:
            current_app.logger.info(f"[DEBUG] User authenticated via bearer token: {payload.get('uid')}")
            return {"authenticated": True, "user_id": payload.get("uid")}
        else:
            current_app.logger.warning("[DEBUG] Invalid bearer token")

    # Try to get JWT from cookie as fallback
    from ..utils.jwt_utils import verify_jwt
    from flask import request

    cookie_name = "pf_jwt"
    token = request.cookies.get(cookie_name)

    if token:
        ok, payload = verify_jwt(token)
        if ok:
            current_app.logger.info(f"[DEBUG] User authenticated via cookie: {payload.get('uid')}")
            return {"authenticated": True, "user_id": payload.get("uid")}
        else:
            current_app.logger.warning("[DEBUG] Invalid cookie token")

    current_app.logger.info("[DEBUG] User not authenticated")
    return {"authenticated": False}


@api_bp.get("/tokens")
@cache.cached(timeout=60)
def list_tokens():
    tokens = (
        Token.query.order_by(
            case((Token.market_cap == None, 1), else_=0),  # noqa: E711
            Token.market_cap.desc(),
        ).all()
    )
    return jsonify({"items": [t.to_dict() for t in tokens]})


@api_bp.get("/tokens/<symbol>")
@cache.cached(timeout=30)
def get_token(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    return jsonify(token.to_dict())


# ---- Lightning: Balance / Deposit / Withdraw ----

def _get_user_from_jwt() -> User | None:
    payload = getattr(g, "jwt_payload", {}) or {}
    uid = payload.get("uid")
    sub = payload.get("sub")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    if not user and isinstance(sub, str):
        user = User.query.filter_by(pubkey_hex=sub.lower()).first()
    return user


def _get_or_create_balance(user_id: int, asset: str = "BTC") -> AccountBalance:
    bal = AccountBalance.query.filter_by(user_id=user_id, asset=asset).with_for_update().first()
    if not bal:
        bal = AccountBalance(user_id=user_id, asset=asset, balance_sats=0)
        db.session.add(bal)
        db.session.flush()
    return bal


def _is_admin(user: User | None) -> bool:
    return bool(user and getattr(user, "is_admin", False))


def _parse_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except Exception:
        raise ValueError("invalid_decimal")


# ---- Tokens: Launch, Details, Trending ----


@api_bp.post("/tokens/launch")
@require_auth
@csrf.exempt
def tokens_launch():
    """Permissionless token launch with optional initial AMM pool against gBTC.

    Body:
      - symbol (str, required, A-Z0-9, 3..12) OR post_url (str, Twitter URL)
      - name (str, required)
      - description, logo_url, website, twitter, telegram, discord (optional)
      - total_supply (decimal, optional, defaults to 1B)
      - initial_price_btc (decimal, optional)
      - initial_liquidity_btc (decimal, optional, defaults to 19M sats)
      - create_pool (bool, default true)
      - fee_bps_base (int, default 30)
      - stage1_threshold, stage2_threshold, stage3_threshold (decimals, optional)
      - burn_token_symbol (optional) and burn_stage{1..4}_amount (decimals, optional)
    """
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    data = request.get_json(force=True)

    # Check if using Twitter URL mode
    post_url = data.get("post_url", "").strip()

    if post_url:
        # Extract post ID from Twitter URL including complex URLs with /photo/1
        import re
        patterns = [
            r'https://(twitter\.com|x\.com)/.*/status/(\d+)',  # Basic status URL
            r'https://(twitter\.com|x\.com)/.*/status/(\d+)/.*',  # Status with additional paths like /photo/1
        ]

        post_id = None
        for pattern in patterns:
            match = re.match(pattern, post_url)
            if match:
                post_id = match.group(2)
                break

        if not post_id:
            return jsonify({"error": "invalid_twitter_url"}), 400

        # Use post ID as both symbol and name
        symbol = post_id
        name = post_id

        # Twitter-specific data
        tweet_data = {
            "tweet_url": post_url,
            "tweet_author": f"Author {post_id[:4]}",  # Placeholder
            "tweet_content": f"Tokenized tweet {post_id}",  # Placeholder
            "tweet_created_at": datetime.utcnow(),
        }
    else:
        # Traditional mode
        symbol = (data.get("symbol") or "").strip().upper()
        name = (data.get("name") or "").strip()
        tweet_data = {}

        if not symbol or not name:
            return jsonify({"error": "symbol_and_name_required"}), 400

    # For Twitter post IDs, allow numeric symbols of any length (post IDs are usually 18-19 digits)
    if post_url:
        # Post ID should be all digits and at least 3 digits
        if not symbol.isdigit() or len(symbol) < 3:
            return jsonify({"error": "invalid_post_id_symbol"}), 400
    else:
        # Traditional symbol validation
        if not (3 <= len(symbol) <= 12) or not symbol.replace("_", "").isalnum():
            return jsonify({"error": "invalid_symbol"}), 400
    reserved = {"GUSD", "GBTC", "PFUN"}
    if symbol in reserved or symbol.startswith("LP-"):
        return jsonify({"error": "reserved_symbol"}), 400
    if Token.query.filter_by(symbol=symbol).first():
        return jsonify({"error": "symbol_taken"}), 400

    # Create token
    tok = Token(symbol=symbol, name=name)
    db.session.add(tok)
    db.session.flush()  # get tok.id

    # Save metadata
    info = TokenInfo(
        token_id=tok.id,
        description=(data.get("description") or None),
        logo_url=(data.get("logo_url") or None),
        website=(data.get("website") or None),
        twitter=(data.get("twitter") or None),
        telegram=(data.get("telegram") or None),
        discord=(data.get("discord") or None),
        total_supply=_parse_decimal(data.get("total_supply", "1000000000")),  # Default to 1B
        launch_user_id=user.id,
        launch_at=datetime.utcnow(),
        **tweet_data
    )
    db.session.add(info)

    # Optionally create AMM pool against gBTC using initial price/liquidity
    pool_obj = None
    create_pool = bool(data.get("create_pool", True))
    if create_pool:
        gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
        if not gbtc:
            db.session.rollback()
            return jsonify({"error": "gbtc_not_found"}), 400

        fee_bps_base = int(data.get("fee_bps_base", 30))
        s1 = data.get("stage1_threshold")
        s2 = data.get("stage2_threshold")
        s3 = data.get("stage3_threshold")
        burn_symbol = data.get("burn_token_symbol")
        burn_token = Token.query.filter_by(symbol=burn_symbol).first() if isinstance(burn_symbol, str) else None

        initial_price = data.get("initial_price_btc")
        initial_liq_btc = data.get("initial_liquidity_btc")

        # Fixed parameters for Twitter-based tokens
        if post_url:
            # 1 billion tokens paired with 19 million sats
            total_supply = Decimal("1000000000")
            sats_amount = Decimal("19000000")

            # Calculate initial price: 19M sats / 1B tokens = 0.000019 sats per token
            if initial_price is None:
                initial_price = sats_amount / total_supply

            if initial_liq_btc is None:
                initial_liq_btc = sats_amount

        if initial_price is not None and initial_liq_btc is not None:
            try:
                p = _parse_decimal(initial_price)
                L = _parse_decimal(initial_liq_btc)
            except Exception:
                return jsonify({"error": "invalid_initial_price_or_liquidity"}), 400
            if p <= 0 or L <= 0:
                return jsonify({"error": "initial_price_and_liquidity_must_be_positive"}), 400
            # Use half liquidity on each side: reserve_b (gBTC) = L/2; reserve_a (new token) = (L/2)/p
            reserve_b = (L / Decimal(2)).quantize(Decimal("1.000000000000000000"))
            reserve_a = (reserve_b / p).quantize(Decimal("1.000000000000000000"))
        else:
            # Fallback: use calculated values for Twitter tokens or small defaults
            if post_url:
                reserve_a = Decimal("500000000")  # Half of 1B
                reserve_b = Decimal("9500000")   # Half of 19M
            else:
                reserve_a = Decimal("1000")
                reserve_b = Decimal("1000")

        pool_obj = SwapPool(
            token_a_id=tok.id,
            token_b_id=gbtc.id,
            reserve_a=reserve_a,
            reserve_b=reserve_b,
            fee_bps_base=fee_bps_base,
            stage=1,
            stage1_threshold=_parse_decimal(s1) if s1 is not None else None,
            stage2_threshold=_parse_decimal(s2) if s2 is not None else None,
            stage3_threshold=_parse_decimal(s3) if s3 is not None else None,
            burn_token_id=burn_token.id if burn_token else None,
            burn_stage1_amount=_parse_decimal(data.get("burn_stage1_amount")) if data.get("burn_stage1_amount") is not None else None,
            burn_stage2_amount=_parse_decimal(data.get("burn_stage2_amount")) if data.get("burn_stage2_amount") is not None else None,
            burn_stage3_amount=_parse_decimal(data.get("burn_stage3_amount")) if data.get("burn_stage3_amount") is not None else None,
            burn_stage4_amount=_parse_decimal(data.get("burn_stage4_amount")) if data.get("burn_stage4_amount") is not None else None,
        )
        db.session.add(pool_obj)

    db.session.commit()

    out = {"token": tok.to_dict(), "info": info.to_dict()}
    if pool_obj:
        out["pool"] = pool_obj.to_dict()
    return jsonify(out), 201


@api_bp.get("/tokens/<symbol>/full")
def tokens_full(symbol: str):
    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    info = TokenInfo.query.filter_by(token_id=tok.id).first()
    # Prefer pool with gUSD pairing if present
    gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
    pool = None
    if gbtc:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == tok.id) & (SwapPool.token_b_id == gbtc.id))
            | ((SwapPool.token_b_id == tok.id) & (SwapPool.token_a_id == gbtc.id))
        ).first()
    if not pool:
        pool = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).first()
    return jsonify({
        "token": tok.to_dict(),
        "info": info.to_dict() if info else None,
        "pool": pool.to_dict() if pool else None,
    })


@api_bp.get("/tokens/trending")
def tokens_trending():
    """Return tokens ranked by 24h trade volume (approx), along with pool and price.
    For now, measure volume as sum of amount_in (token A units) for pools with gBTC pairing.
    """
    from datetime import timedelta as _td

    since = datetime.utcnow() - _td(days=1)
    gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
    q = db.session.query(SwapPool).order_by(SwapPool.id.asc())
    pools = q.all()
    items = []
    for p in pools:
        # Restrict to pools that involve gBTC to approximate BTC pricing
        if not gbtc or (p.token_a_id != gbtc.id and p.token_b_id != gbtc.id):
            continue
        vol = (
            db.session.query(SwapTrade)
            .filter(SwapTrade.pool_id == p.id, SwapTrade.created_at >= since)
            .with_entities(db.func.coalesce(db.func.sum(SwapTrade.amount_in), 0))
            .scalar()
        )
        token_id = p.token_a_id if p.token_b_id == gbtc.id else p.token_b_id
        tok = db.session.get(Token, token_id)
        if not tok:
            continue
        # Approx price in BTC from reserves (gBTC/token)
        if p.token_b_id == gbtc.id:
            price = (Decimal(p.reserve_b) / Decimal(p.reserve_a)) if p.reserve_a and p.reserve_b else None
        else:
            price = (Decimal(p.reserve_a) / Decimal(p.reserve_b)) if p.reserve_a and p.reserve_b else None
        items.append({
            "symbol": tok.symbol,
            "name": tok.name,
            "token_id": tok.id,
            "pool_id": p.id,
            "stage": int(p.stage or 1),
            "fee_bps": p.current_fee_bps(),
            "price_btc": float(price) if price is not None else None,
            "volume_24h": float(vol or 0),
        })
    # Sort by 24h volume desc
    items.sort(key=lambda x: x["volume_24h"], reverse=True)
    return jsonify({"items": items})


@api_bp.get("/ohlc")
@cache.cached(timeout=30, query_string=True)
def ohlc():
    """Return OHLC candles for a token.

    Query params:
      - symbol: token symbol (required)
      - interval: one of '1m','5m','1h' (default '1m')
      - window: one of '1h','6h','24h','7d','30d' (default '24h')
      - limit: max candles to return (default 300, max 1000)
    """
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol_required"}), 400
    interval = (request.args.get("interval") or "1m").lower()
    if interval not in {"1m", "5m", "1h"}:
        return jsonify({"error": "invalid_interval"}), 400
    window = (request.args.get("window") or "24h").lower()
    now = datetime.utcnow()
    since = None
    if window in {"1h", "1hour"}:
        since = now - timedelta(hours=1)
    elif window in {"6h"}:
        since = now - timedelta(hours=6)
    elif window in {"24h", "1d"}:
        since = now - timedelta(days=1)
    elif window in {"7d", "1w"}:
        since = now - timedelta(days=7)
    elif window in {"30d", "1m"}:
        since = now - timedelta(days=30)

    limit = max(1, min(1000, int(request.args.get("limit", 300))))

    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"items": []})

    # Prefer DB candles
    q = OHLCCandle.query.filter_by(token_id=tok.id, interval=interval)
    if since is not None:
        q = q.filter(OHLCCandle.ts >= since)
    rows = q.order_by(OHLCCandle.ts.desc()).limit(limit).all()
    rows = list(reversed(rows))
    if rows:
        items = [
            {
                "t": c.ts.isoformat() + "Z",
                "o": float(c.o),
                "h": float(c.h),
                "l": float(c.l),
                "c": float(c.c),
                "v": float(c.v) if c.v is not None else None,
            }
            for c in rows
        ]
        return jsonify({"items": items})

    # Fallback: aggregate ad-hoc from recent trades if no candles yet
    try:
        from ..services.market_data import aggregate_candles_from_trades
        items = aggregate_candles_from_trades(tok.id, interval, since)
        # Do not persist here; scheduler will persist
        return jsonify({"items": items[:limit]})
    except Exception:
        return jsonify({"items": []})


@api_bp.get("/ohlc_fallback")
@cache.cached(timeout=5, query_string=True)
def ohlc_fallback():
    """Deprecated duplicate; aggregate OHLC candles from trades for a token's preferred pool.

    Query params:
      - symbol (required)
      - interval: one of '1m','5m','1h' (default '1m')
      - limit: number of buckets to return (default 300, max 1000)
      - window: alternative to limit, e.g. '24h','7d' (optional)
    """
    symbol = request.args.get("symbol", type=str)
    if not symbol:
        return jsonify({"error": "symbol_required"}), 400
    interval = (request.args.get("interval", default="1m", type=str) or "1m").lower()
    limit = max(1, min(1000, request.args.get("limit", default=300, type=int)))
    window = (request.args.get("window", type=str) or "").lower()

    bucket_seconds = {"1m": 60, "5m": 300, "1h": 3600}.get(interval)
    if not bucket_seconds:
        return jsonify({"error": "invalid_interval"}), 400

    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404

    # Preferred pool with gBTC pairing if possible
    gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
    pool = None
    if gbtc:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == tok.id) & (SwapPool.token_b_id == gbtc.id))
            | ((SwapPool.token_b_id == tok.id) & (SwapPool.token_a_id == gbtc.id))
        ).first()
    if not pool:
        pool = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).first()
    if not pool:
        return jsonify({"items": []})

    now = datetime.utcnow()
    if window in {"1h", "1hour"}:
        since = now - timedelta(hours=1)
    elif window in {"6h"}:
        since = now - timedelta(hours=6)
    elif window in {"24h", "1d"}:
        since = now - timedelta(days=1)
    elif window in {"7d", "1w"}:
        since = now - timedelta(days=7)
    elif window in {"30d", "1m"}:
        since = now - timedelta(days=30)
    else:
        # Default to enough history for the requested number of buckets
        since = now - timedelta(seconds=bucket_seconds * limit)

    # Fetch trades in the window, oldest first
    rows = (
        SwapTrade.query
        .filter(SwapTrade.pool_id == pool.id, SwapTrade.created_at >= since)
        .order_by(SwapTrade.created_at.asc())
        .all()
    )

    # Helper: compute price and volume in token units of the requested token
    token_is_a = (pool.token_a_id == tok.id)
    def trade_price_and_volume(t: SwapTrade):
        pr = None
        vol = None
        if gbtc and pool.token_b_id == gbtc.id:
            # price = gBTC per token (A is token, B is gUSD)
            if t.side == "AtoB":
                pr = (t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None
                vol = t.amount_in if token_is_a else t.amount_out
            else:
                pr = (t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None
                vol = t.amount_out if token_is_a else t.amount_in
        else:
            # gUSD is token_a, token is B
            if t.side == "AtoB":
                pr = (t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None
                vol = t.amount_out if not token_is_a else t.amount_in
            else:
                pr = (t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None
                vol = t.amount_in if not token_is_a else t.amount_out
        return pr, vol

    # Aggregate into OHLC buckets
    from collections import OrderedDict
    buckets = OrderedDict()
    for t in rows:
        pr, vol = trade_price_and_volume(t)
        if pr is None:
            continue
        ts = int(t.created_at.timestamp())
        bucket_ts = (ts // bucket_seconds) * bucket_seconds
        start_at = datetime.utcfromtimestamp(bucket_ts)
        key = start_at
        b = buckets.get(key)
        p = float(pr)
        v = float(vol or 0)
        if b is None:
            buckets[key] = {"o": p, "h": p, "l": p, "c": p, "v": v}
        else:
            b["h"] = max(b["h"], p)
            b["l"] = min(b["l"], p)
            b["c"] = p
            b["v"] += v

    # Limit to requested number of buckets (most recent)
    items = []
    for start_at, b in buckets.items():
        items.append({
            "t": start_at.isoformat() + "Z",
            "o": round(b["o"], 8),
            "h": round(b["h"], 8),
            "l": round(b["l"], 8),
            "c": round(b["c"], 8),
            "v": b["v"],
        })
    if len(items) > limit:
        items = items[-limit:]

    return jsonify({"items": items, "interval": interval, "symbol": tok.symbol})


@api_bp.get("/og/preview")
@cache.cached(timeout=60, query_string=True)
def og_preview():
    """Fetch basic OpenGraph preview for a URL (title, description, image).
    Lightweight regex parsing to avoid bringing in heavy HTML parsers.
    """
    url = request.args.get("url", type=str)
    if not url:
        return jsonify({"error": "url_required"}), 400
    try:
        u = urlparse(url)
    except Exception:
        return jsonify({"error": "invalid_url"}), 400
    if u.scheme not in ("http", "https"):
        return jsonify({"error": "unsupported_scheme"}), 400
    host = (u.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return jsonify({"error": "forbidden_host"}), 400

    headers = {"User-Agent": "PostfunBot/1.0 (+https://postfun.app)"}
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=5, allow_redirects=True, stream=True)
        # Limit size to avoid excessive memory
        max_bytes = 1024 * 1024  # 1MB
        data = resp.raw.read(max_bytes, decode_content=True) if hasattr(resp, "raw") else resp.content[:max_bytes]
        enc = resp.encoding or "utf-8"
        html = data.decode(enc, errors="ignore")
    except Exception as e:
        return jsonify({"error": "fetch_failed", "detail": str(e)}), 502

    def _find_meta_val(attr_name: str):
        # Try og: and twitter: variants
        candidates = [
            f"og:{attr_name}",
        ]
        if attr_name == "title":
            candidates += ["twitter:title"]
        if attr_name == "description":
            candidates += ["twitter:description", "description"]
        if attr_name == "image":
            candidates += ["twitter:image", "image"]

        for cand in candidates:
            # property=..., content=...
            rx1 = re.compile(rf"<meta[^>]+(?:property|name)=[\"']{re.escape(cand)}[\"'][^>]+content=[\"'](.*?)[\"'][^>]*>", re.I|re.S)
            m = rx1.search(html)
            if m:
                return m.group(1).strip()
            # content=..., property=...
            rx2 = re.compile(rf"<meta[^>]+content=[\"'](.*?)[\"'][^>]+(?:property|name)=[\"']{re.escape(cand)}[\"'][^>]*>", re.I|re.S)
            m = rx2.search(html)
            if m:
                return m.group(1).strip()
        return None

    title = _find_meta_val("title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = m.group(1).strip()
    desc = _find_meta_val("description") or ""
    image = _find_meta_val("image")

    return jsonify({
        "url": url,
        "title": title,
        "description": desc,
        "image": image,
    })


@api_bp.get("/tokens/<symbol>/holders")
def tokens_holders(symbol: str):
    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    # Holders: users with amount > 0
    q = (
        db.session.query(TokenBalance)
        .filter(TokenBalance.token_id == tok.id, (TokenBalance.amount > 0))
        .order_by(TokenBalance.amount.desc())
    )
    limit = max(1, min(500, request.args.get("limit", default=50, type=int)))
    rows = q.limit(limit).all()
    holders_count = db.session.query(db.func.count(TokenBalance.id)).filter(
        TokenBalance.token_id == tok.id, (TokenBalance.amount > 0)
    ).scalar()
    items = []
    for r in rows:
        items.append({
            "user_id": r.user_id,
            "amount": float(r.amount or 0),
        })
    return jsonify({
        "token_id": tok.id,
        "symbol": tok.symbol,
        "holders_count": int(holders_count or 0),
        "top_holders": items,
    })


@api_bp.get("/tokens/<symbol>/trades")
def tokens_trades(symbol: str):
    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    pools = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).all()
    pool_ids = [p.id for p in pools]
    if not pool_ids:
        return jsonify({"items": []})
    limit = max(1, min(500, request.args.get("limit", default=100, type=int)))
    rows = (
        SwapTrade.query.filter(SwapTrade.pool_id.in_(pool_ids))
        .order_by(SwapTrade.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [t.to_dict() for t in rows]})


@api_bp.get("/tokens/<symbol>/series")
def tokens_series(symbol: str):
    """Return a simple historical price series for a token based on pool trades.

    Query params:
      - window: one of '1h','6h','24h','7d','30d' (optional)
      - limit: max number of points (default 200, max 1000)
    """
    tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    # Prefer pool with gUSD pairing if present
    gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
    pool = None
    if gbtc:
        pool = SwapPool.query.filter(
            ((SwapPool.token_a_id == tok.id) & (SwapPool.token_b_id == gbtc.id))
            | ((SwapPool.token_b_id == tok.id) & (SwapPool.token_a_id == gbtc.id))
        ).first()
    if not pool:
        pool = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).first()
    if not pool:
        return jsonify({"items": []})

    window = (request.args.get("window") or "").lower()
    now = datetime.utcnow()
    since = None
    if window in {"1h", "1hour"}:
        since = now - timedelta(hours=1)
    elif window in {"6h"}:
        since = now - timedelta(hours=6)
    elif window in {"24h", "1d"}:
        since = now - timedelta(days=1)
    elif window in {"7d", "1w"}:
        since = now - timedelta(days=7)
    elif window in {"30d", "1m"}:
        since = now - timedelta(days=30)

    limit = request.args.get("limit", default=200, type=int)
    limit = max(1, min(1000, int(limit)))

    q = SwapTrade.query.filter_by(pool_id=pool.id)
    if since is not None:
        q = q.filter(SwapTrade.created_at >= since)
    rows = q.order_by(SwapTrade.created_at.desc()).limit(limit).all()
    rows = list(reversed(rows))

    # Build series (ISO time, price in gBTC per token)
    items = []
    for t in rows:
        pr = None
        if gbtc and pool.token_b_id == gbtc.id:
            if t.side == "AtoB":
                pr = (t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None
            else:
                pr = (t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None
        else:
            if t.side == "AtoB":
                pr = (t.amount_in / t.amount_out) if (t.amount_in and t.amount_out) else None
            else:
                pr = (t.amount_out / t.amount_in) if (t.amount_in and t.amount_out) else None
        if pr is not None:
            items.append({
                "t": t.created_at.isoformat() + "Z",
                "price": float(pr),
            })

    # If empty, include a single point from current pool reserves if possible
    if not items and pool and pool.reserve_a and pool.reserve_b:
        if gbtc and pool.token_b_id == gbtc.id:
            pr = (pool.reserve_b / pool.reserve_a)
        elif gbtc and pool.token_a_id == gbtc.id:
            pr = (pool.reserve_a / pool.reserve_b)
        else:
            pr = None
        if pr is not None:
            items.append({"t": now.isoformat() + "Z", "price": float(pr)})

    return jsonify({"items": items})


@api_bp.get("/lightning/balance")
@require_auth
def lightning_balance():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    bal = _get_or_create_balance(user.id)
    return jsonify(bal.to_dict())


@api_bp.post("/lightning/invoice")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_DEFAULT", "100 per hour"))
@csrf.exempt
def lightning_invoice_create():
    """Create a lightning invoice for receiving payments using Nostr signature."""
    try:
        data = request.get_json()
        amount_sats = int(data.get("amount", 0))
        memo = data.get("memo", "")

        if amount_sats < 100:
            return jsonify({"error": "Minimum amount is 100 sats"}), 400

        # Validate Nostr signature from Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Nostr "):
            return jsonify({"error": "missing_nostr_signature"}), 401

        # Extract and validate Nostr event
        event_data = _validate_nostr_auth(auth_header)
        if not event_data:
            return jsonify({"error": "invalid_nostr_signature"}), 401

        # Get user by pubkey
        user = User.query.filter_by(pubkey_hex=event_data["pubkey"].lower()).first()
        if not user:
            return jsonify({"error": "user_not_found"}), 404

        # Debug logging
        current_app.logger.info(f"LNBITS_API_URL: {current_app.config.get('LNBITS_API_URL')}")
        current_app.logger.info(f"LNBITS_INVOICE_KEY: {current_app.config.get('LNBITS_INVOICE_KEY')}")

        try:
            client = LNBitsClient()
            success, result = client.create_invoice(amount_sats, memo)
        except Exception as e:
            current_app.logger.error(f"LNBits client error: {str(e)}")
            return jsonify({"error": f"LNBits client error: {str(e)}"}), 500

        if success and result:
            # Create invoice record
            invoice = LightningInvoice(
                user_id=user.id,
                amount_sats=amount_sats,
                memo=memo,
                payment_request=result["payment_request"],
                payment_hash=result["payment_hash"],
                checking_id=result.get("checking_id"),
                status="pending",
                provider="lnbits",
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
                "created_at": invoice.created_at.isoformat() + "Z" if invoice.created_at else None
            }
        else:
            current_app.logger.error(f"LNBits invoice creation failed: success={success}, result={result}")
            return {"error": "Failed to create invoice with LNBits"}, 500

    except Exception as e:
        return {"error": str(e)}, 500


@api_bp.post("/lightning/deposit")
@require_auth
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_DEFAULT", "100 per hour"))
@csrf.exempt
def lightning_deposit_create():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    data = request.get_json(silent=True) or {}
    amount_sats = data.get("amount_sats")
    memo = data.get("memo") or current_app.config.get("LNBITS_DEFAULT_MEMO", "Deposit")
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    try:
        amount_sats = int(amount_sats)
    except Exception:
        return jsonify({"error": "invalid_amount"}), 400
    if amount_sats <= 0:
        return jsonify({"error": "amount_must_be_positive"}), 400

    try:
        client = LNBitsClient()
    except Exception as e:
        return jsonify({"error": "lightning_not_configured", "detail": str(e)}), 500

    # Idempotency pre-insert (acts as a coarse lock)
    idem_row = None
    if idem_key:
        try:
            idem_row = IdempotencyKey(user_id=user.id, scope="lightning_deposit", key=str(idem_key))
            db.session.add(idem_row)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # Existing idempotent request => return stored resource if available
            idem_row = IdempotencyKey.query.filter_by(user_id=user.id, scope="lightning_deposit", key=str(idem_key)).first()
            if idem_row and idem_row.ref_type == "invoice" and idem_row.ref_id:
                inv0 = db.session.get(LightningInvoice, idem_row.ref_id)
                if inv0:
                    return jsonify(inv0.to_dict())
            # In-flight: the first request hasn't persisted the invoice id yet
            return jsonify({"error": "idempotency_in_progress"}), 409

    ok, res = client.create_invoice(amount_sats=amount_sats, memo=memo)
    if not ok:
        return jsonify({"error": "lnbits_error", "detail": res}), 502

    payment_request = res.get("payment_request") or res.get("pay_request")
    payment_hash = res.get("payment_hash")
    checking_id = res.get("checking_id")
    if not payment_request or not payment_hash:
        return jsonify({"error": "lnbits_bad_response"}), 502

    inv = LightningInvoice(
        user_id=user.id,
        amount_sats=amount_sats,
        memo=memo,
        payment_request=payment_request,
        payment_hash=payment_hash,
        checking_id=checking_id,
        status="pending",
        provider="lnbits",
    )
    db.session.add(inv)
    db.session.commit()

    # Save idempotency reference
    if idem_key:
        try:
            row = IdempotencyKey.query.filter_by(user_id=user.id, scope="lightning_deposit", key=str(idem_key)).with_for_update().first()
            if row:
                row.ref_type = "invoice"
                row.ref_id = inv.id
                db.session.add(row)
                db.session.commit()
        except Exception:
            db.session.rollback()

    return jsonify(inv.to_dict()), 201


@api_bp.get("/lightning/invoices")
@require_auth
def lightning_invoices_list():
    """Get user's lightning invoices."""
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    try:
        invoices = (
            LightningInvoice.query
            .filter_by(user_id=user.id)
            .order_by(LightningInvoice.created_at.desc())
            .all()
        )

        return jsonify({
            "invoices": [invoice.to_dict() for invoice in invoices]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.get("/lightning/invoices/<invoice_id>")
@require_auth
def lightning_invoice_status(invoice_id: str):
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    inv = db.session.get(LightningInvoice, invoice_id)
    if not inv or inv.user_id != user.id:
        return jsonify({"error": "invoice_not_found"}), 404

    # If already paid and credited, just return
    if inv.status in ("paid", "expired", "cancelled") and inv.credited:
        return jsonify(inv.to_dict())

    # Poll provider for status
    try:
        client = LNBitsClient()
        ok, res = client.get_payment_status(inv.payment_hash)
    except Exception as e:
        ok, res = False, {"error": str(e)}

    if ok:
        # LNbits returns { "paid": bool, ... }
        paid = bool(res.get("paid"))
        if paid and inv.status != "paid":
            inv.status = "paid"
            inv.paid_at = datetime.utcnow()
        # If paid and not credited, credit user's balance and write ledger
        if paid and not inv.credited:
            bal = _get_or_create_balance(user.id)
            bal.balance_sats = int(bal.balance_sats) + int(inv.amount_sats)
            db.session.add(bal)
            le = LedgerEntry(
                user_id=user.id,
                entry_type="deposit",
                delta_sats=int(inv.amount_sats),
                ref_type="invoice",
                ref_id=inv.id,
            )
            db.session.add(le)
            inv.credited = True
            db.session.add(inv)
            db.session.commit()
    else:
        # keep as pending; optionally return provider error
        pass

    return jsonify(inv.to_dict())


@api_bp.post("/lightning/withdraw")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_DEFAULT", "100 per hour"))
@csrf.exempt
def lightning_withdraw():
    # Validate Nostr signature from Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Nostr "):
        return jsonify({"error": "missing_nostr_signature"}), 401

    # Extract and validate Nostr event
    event_data = _validate_nostr_auth(auth_header)
    if not event_data:
        return jsonify({"error": "invalid_nostr_signature"}), 401

    # Get user by pubkey
    user = User.query.filter_by(pubkey_hex=event_data["pubkey"].lower()).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    if getattr(user, "withdraw_frozen", False):
        return jsonify({"error": "withdraw_frozen"}), 403
    data = request.get_json(force=True)
    bolt11 = data.get("bolt11")
    amount_sats = data.get("amount_sats")  # optional; try to decode if missing
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    if not isinstance(bolt11, str) or not bolt11:
        return jsonify({"error": "invalid_bolt11"}), 400

    # Try to derive amount if not provided (best-effort)
    if amount_sats is None:
        amount_sats = data.get("amount")
    if amount_sats is None:
        # could call LNbits decode here if available; for now, require client-provided
        return jsonify({"error": "amount_required"}), 400
    try:
        amount_sats = int(amount_sats)
    except Exception:
        return jsonify({"error": "invalid_amount"}), 400
    if amount_sats <= 0:
        return jsonify({"error": "amount_must_be_positive"}), 400

    max_fee = int(current_app.config.get("LNBITS_MAX_FEE_SATS", 20))

    # Reserve funds: deduct amount now; add fee later when known; refund on failure
    bal = _get_or_create_balance(user.id)
    if bal.balance_sats < amount_sats:
        return jsonify({"error": "insufficient_funds", "balance_sats": int(bal.balance_sats)}), 400

    # Idempotency pre-insert (acts as a coarse lock)
    idem_row = None
    if idem_key:
        try:
            idem_row = IdempotencyKey(user_id=user.id, scope="lightning_withdraw", key=str(idem_key))
            db.session.add(idem_row)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            idem_row = IdempotencyKey.query.filter_by(user_id=user.id, scope="lightning_withdraw", key=str(idem_key)).first()
            if idem_row and idem_row.ref_type == "withdrawal" and idem_row.ref_id:
                w0 = db.session.get(LightningWithdrawal, idem_row.ref_id)
                if w0:
                    return jsonify(w0.to_dict())
            return jsonify({"error": "idempotency_in_progress"}), 409

    w = LightningWithdrawal(
        user_id=user.id,
        amount_sats=amount_sats,
        bolt11=bolt11,
        status="pending",
        provider="lnbits",
    )
    db.session.add(w)
    # Deduct immediately
    bal.balance_sats = int(bal.balance_sats) - amount_sats
    db.session.add(bal)
    le = LedgerEntry(
        user_id=user.id,
        entry_type="withdrawal",
        delta_sats=-int(amount_sats),
        ref_type="withdrawal",
        ref_id=w.id,
    )
    db.session.add(le)
    db.session.commit()

    # Trigger payment with provider
    try:
        client = LNBitsClient()
        ok, res = client.pay_invoice(bolt11=bolt11, max_fee_sats=max_fee)
        if not ok:
            raise RuntimeError(str(res))
        w.payment_hash = res.get("payment_hash") or w.payment_hash
        w.checking_id = res.get("checking_id") or w.checking_id
        db.session.add(w)
        db.session.commit()
        # Save idempotency reference
        if idem_key:
            try:
                row = IdempotencyKey.query.filter_by(user_id=user.id, scope="lightning_withdraw", key=str(idem_key)).with_for_update().first()
                if row:
                    row.ref_type = "withdrawal"
                    row.ref_id = w.id
                    db.session.add(row)
                    db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception as e:
        # Refund on failure
        bal = _get_or_create_balance(user.id)
        bal.balance_sats = int(bal.balance_sats) + amount_sats
        db.session.add(bal)
        w.status = "failed"
        db.session.add(w)
        db.session.add(LedgerEntry(
            user_id=user.id,
            entry_type="adjustment",
            delta_sats=int(amount_sats),
            ref_type="withdrawal",
            ref_id=w.id,
            meta=f"refund: {e}",
        ))
        db.session.commit()
        return jsonify({"error": "withdraw_failed", "detail": str(e)}), 502

    return jsonify(w.to_dict()), 201


@api_bp.post("/admin/reconcile-now")
@require_auth
@csrf.exempt
def admin_reconcile_now():
    """Admin-only: trigger a one-shot reconcile for invoices or withdrawals.

    Body: { "op": "invoices" | "withdrawals" }
    """
    user = _get_user_from_jwt()
    if not _is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    op = (data.get("op") or "").strip().lower()
    try:
        if op == "invoices":
            count = reconcile_invoices_once()
        elif op == "withdrawals":
            count = reconcile_withdrawals_once()
        else:
            return jsonify({"error": "invalid_op"}), 400
        return jsonify({"ok": True, "op": op, "count": int(count or 0)})
    except Exception as e:
        return jsonify({"error": "reconcile_failed", "detail": str(e)}), 500


@api_bp.get("/lightning/withdrawals/<withdraw_id>")
@require_auth
def lightning_withdraw_status(withdraw_id: str):
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    w = db.session.get(LightningWithdrawal, withdraw_id)
    if not w or w.user_id != user.id:
        return jsonify({"error": "withdrawal_not_found"}), 404

    if w.status in ("confirmed", "failed"):
        return jsonify(w.to_dict())


# ---- AMM: Pools, Quotes, Swaps, Balances ----


@api_bp.post("/amm/pools")
@require_auth
@csrf.exempt
def amm_create_pool():
    user = _get_user_from_jwt()
    if not _is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True)

    # Identify tokens by id or symbol
    token_a_id = data.get("token_a_id")
    token_b_id = data.get("token_b_id")
    symbol_a = data.get("symbol_a")
    symbol_b = data.get("symbol_b")

    if not token_a_id and isinstance(symbol_a, str):
        t = Token.query.filter_by(symbol=symbol_a).first()
        if not t:
            return jsonify({"error": "token_a_not_found"}), 404
        token_a_id = t.id
    if not token_b_id and isinstance(symbol_b, str):
        t = Token.query.filter_by(symbol=symbol_b).first()
        if not t:
            return jsonify({"error": "token_b_not_found"}), 404
        token_b_id = t.id
    if not token_a_id or not token_b_id:
        return jsonify({"error": "token_ids_required"}), 400

    reserve_a = _parse_decimal(data.get("reserve_a", 0))
    reserve_b = _parse_decimal(data.get("reserve_b", 0))
    fee_bps_base = int(data.get("fee_bps_base", 30))
    stage1_threshold = data.get("stage1_threshold")
    stage2_threshold = data.get("stage2_threshold")
    stage3_threshold = data.get("stage3_threshold")
    burn_token_id = data.get("burn_token_id")
    burn_stage1_amount = data.get("burn_stage1_amount")
    burn_stage2_amount = data.get("burn_stage2_amount")
    burn_stage3_amount = data.get("burn_stage3_amount")
    burn_stage4_amount = data.get("burn_stage4_amount")

    pool = SwapPool(
        token_a_id=int(token_a_id),
        token_b_id=int(token_b_id),
        reserve_a=reserve_a,
        reserve_b=reserve_b,
        fee_bps_base=fee_bps_base,
        stage=1,
        stage1_threshold=_parse_decimal(stage1_threshold) if stage1_threshold is not None else None,
        stage2_threshold=_parse_decimal(stage2_threshold) if stage2_threshold is not None else None,
        stage3_threshold=_parse_decimal(stage3_threshold) if stage3_threshold is not None else None,
        burn_token_id=int(burn_token_id) if burn_token_id else None,
        burn_stage1_amount=_parse_decimal(burn_stage1_amount) if burn_stage1_amount is not None else None,
        burn_stage2_amount=_parse_decimal(burn_stage2_amount) if burn_stage2_amount is not None else None,
        burn_stage3_amount=_parse_decimal(burn_stage3_amount) if burn_stage3_amount is not None else None,
        burn_stage4_amount=_parse_decimal(burn_stage4_amount) if burn_stage4_amount is not None else None,
    )
    db.session.add(pool)
    db.session.commit()
    return jsonify(pool.to_dict()), 201


@api_bp.get("/amm/pools")
def amm_list_pools():
    pools = SwapPool.query.order_by(SwapPool.id.asc()).all()
    return jsonify({"items": [p.to_dict() for p in pools]})


@api_bp.get("/amm/pools/<int:pool_id>")
def amm_get_pool(pool_id: int):
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return jsonify({"error": "pool_not_found"}), 404
    return jsonify(pool.to_dict())


@api_bp.post("/amm/quote")
@csrf.exempt
def amm_quote():
    data = request.get_json(force=True)
    pool_id = data.get("pool_id")
    side = data.get("side")  # optional when routing by symbol+action
    symbol = data.get("symbol")
    action = (data.get("action") or "").lower()  # 'buy' | 'sell' when symbol is provided
    amount_in_raw = data.get("amount_in")
    try:
        amount_in = _parse_decimal(amount_in_raw)
    except Exception:
        return jsonify({"error": "invalid_amount_in"}), 400

    pool = db.session.get(SwapPool, int(pool_id)) if pool_id else None

    # Routing by symbol + action
    if not pool and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
        if not tok:
            return jsonify({"error": "token_not_found"}), 404
        # Candidate pools that contain the token
        cands = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).all()
        if not cands:
            return jsonify({"error": "pool_not_found"}), 404
        gbtc = Token.query.filter_by(symbol="gBTC").first() or Token.query.filter_by(symbol="GBTC").first()
        best = None
        for p in cands:
            try:
                # Determine side based on action
                if action == "buy":
                    # pay other token, receive 'symbol'
                    side_p = "BtoA" if p.token_a_id == tok.id else "AtoB"
                elif action == "sell":
                    # pay 'symbol', receive other token
                    side_p = "AtoB" if p.token_a_id == tok.id else "BtoA"
                else:
                    continue
                qq = quote_swap(p, side_p, amount_in)
                score = (float(qq.amount_out), 1 if (gbtc and (p.token_a_id == (gbtc.id) or p.token_b_id == (gbtc.id))) else 0)
                if best is None or score > best[0]:
                    best = (score, p, side_p, qq)
            except Exception:
                continue
        if not best:
            return jsonify({"error": "insufficient_liquidity"}), 400
        _, pool, side, q = best
    else:
        if not pool:
            return jsonify({"error": "pool_not_found"}), 404
        try:
            q = quote_swap(pool, side, amount_in)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    return jsonify({
        "pool_id": int(pool.id),
        "side": side,
        "amount_in": float(amount_in),
        "amount_out": float(q.amount_out),
        "fee_bps": int(q.fee_bps),
        "fee_amount": float(q.fee_amount),
        "effective_in": float(q.effective_in),
        "execution_price": float(q.execution_price),
        "mid_price": float(q.mid_price),
        "price_impact_bps": int(q.price_impact_bps),
        "stage": int(pool.stage or 1),
    })


@api_bp.post("/amm/swap")
@require_auth
@csrf.exempt
def amm_swap():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    data = request.get_json(force=True)
    pool_id = data.get("pool_id")
    side = data.get("side")  # optional when routing
    symbol = data.get("symbol")
    action = (data.get("action") or "").lower()  # 'buy' | 'sell'
    amount_in_raw = data.get("amount_in")
    min_amount_out_raw = data.get("min_amount_out")
    max_slippage_bps = data.get("max_slippage_bps")
    try:
        amount_in = _parse_decimal(amount_in_raw)
    except Exception:
        return jsonify({"error": "invalid_amount_in"}), 400

    # Routing if needed
    pool = db.session.get(SwapPool, int(pool_id)) if pool_id else None
    if not pool and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
        if not tok:
            return jsonify({"error": "token_not_found"}), 404
        cands = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).all()
        if not cands:
            return jsonify({"error": "pool_not_found"}), 404
        best = None
        for p in cands:
            try:
                if action == "buy":
                    side_p = "BtoA" if p.token_a_id == tok.id else "AtoB"
                elif action == "sell":
                    side_p = "AtoB" if p.token_a_id == tok.id else "BtoA"
                else:
                    continue
                qq = quote_swap(p, side_p, amount_in)
                score = float(qq.amount_out)
                if best is None or score > best[0]:
                    best = (score, p, side_p)
            except Exception:
                continue
        if not best:
            return jsonify({"error": "insufficient_liquidity"}), 400
        _, pool, side = best
    if not pool or not side:
        return jsonify({"error": "missing_params"}), 400

    # Parse optional constraints
    min_amount_out = None
    if min_amount_out_raw is not None:
        try:
            min_amount_out = _parse_decimal(min_amount_out_raw)
        except Exception:
            return jsonify({"error": "invalid_min_amount_out"}), 400
    try:
        trade, q, pool = execute_swap(
            db.session,
            int(pool.id),
            user.id,
            side,
            amount_in,
            min_amount_out=min_amount_out,
            max_slippage_bps=int(max_slippage_bps) if max_slippage_bps is not None else None,
        )
        db.session.commit()
        # Invalidate hot caches affected by trades
        try:
            from ..web import _cached_trending_items, _cached_stats
            cache.delete_memoized(_cached_trending_items)
            cache.delete_memoized(_cached_stats)
        except Exception:
            pass
        return jsonify({
            "trade": trade.to_dict(),
            "pool": pool.to_dict(),
            "quote": {
                "amount_out": float(q.amount_out),
                "fee_bps": q.fee_bps,
                "fee_amount": float(q.fee_amount),
                "effective_in": float(q.effective_in),
                "execution_price": float(q.execution_price),
                "mid_price": float(q.mid_price),
                "price_impact_bps": int(q.price_impact_bps),
            },
        }), 201
    except ValueError as ve:
        db.session.rollback()
        # Friendlier error codes
        code = str(ve)
        mapping = {
            "insufficient_balance": ("insufficient_balance", 400),
            "insufficient_liquidity": ("insufficient_liquidity", 400),
            "pool_exhausted": ("pool_exhausted", 400),
            "slippage_too_high": ("slippage_too_high", 400),
            "price_impact_too_high": ("price_impact_too_high", 400),
            "invalid_side": ("invalid_side", 400),
            "pool_not_found": ("pool_not_found", 404),
            "token_frozen": ("token_frozen", 400),
        }
        err, status = mapping.get(code, (code or "bad_request", 400))
        return jsonify({"error": err}), status
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "swap_failed", "detail": str(e)}), 500


@api_bp.get("/amm/balances")
@require_auth
def amm_balances():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    rows = (
        db.session.query(TokenBalance, Token)
        .join(Token, Token.id == TokenBalance.token_id)
        .filter(TokenBalance.user_id == user.id)
        .all()
    )
    items = []
    for bal, tok in rows:
        items.append({
            "token_id": tok.id,
            "symbol": tok.symbol,
            "name": tok.name,
            "amount": float(bal.amount or 0),
        })
    return jsonify({"items": items})


def _get_or_create_token_balance(user_id: int, token_id: int) -> TokenBalance:
    row = (
        TokenBalance.query.filter_by(user_id=user_id, token_id=token_id)
        .with_for_update()
        .first()
    )
    if not row:
        row = TokenBalance(user_id=user_id, token_id=token_id, amount=Decimal("0"))
        db.session.add(row)
        db.session.flush()
    return row


@api_bp.post("/amm/airdrop")
@require_auth
@csrf.exempt
def amm_airdrop():
    """Admin: credit a user's token balance for testing."""
    actor = _get_user_from_jwt()
    if not _is_admin(actor):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True)

    target_user_id = data.get("target_user_id")
    target_pubkey = data.get("target_pubkey")
    target_npub = data.get("target_npub")
    token_id = data.get("token_id")
    symbol = data.get("symbol")
    amount_raw = data.get("amount")

    # Resolve user
    target_user: User | None = None
    if isinstance(target_user_id, int):
        target_user = db.session.get(User, target_user_id)
    if not target_user and isinstance(target_pubkey, str):
        target_user = User.query.filter_by(pubkey_hex=target_pubkey.lower()).first()
    if not target_user and isinstance(target_npub, str):
        target_user = User.query.filter_by(npub=target_npub).first()
    if not target_user:
        return jsonify({"error": "target_user_not_found"}), 404

    # Resolve token
    tok: Token | None = None
    if isinstance(token_id, int):
        tok = db.session.get(Token, token_id)
    if not tok and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404

    # Parse amount
    try:
        amount = _parse_decimal(amount_raw)
    except Exception:
        return jsonify({"error": "invalid_amount"}), 400
    if amount <= 0:
        return jsonify({"error": "amount_must_be_positive"}), 400

    bal = _get_or_create_token_balance(target_user.id, tok.id)
    bal.amount = (bal.amount or Decimal("0")) + amount
    db.session.add(bal)
    db.session.commit()
    return jsonify({
        "user_id": target_user.id,
        "token_id": tok.id,
        "symbol": tok.symbol,
        "amount": float(bal.amount),
    }), 201


@api_bp.get("/amm/pools/<int:pool_id>/trades")
def amm_pool_trades(pool_id: int):
    limit = max(1, min(500, request.args.get("limit", default=100, type=int)))
    rows = (
        SwapTrade.query.filter_by(pool_id=pool_id)
        .order_by(SwapTrade.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [t.to_dict() for t in rows]})


@api_bp.get("/amm/pools/<int:pool_id>/burns")
def amm_pool_burns(pool_id: int):
    limit = max(1, min(500, request.args.get("limit", default=100, type=int)))
    rows = (
        BurnEvent.query.filter_by(pool_id=pool_id)
        .order_by(BurnEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [b.to_dict() for b in rows]})


# ---- Fees: distribution rules & payouts ----

def _default_fee_rule_dict(pool_id: int) -> dict:
    return {
        "pool_id": pool_id,
        "creator_user_id": None,
        "minter_user_id": None,
        "treasury_account": None,
        "bps_creator": 5000,
        "bps_minter": 3000,
        "bps_treasury": 2000,
    }


def _fee_rule_to_dict(r: FeeDistributionRule) -> dict:
    return {
        "pool_id": r.pool_id,
        "creator_user_id": r.creator_user_id,
        "minter_user_id": r.minter_user_id,
        "treasury_account": r.treasury_account,
        "bps_creator": int(r.bps_creator),
        "bps_minter": int(r.bps_minter),
        "bps_treasury": int(r.bps_treasury),
    }


@api_bp.get("/fees/pools/<int:pool_id>/rule")
def fees_get_rule(pool_id: int):
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return jsonify({"error": "pool_not_found"}), 404
    r = FeeDistributionRule.query.filter_by(pool_id=pool_id).first()
    return jsonify(_fee_rule_to_dict(r) if r else _default_fee_rule_dict(pool_id))


@api_bp.post("/fees/pools/<int:pool_id>/rule")
@require_auth
@csrf.exempt
def fees_set_rule(pool_id: int):
    actor = _get_user_from_jwt()
    if not _is_admin(actor):
        return jsonify({"error": "forbidden"}), 403
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return jsonify({"error": "pool_not_found"}), 404
    data = request.get_json(force=True)
    bps_creator = int(data.get("bps_creator", 5000))
    bps_minter = int(data.get("bps_minter", 3000))
    bps_treasury = int(data.get("bps_treasury", 2000))
    if bps_creator < 0 or bps_minter < 0 or bps_treasury < 0 or (bps_creator + bps_minter + bps_treasury) != 10000:
        return jsonify({"error": "bps_must_sum_to_10000"}), 400
    creator_user_id = data.get("creator_user_id")
    minter_user_id = data.get("minter_user_id")
    treasury_account = data.get("treasury_account")
    r = FeeDistributionRule.query.filter_by(pool_id=pool_id).first()
    if not r:
        r = FeeDistributionRule(pool_id=pool_id)
        db.session.add(r)
    r.creator_user_id = int(creator_user_id) if creator_user_id is not None else None
    r.minter_user_id = int(minter_user_id) if minter_user_id is not None else None
    r.treasury_account = treasury_account or None
    r.bps_creator = bps_creator
    r.bps_minter = bps_minter
    r.bps_treasury = bps_treasury
    db.session.add(r)
    db.session.commit()
    return jsonify(_fee_rule_to_dict(r))


def _fees_summary_for_pool(pool: SwapPool, rule: FeeDistributionRule | None) -> dict:
    # Base allocations from accumulated fees
    bps_c = int(rule.bps_creator if rule else 5000)
    bps_m = int(rule.bps_minter if rule else 3000)
    bps_t = int(rule.bps_treasury if rule else 2000)
    fa = Decimal(pool.fee_accum_a or 0)
    fb = Decimal(pool.fee_accum_b or 0)
    def allocs(bps: int):
        return {
            "A": (fa * Decimal(bps) / Decimal(10000)),
            "B": (fb * Decimal(bps) / Decimal(10000)),
        }
    # Totals paid so far from payouts table
    def paid(entity: str):
        rows = FeePayout.query.filter_by(pool_id=pool.id, entity=entity).all()
        totA = Decimal("0")
        totB = Decimal("0")
        for p in rows:
            if p.asset == "A":
                totA += Decimal(p.amount or 0)
            elif p.asset == "B":
                totB += Decimal(p.amount or 0)
        return {"A": totA, "B": totB}

    out = {}
    for entity, bps in (("creator", bps_c), ("minter", bps_m), ("treasury", bps_t)):
        a = allocs(bps)
        p = paid(entity)
        out[entity] = {
            "alloc": {"A": float(a["A"]), "B": float(a["B"])},
            "paid": {"A": float(p["A"]), "B": float(p["B"])},
            "pending": {"A": float(max(Decimal("0"), a["A"] - p["A"])), "B": float(max(Decimal("0"), a["B"] - p["B"]))},
        }
    return out


@api_bp.get("/fees/pools/<int:pool_id>/summary")
def fees_summary(pool_id: int):
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return jsonify({"error": "pool_not_found"}), 404
    r = FeeDistributionRule.query.filter_by(pool_id=pool_id).first()
    return jsonify(_fees_summary_for_pool(pool, r))


@api_bp.post("/fees/payout")
@require_auth
@csrf.exempt
def fees_payout():
    actor = _get_user_from_jwt()
    if not _is_admin(actor):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True)
    pool_id = data.get("pool_id")
    entity = data.get("entity")  # creator|minter|treasury
    asset = data.get("asset")    # A|B
    amount_raw = data.get("amount")
    if entity not in {"creator", "minter", "treasury"} or asset not in {"A", "B"}:
        return jsonify({"error": "invalid_params"}), 400
    pool = db.session.get(SwapPool, int(pool_id)) if pool_id else None
    if not pool:
        return jsonify({"error": "pool_not_found"}), 404
    try:
        amount = _parse_decimal(amount_raw)
    except Exception:
        return jsonify({"error": "invalid_amount"}), 400
    if amount <= 0:
        return jsonify({"error": "amount_must_be_positive"}), 400
    rule = FeeDistributionRule.query.filter_by(pool_id=pool.id).first()
    summary = _fees_summary_for_pool(pool, rule)
    pending = Decimal(str(summary.get(entity, {}).get("pending", {}).get(asset, 0)))
    if amount > pending:
        return jsonify({"error": "amount_exceeds_pending", "pending": float(pending)}), 400
    p = FeePayout(pool_id=pool.id, entity=entity, asset=asset, amount=amount, note=(data.get("note") or None))
    db.session.add(p)
    db.session.commit()
    return jsonify({
        "ok": True,
        "payout": {
            "id": p.id,
            "pool_id": p.pool_id,
            "entity": p.entity,
            "asset": p.asset,
            "amount": float(p.amount or 0),
            "note": p.note,
            "created_at": p.created_at.isoformat() + "Z",
        }
    }), 201


# ---- Watchlist ----


@api_bp.get("/watchlist")
@require_auth
def watchlist_list():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    rows = (
        db.session.query(WatchlistItem, Token)
        .join(Token, Token.id == WatchlistItem.token_id)
        .filter(WatchlistItem.user_id == user.id)
        .order_by(Token.symbol.asc())
        .all()
    )
    items = []
    for wl, tok in rows:
        items.append({
            "token_id": tok.id,
            "symbol": tok.symbol,
            "name": tok.name,
        })
    return jsonify({"items": items})


@api_bp.post("/watchlist")
@require_auth
@csrf.exempt
def watchlist_add():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    data = request.get_json(force=True)
    token_id = data.get("token_id")
    symbol = data.get("symbol")
    tok = db.session.get(Token, int(token_id)) if token_id else None
    if not tok and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    exists = WatchlistItem.query.filter_by(user_id=user.id, token_id=tok.id).first()
    if exists:
        return jsonify({"ok": True})
    wl = WatchlistItem(user_id=user.id, token_id=tok.id)
    db.session.add(wl)
    db.session.commit()
    return jsonify({"ok": True}), 201


@api_bp.delete("/watchlist")
@require_auth
@csrf.exempt
def watchlist_remove():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    data = request.get_json(silent=True) or {}
    token_id = data.get("token_id")
    symbol = data.get("symbol")
    tok = db.session.get(Token, int(token_id)) if token_id else None
    if not tok and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    WatchlistItem.query.filter_by(user_id=user.id, token_id=tok.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ---- Alerts (price) ----


@api_bp.get("/alerts")
@require_auth
def alerts_list():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    rows = (
        db.session.query(AlertRule, Token)
        .join(Token, Token.id == AlertRule.token_id)
        .filter(AlertRule.user_id == user.id)
        .order_by(AlertRule.created_at.desc())
        .all()
    )
    items = []
    for r, tok in rows:
        items.append({
            "id": r.id,
            "token_id": tok.id,
            "symbol": tok.symbol,
            "condition": r.condition,
            "threshold": float(r.threshold),
            "active": bool(r.active),
            "created_at": r.created_at.isoformat() + "Z",
            "last_triggered_at": r.last_triggered_at.isoformat() + "Z" if r.last_triggered_at else None,
        })
    return jsonify({"items": items})


@api_bp.post("/alerts")
@require_auth
@csrf.exempt
def alerts_create():
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    data = request.get_json(force=True)
    token_id = data.get("token_id")
    symbol = data.get("symbol")
    condition = data.get("condition")  # price_above | price_below
    threshold_raw = data.get("threshold")
    if condition not in {"price_above", "price_below"}:
        return jsonify({"error": "invalid_condition"}), 400
    try:
        threshold = _parse_decimal(threshold_raw)
    except Exception:
        return jsonify({"error": "invalid_threshold"}), 400
    if token_id:
        tok = db.session.get(Token, int(token_id))
    else:
        tok = None
    if not tok and isinstance(symbol, str):
        tok = Token.query.filter_by(symbol=symbol).first()
    if not tok:
        return jsonify({"error": "token_not_found"}), 404
    r = AlertRule(user_id=user.id, token_id=tok.id, condition=condition, threshold=threshold, active=True)
    db.session.add(r)
    try:
        db.session.commit()
        return jsonify({"id": r.id}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "create_failed", "detail": str(e)}), 400


@api_bp.post("/alerts/<int:rule_id>/toggle")
@require_auth
@csrf.exempt
def alerts_toggle(rule_id: int):
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    r = db.session.get(AlertRule, rule_id)
    if not r or r.user_id != user.id:
        return jsonify({"error": "alert_not_found"}), 404
    r.active = not bool(r.active)
    db.session.add(r)
    db.session.commit()
    return jsonify({"ok": True, "active": bool(r.active)})


@api_bp.delete("/alerts/<int:rule_id>")
@require_auth
@csrf.exempt
def alerts_delete(rule_id: int):
    user = _get_user_from_jwt()
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    r = db.session.get(AlertRule, rule_id)
    if not r or r.user_id != user.id:
        return jsonify({"error": "alert_not_found"}), 404
    db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True})
