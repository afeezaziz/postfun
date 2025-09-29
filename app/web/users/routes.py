from __future__ import annotations

from functools import wraps
from typing import Optional
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import urllib.parse

from flask import render_template, request, g, redirect, url_for, abort, flash, Response, current_app, session
from sqlalchemy import case, func
from requests_oauthlib import OAuth2Session

from ...utils.jwt_utils import verify_jwt
from ...extensions import db, cache
from ...models import (
    User,
    Token,
    TokenInfo,
    SwapTrade,
    SwapPool,
    TokenBalance,
    CreatorFollow,
    FeeDistributionRule,
    FeePayout,
    TwitterUser,
    UserTwitterConnection,
    LightningInvoice,
    LightningWithdrawal,
)
from ...services.amm import quote_swap
from flask import jsonify

from . import users_bp

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


def _get_gusd_token():
    from ...models import Token
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


# User profile routes
@users_bp.route("/profile")
@require_auth_web
def user_profile():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = None
    if isinstance(uid, int):
        user = db.session.get(User, uid)
    if not user:
        abort(404)

    return render_template("profile.html", user=user)


@users_bp.route("/users/<path:identifier>")
def user_profile_by_identifier(identifier: str):
    """Route to find user by npub or @twitter_username"""
    user = None
    twitter_user = None
    profile_type = "npub"  # default

    # Check if it's an npub (starts with 'npub1')
    if identifier.startswith("npub1"):
        user = User.query.filter_by(npub=identifier).first()
        profile_type = "npub"

    # Check if it's a Twitter username (starts with '@')
    elif identifier.startswith("@"):
        twitter_username = identifier[1:]  # Remove '@'
        twitter_user = TwitterUser.query.filter_by(username=twitter_username).first()

        if twitter_user:
            # Find the connected user
            connection = UserTwitterConnection.query.filter_by(twitter_user_id=twitter_user.id).first()
            if connection:
                user = connection.user
                profile_type = "twitter"
            else:
                # Twitter user exists but not connected to a platform user
                return render_template("twitter_profile.html", twitter_user=twitter_user, connected=False)

    # If no user found, return 404
    if not user:
        abort(404)

    # Calculate user statistics (same as existing user_profile route)
    total_trades = SwapTrade.query.filter_by(user_id=user.id).count()

    # Calculate total volume in gUSD
    total_volume = 0
    gusd = _get_gusd_token()
    if gusd:
        user_trades = SwapTrade.query.filter_by(user_id=user.id).all()
        for trade in user_trades:
            pool = SwapPool.query.get(trade.pool_id)
            if pool:
                if pool.token_a_id == gusd.id:
                    total_volume += float(trade.amount_in if trade.side == "AtoB" else trade.amount_out)
                elif pool.token_b_id == gusd.id:
                    total_volume += float(trade.amount_out if trade.side == "AtoB" else trade.amount_in)

    tokens_created = TokenInfo.query.filter_by(launch_user_id=user.id).count()
    holdings_count = TokenBalance.query.filter_by(user_id=user.id, amount=0).count()

    # Calculate portfolio value (simplified)
    portfolio_value = 0
    if gusd:
        holdings = TokenBalance.query.filter_by(user_id=user.id).all()
        for holding in holdings:
            token = Token.query.get(holding.token_id)
            if token and token.price:
                portfolio_value += float(holding.amount) * float(token.price)

    user_stats = {
        "total_trades": total_trades,
        "total_volume": total_volume,
        "tokens_created": tokens_created,
        "holdings_count": holdings_count,
        "portfolio_value": portfolio_value,
        "portfolio_change_24h": 0  # Placeholder for now
    }

    # Get Twitter connection info if available
    twitter_connection = None
    if user.twitter_connection:
        twitter_connection = user.twitter_connection
        twitter_user = twitter_connection.twitter_user

    return render_template("user.html",
                         user=user,
                         user_stats=user_stats,
                         profile_type=profile_type,
                         twitter_connection=twitter_connection,
                         twitter_user=twitter_user)


# Creator profile routes
@users_bp.route("/creator/<int:user_id>")
def creator_profile(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    launches = TokenInfo.query.filter_by(launch_user_id=user.id).order_by(TokenInfo.launch_at.desc()).all()
    tokens = []
    for info in launches:
        t = db.session.get(Token, info.token_id)
        if t:
            tokens.append(t)
    price_by_symbol = {t.symbol: (_amm_price_for_token(t) or float(t.price or 0)) for t in tokens if t and t.symbol}
    follower_count = CreatorFollow.query.filter_by(creator_user_id=user.id).count()
    # follow status
    is_following = False
    payload = get_jwt_from_cookie()
    if payload and isinstance(payload.get("uid"), int):
        me = db.session.get(User, int(payload.get("uid")))
        if me:
            is_following = CreatorFollow.query.filter_by(follower_user_id=me.id, creator_user_id=user.id).first() is not None

    # Aggregate fee summary for this creator (based on rules assigning creator_user_id)
    from decimal import Decimal as _D
    rules = FeeDistributionRule.query.filter_by(creator_user_id=user.id).all()
    total = {"allocA": 0.0, "allocB": 0.0, "paidA": 0.0, "paidB": 0.0, "pendingA": 0.0, "pendingB": 0.0}
    items = []
    gusd = _get_gusd_token()
    for r in rules:
        pool = db.session.get(SwapPool, r.pool_id)
        if not pool:
            continue
        fa = _D(pool.fee_accum_a or 0); fb = _D(pool.fee_accum_b or 0)
        bps = int(r.bps_creator or 0)
        allocA = fa * _D(bps) / _D(10000)
        allocB = fb * _D(bps) / _D(10000)
        # paid for creator entity
        rows = FeePayout.query.filter_by(pool_id=pool.id, entity="creator").all()
        paidA = _D("0"); paidB = _D("0")
        for p in rows:
            if p.asset == "A": paidA += _D(p.amount or 0)
            elif p.asset == "B": paidB += _D(p.amount or 0)
        pendA = max(_D("0"), allocA - paidA)
        pendB = max(_D("0"), allocB - paidB)
        # Figure display symbol (prefer non-gUSD token)
        tokA = db.session.get(Token, pool.token_a_id)
        tokB = db.session.get(Token, pool.token_b_id)
        disp_token = tokA
        if gusd and tokA and tokA.id == (gusd.id if gusd else -1):
            disp_token = tokB
        elif gusd and tokB and tokB.id == (gusd.id if gusd else -1):
            disp_token = tokA
        items.append({
            "pool_id": pool.id,
            "symbol": disp_token.symbol if disp_token else (tokA.symbol if tokA else '?'),
            "allocA": float(allocA), "allocB": float(allocB),
            "paidA": float(paidA), "paidB": float(paidB),
            "pendingA": float(pendA), "pendingB": float(pendB),
        })
        total["allocA"] += float(allocA); total["allocB"] += float(allocB)
        total["paidA"] += float(paidA); total["paidB"] += float(paidB)
        total["pendingA"] += float(pendA); total["pendingB"] += float(pendB)

    meta_title = f"Creator — {user.npub or user.pubkey_hex} | Postfun"
    meta_url = url_for("web.users.creator_profile", user_id=user.id, _external=True)
    return render_template(
        "creator.html",
        creator=user,
        launches=launches,
        tokens=tokens,
        follower_count=follower_count,
        is_following=is_following,
        price_by_symbol=price_by_symbol,
        creator_fee_summary={"total": total, "items": items},
        meta_title=meta_title,
        meta_description="Creator profile on Postfun.",
        meta_url=meta_url,
    )


@users_bp.route("/creator/<int:user_id>/follow", methods=["POST"])
@require_auth_web
def creator_follow(user_id: int):
    payload = g.jwt_payload
    uid = payload.get("uid") if payload else None
    me = db.session.get(User, uid) if isinstance(uid, int) else None
    if not me:
        return redirect(url_for("web.home"))
    if me.id == user_id:
        flash("You cannot follow yourself", "error")
        return redirect(url_for("web.users.creator_profile", user_id=user_id))
    exists = CreatorFollow.query.filter_by(follower_user_id=me.id, creator_user_id=user_id).first()
    if not exists:
        db.session.add(CreatorFollow(follower_user_id=me.id, creator_user_id=user_id))
        try:
            db.session.commit()
            flash("Followed creator", "success")
        except Exception:
            db.session.rollback()
            flash("Could not follow", "error")
    return redirect(url_for("web.users.creator_profile", user_id=user_id))


@users_bp.route("/creator/<int:user_id>/unfollow", methods=["POST"])
@require_auth_web
def creator_unfollow(user_id: int):
    payload = g.jwt_payload
    uid = payload.get("uid") if payload else None
    me = db.session.get(User, uid) if isinstance(uid, int) else None
    if not me:
        return redirect(url_for("web.home"))
    row = CreatorFollow.query.filter_by(follower_user_id=me.id, creator_user_id=user_id).first()
    if row:
        try:
            db.session.delete(row)
            db.session.commit()
            flash("Unfollowed creator", "success")
        except Exception:
            db.session.rollback()
            flash("Could not unfollow", "error")
    return redirect(url_for("web.users.creator_profile", user_id=user_id))


# Wallet route
@users_bp.route("/wallet")
@require_auth_web
def wallet():
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return redirect(url_for("web.home"))

    # Get user's token balances
    balances = (
        TokenBalance.query
        .join(Token, TokenBalance.token_id == Token.id)
        .filter(TokenBalance.user_id == user.id, TokenBalance.amount > 0)
        .order_by(TokenBalance.amount.desc())
        .all()
    )

    # Calculate total balance and individual values
    total_balance = 0.0
    for balance in balances:
        price = _amm_price_for_token(balance.token) or float(balance.token.price or 0)
        value = float(balance.amount or 0) * price
        balance.value = value
        total_balance += value

    # Get price map for tokens
    price_by_symbol = {balance.token.symbol: (_amm_price_for_token(balance.token) or float(balance.token.price or 0)) for balance in balances}

    # Get lightning invoices
    lightning_invoices = (
        LightningInvoice.query
        .filter_by(user_id=user.id)
        .order_by(LightningInvoice.created_at.desc())
        .limit(10)
        .all()
    )

    # Get lightning withdrawals
    lightning_withdrawals = (
        LightningWithdrawal.query
        .filter_by(user_id=user.id)
        .order_by(LightningWithdrawal.created_at.desc())
        .limit(10)
        .all()
    )

    # Get recent activity (mock data for now)
    recent_activity = []

    return render_template(
        "wallet.html",
        user=user,
        balances=balances,
        total_balance=total_balance,
        price_by_symbol=price_by_symbol,
        recent_activity=recent_activity,
        lightning_invoices=lightning_invoices,
        lightning_withdrawals=lightning_withdrawals,
        meta_title="Wallet — Postfun",
        meta_description="Your wallet balances and activity on Postfun.",
        meta_url=url_for("web.users.wallet", _external=True),
    )


# Dashboard route
@users_bp.route("/dashboard")
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
        from ...models import WatchlistItem, AlertRule
        wl_count = WatchlistItem.query.filter_by(user_id=user.id).count()
        alerts_count = AlertRule.query.filter_by(user_id=user.id).count()

    # Trending by AMM 24h volume (gUSD pairs)
    from datetime import timedelta as _td

    trending = []
    since = datetime.utcnow() - _td(days=1)
    gusd = _get_gusd_token()
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


# Portfolio route
@users_bp.route("/portfolio")
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
    return render_template(
        "portfolio.html",
        user=user,
        holdings=holdings,
        price_by_symbol=price_by_symbol,
        meta_title="Portfolio — Postfun",
        meta_description="Your holdings on Postfun.",
        meta_url=url_for("web.users.portfolio", _external=True),
    )


# Twitter connection API endpoints
@users_bp.route("/api/connect-twitter", methods=["POST"])
@require_auth_web
def connect_twitter():
    """Connect Twitter account to user profile"""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    data = request.get_json()
    if not data or "username" not in data:
        return jsonify({"success": False, "error": "Twitter username is required"}), 400

    username = data["username"].strip().lstrip("@")
    if not username:
        return jsonify({"success": False, "error": "Invalid Twitter username"}), 400

    # Check if user already has a Twitter connection
    if user.twitter_connection:
        return jsonify({"success": False, "error": "Twitter account already connected"}), 400

    # Find or create Twitter user
    twitter_user = TwitterUser.query.filter_by(username=username).first()
    if not twitter_user:
        # Create new Twitter user record
        twitter_user = TwitterUser(
            username=username,
            display_name=username,
            created_at=datetime.utcnow()
        )
        db.session.add(twitter_user)
        db.session.flush()  # Get ID

    # Check if Twitter user is already connected to another user
    if twitter_user.user_connection:
        return jsonify({"success": False, "error": "Twitter account already connected to another user"}), 400

    # Create connection
    connection = UserTwitterConnection(
        user_id=user.id,
        twitter_user_id=twitter_user.id,
        connected_at=datetime.utcnow(),
        verified=False
    )
    db.session.add(connection)

    try:
        db.session.commit()
        return jsonify({
            "success": True,
            "message": "Twitter account connected successfully",
            "username": username
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": f"Failed to connect Twitter account: {str(e)}"}), 500


@users_bp.route("/api/disconnect-twitter", methods=["POST"])
@require_auth_web
def disconnect_twitter():
    """Disconnect Twitter account from user profile"""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if not user.twitter_connection:
        return jsonify({"success": False, "error": "No Twitter account connected"}), 400

    try:
        # Delete the connection (but keep the Twitter user record)
        db.session.delete(user.twitter_connection)
        db.session.commit()
        return jsonify({
            "success": True,
            "message": "Twitter account disconnected successfully"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": f"Failed to disconnect Twitter account: {str(e)}"}), 500


# Twitter OAuth2 configuration
TWITTER_CLIENT_ID = None
TWITTER_CLIENT_SECRET = None
TWITTER_AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TWITTER_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
TWITTER_SCOPES = ["tweet.read", "users.read", "offline.access"]

def get_twitter_oauth():
    """Initialize Twitter OAuth2 session"""
    global TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET

    if not TWITTER_CLIENT_ID:
        TWITTER_CLIENT_ID = current_app.config.get('TWITTER_CLIENT_ID')
        TWITTER_CLIENT_SECRET = current_app.config.get('TWITTER_CLIENT_SECRET')

    if not TWITTER_CLIENT_ID or not TWITTER_CLIENT_SECRET:
        raise ValueError("Twitter OAuth2 credentials not configured")

    # Generate redirect URI and force HTTPS for production
    redirect_uri = url_for('web.users.twitter_callback', _external=True)
    if not current_app.debug and not redirect_uri.startswith('https://'):
        redirect_uri = redirect_uri.replace('http://', 'https://', 1)

    return OAuth2Session(
        client_id=TWITTER_CLIENT_ID,
        scope=TWITTER_SCOPES,
        redirect_uri=redirect_uri
    )


# Twitter OAuth2 authentication routes
@users_bp.route("/twitter/auth")
@require_auth_web
def twitter_auth():
    """Initiate Twitter OAuth2 authentication"""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        flash("You must be logged in to connect Twitter", "error")
        return redirect(url_for("web.home"))

    # Check if user already has Twitter connected
    if user.twitter_connection:
        flash("Twitter account already connected", "info")
        return redirect(url_for("web.users.user_profile"))

    try:
        twitter = get_twitter_oauth()
        authorization_url, state = twitter.authorization_url(
            TWITTER_AUTH_URL,
            # Optional parameters
            code_challenge="challenge",
            code_challenge_method="plain"
        )

        # Store state in session for security
        session['twitter_oauth_state'] = state
        session['twitter_user_id'] = user.id

        return redirect(authorization_url)

    except Exception as e:
        current_app.logger.error(f"Twitter OAuth2 error: {str(e)}")
        flash("Failed to initiate Twitter authentication", "error")
        return redirect(url_for("web.users.user_profile"))


@users_bp.route("/twitter/callback")
@require_auth_web
def twitter_callback():
    """Handle Twitter OAuth2 callback"""
    payload = g.jwt_payload
    uid = payload.get("uid")
    user = db.session.get(User, uid) if isinstance(uid, int) else None
    if not user:
        flash("Authentication required", "error")
        return redirect(url_for("web.home"))

    # Verify state matches
    state = session.get('twitter_oauth_state')
    callback_state = request.args.get('state')

    if not state or state != callback_state:
        flash("Invalid OAuth2 state", "error")
        return redirect(url_for("web.users.user_profile"))

    # Check for error response
    error = request.args.get('error')
    if error:
        error_description = request.args.get('error_description', 'Unknown error')
        flash(f"Twitter authentication failed: {error_description}", "error")
        return redirect(url_for("web.users.user_profile"))

    # Get authorization code
    code = request.args.get('code')
    if not code:
        flash("Authorization code not received", "error")
        return redirect(url_for("web.users.user_profile"))

    try:
        # Exchange authorization code for access token
        twitter = get_twitter_oauth()
        token = twitter.fetch_token(
            TWITTER_TOKEN_URL,
            client_secret=current_app.config.get('TWITTER_CLIENT_SECRET'),
            code=code
        )

        # Get user information from Twitter API
        response = twitter.get(
            "https://api.twitter.com/2/users/me",
            params={"user.fields": "public_metrics,profile_image_url,verified,description"}
        )

        if response.status_code != 200:
            flash("Failed to fetch Twitter user information", "error")
            return redirect(url_for("web.users.user_profile"))

        twitter_user_data = response.json().get('data', {})
        twitter_user_id = twitter_user_data.get('id')
        username = twitter_user_data.get('username')
        display_name = twitter_user_data.get('name', username)

        if not twitter_user_id or not username:
            flash("Invalid Twitter user data received", "error")
            return redirect(url_for("web.users.user_profile"))

        # Find or create Twitter user record
        twitter_user = TwitterUser.query.filter_by(twitter_user_id=twitter_user_id).first()

        if not twitter_user:
            twitter_user = TwitterUser(
                twitter_user_id=twitter_user_id,
                username=username,
                display_name=display_name,
                description=twitter_user_data.get('description'),
                profile_image_url=twitter_user_data.get('profile_image_url'),
                verified=twitter_user_data.get('verified', False),
                followers_count=twitter_user_data.get('public_metrics', {}).get('followers_count', 0),
                following_count=twitter_user_data.get('public_metrics', {}).get('following_count', 0),
                tweet_count=twitter_user_data.get('public_metrics', {}).get('tweet_count', 0),
                created_at=datetime.utcnow()
            )
            db.session.add(twitter_user)
            db.session.flush()

        # Check if Twitter user is already connected to another user
        if twitter_user.user_connection and twitter_user.user_connection.user_id != user.id:
            flash("This Twitter account is already connected to another user", "error")
            return redirect(url_for("web.users.user_profile"))

        # Create or update connection
        if not user.twitter_connection:
            connection = UserTwitterConnection(
                user_id=user.id,
                twitter_user_id=twitter_user.id,
                connected_at=datetime.utcnow(),
                verified=True
            )
            db.session.add(connection)
        else:
            user.twitter_connection.verified = True
            user.twitter_connection.connected_at = datetime.utcnow()

        # Store access token for future API calls
        session['twitter_access_token'] = token.get('access_token')
        session['twitter_refresh_token'] = token.get('refresh_token')

        db.session.commit()

        flash("Twitter account connected successfully!", "success")
        return redirect(url_for("web.users.user_profile"))

    except Exception as e:
        current_app.logger.error(f"Twitter OAuth2 callback error: {str(e)}")
        flash("Failed to complete Twitter authentication", "error")
        return redirect(url_for("web.users.user_profile"))

    finally:
        # Clean up session
        session.pop('twitter_oauth_state', None)
        session.pop('twitter_user_id', None)