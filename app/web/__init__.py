from __future__ import annotations

from functools import wraps
from typing import Optional
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
import time
import json

from flask import Blueprint, render_template, request, g, redirect, url_for, abort, flash, Response

from ..utils.jwt_utils import verify_jwt
from ..extensions import db
from ..models import User, Token


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


@web_bp.route("/")
def home():
    # Show top tokens by market cap as trending
    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).limit(8).all()
    return render_template("home.html", tokens=tokens)


@web_bp.route("/token/<symbol>")
def token_detail(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    return render_template("token_detail.html", token=token)


@web_bp.route("/dashboard")
@require_auth_web
def dashboard():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    return render_template("dashboard.html", user=user)


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
        qry = qry.order_by(sort_col.asc().nullslast())
    else:
        qry = qry.order_by(sort_col.desc().nullslast())

    total = qry.count()
    if page < 1:
        page = 1
    if per < 1:
        per = 12
    tokens = qry.limit(per).offset((page - 1) * per).all()
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
        qry = qry.order_by(sort_col.asc().nullslast())
    else:
        qry = qry.order_by(sort_col.desc().nullslast())

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
            return redirect(url_for("web.token_detail", symbol=symbol))
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

    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).all()
    items = [_compute_token_metrics(t) for t in tokens]

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
    )


@web_bp.route("/portfolio")
@require_auth_web
def portfolio():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).limit(4).all()
    holdings = [{"token": t, "amount": 0.0, "value": 0.0} for t in tokens]
    return render_template("portfolio.html", user=user, holdings=holdings)


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
    series = _mock_series(token)
    holders = _mock_holders(token)
    swaps = _mock_swaps(token)
    return render_template(
        "pool.html",
        token=token,
        series=series,
        holders=holders,
        swaps=swaps,
        confirm_trade_preview=False,
        trade_form=None,
    )


@web_bp.route("/pool/<symbol>/trade", methods=["POST"])
@require_auth_web
def pool_trade(symbol: str):
    token = Token.query.filter_by(symbol=symbol).first()
    if not token:
        abort(404)
    side = (request.form.get("side") or "buy").lower()
    amount_s = (request.form.get("amount") or "").strip()
    confirm_flag = request.form.get("confirm") == "yes"
    errors = []
    amt = None
    try:
        if amount_s:
            amt = Decimal(amount_s)
        else:
            errors.append("Amount is required")
    except (InvalidOperation, ValueError):
        errors.append("Invalid amount")
    if side not in {"buy", "sell"}:
        errors.append("Invalid side")
    if errors:
        for e in errors:
            flash(e, "error")
        # re-render with errors (no separate error slots in template; using flashes)
        series = _mock_series(token)
        holders = _mock_holders(token)
        swaps = _mock_swaps(token)
        return render_template(
            "pool.html",
            token=token,
            series=series,
            holders=holders,
            swaps=swaps,
            confirm_trade_preview=False,
            trade_form={"side": side, "amount": amount_s},
        ), 400

    # Confirm step: if not confirmed, show preview
    if not confirm_flag:
        series = _mock_series(token)
        holders = _mock_holders(token)
        swaps = _mock_swaps(token)
        price = Decimal(token.price or 0) or Decimal("0")
        total = (price * amt).quantize(Decimal("0.00000001"))
        return render_template(
            "pool.html",
            token=token,
            series=series,
            holders=holders,
            swaps=swaps,
            confirm_trade_preview=True,
            trade_form={"side": side, "amount": str(amt), "price": str(price), "total": str(total)},
        )

    # Confirmed: mock execution: adjust price slightly and flash success
    try:
        price = Decimal(token.price or 0) or Decimal("0")
        if side == "buy":
            # buying increases price slightly
            token.price = (price * Decimal("1.002")).quantize(Decimal("0.00000001"))
        else:
            token.price = (price * Decimal("0.998")).quantize(Decimal("0.00000001"))
        db.session.commit()
        flash(f"Executed mock {side} of {amt} {symbol}", "success")
    except Exception:
        db.session.rollback()
        flash("Trade failed", "error")
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
    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).all()
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
    for t in Token.query.order_by(Token.market_cap.desc().nullslast()).all():
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
            price = float(t.price or 0) if t and t.price is not None else 0.0
            data = json.dumps({"symbol": sym, "price": price})
            yield f"data: {data}\n\n"
            time.sleep(5)

    return Response(event_stream(symbol), mimetype="text/event-stream", headers={
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
        qry = qry.order_by(sort_col.asc().nullslast())
    else:
        qry = qry.order_by(sort_col.desc().nullslast())

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

    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).all()
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
    tokens = Token.query.order_by(Token.market_cap.desc().nullslast()).all()
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
