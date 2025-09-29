from __future__ import annotations

from typing import Optional
from datetime import datetime, timedelta

from app.extensions import db, cache
from app.models import (
    User,
    Token,
    TokenInfo,
    SwapPool,
    SwapTrade,
    WatchlistItem,
    CreatorFollow,
    FeeDistributionRule,
    FeePayout,
)
from sqlalchemy import case, func


def get_gusd_token() -> Optional[Token]:
    return Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()


def amm_price_for_token(token: Token) -> Optional[float]:
    """Compute AMM price for token against gUSD if such a pool exists."""
    gusd = get_gusd_token()
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


@cache.memoize(timeout=30)
def cached_trending_items():
    from datetime import timedelta as _td
    since = datetime.utcnow() - _td(days=1)
    gusd = get_gusd_token()
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
def cached_recent_launches():
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
def cached_top_creators():
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
def cached_stats():
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
    gusd = get_gusd_token()
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


@cache.memoize(timeout=5)
def fee_summary_for_pool_cached(pool_id: int):
    from decimal import Decimal as _D
    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        return None
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