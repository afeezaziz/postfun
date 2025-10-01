from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, List, Dict
from datetime import datetime, timedelta

from ..extensions import db
from ..models import Token, SwapPool, SwapTrade


@dataclass
class MarketTick:
    symbol: str
    price: Decimal


class MarketDataProvider:
    """Abstract data provider.

    Replace this with a real implementation that calls an external API.
    """

    def fetch_prices(self, symbols: Iterable[str]) -> list[MarketTick]:
        raise NotImplementedError


class MockMarketDataProvider(MarketDataProvider):
    """Deterministic mock provider that produces gentle price motion per symbol."""

    def fetch_prices(self, symbols: Iterable[str]) -> list[MarketTick]:
        now = time.time()
        out: list[MarketTick] = []
        for sym in symbols:
            # Seed curve by symbol to keep motion stable across restarts
            seed = sum(ord(c) for c in sym) or 1
            # Oscillate +/- 1% around current DB price using a slow sine wave
            t = now / 30.0  # ~30s period
            factor = 1 + 0.01 * math.sin(t + seed % 10)
            tok: Token | None = Token.query.filter_by(symbol=sym).first()
            base = Decimal(tok.price or 1) if tok else Decimal("1")
            price = (base * Decimal(f"{factor:.8f}")).quantize(Decimal("0.00000001"))
            out.append(MarketTick(symbol=sym, price=price))
        return out


def refresh_all_tokens(provider: MarketDataProvider | None = None) -> int:
    """Fetch latest prices for all tokens and persist.

    Returns number of tokens updated.
    """
    if provider is None:
        provider = MockMarketDataProvider()
    tokens = Token.query.all()
    if not tokens:
        return 0
    by_symbol = {t.symbol: t for t in tokens}
    ticks = provider.fetch_prices(by_symbol.keys())
    n = 0
    for tick in ticks:
        tok = by_symbol.get(tick.symbol)
        if not tok:
            continue
        try:
            old = Decimal(tok.price or 0)
            tok.price = tick.price
            # naive change_24h update to keep non-null
            if old > 0:
                pct = ((tok.price - old) / old) * Decimal(100)
                tok.change_24h = pct.quantize(Decimal("0.0001"))
            n += 1
        except Exception:
            pass
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return n


# ---- OHLC aggregation ----

_BUCKET_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}


def _preferred_pool_for_token(token_id: int) -> Optional[SwapPool]:
    from ..models import Token as _T
    tok = Token.query.get(token_id)
    if not tok:
        return None
    gusd = _T.query.filter_by(symbol="GUSD").first() or _T.query.filter_by(symbol="gUSD").first()
    pool = None
    if gusd:
        pool = (
            SwapPool.query.filter(
                ((SwapPool.token_a_id == tok.id) & (SwapPool.token_b_id == gusd.id))
                | ((SwapPool.token_b_id == tok.id) & (SwapPool.token_a_id == gusd.id))
            ).first()
        )
    if not pool:
        pool = SwapPool.query.filter((SwapPool.token_a_id == tok.id) | (SwapPool.token_b_id == tok.id)).first()
    return pool


def aggregate_candles_from_trades(token_id: int, interval: str = "1m", since: Optional[datetime] = None) -> List[Dict]:
    bucket_seconds = _BUCKET_SECONDS.get(interval)
    if not bucket_seconds:
        return []
    pool = _preferred_pool_for_token(token_id)
    if not pool:
        return []
    # Determine counter-token (gUSD) and whether token is A or B
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
    token_is_a = (pool.token_a_id == token_id)
    now = datetime.utcnow()
    if since is None:
        since = now - timedelta(hours=24)
    rows = (
        SwapTrade.query
        .filter(SwapTrade.pool_id == pool.id, SwapTrade.created_at >= since)
        .order_by(SwapTrade.created_at.asc())
        .all()
    )
    from collections import OrderedDict
    buckets = OrderedDict()

    def trade_price_and_volume(t: SwapTrade):
        pr = None
        vol = None
        if gusd and pool.token_b_id == gusd.id:
            # price = gUSD per token (A is token, B is gUSD)
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
        return (float(pr) if pr is not None else None), float(vol or 0)

    for t in rows:
        pr, vol = trade_price_and_volume(t)
        if pr is None:
            continue
        ts = int(t.created_at.timestamp())
        bucket_ts = (ts // bucket_seconds) * bucket_seconds
        start_at = datetime.utcfromtimestamp(bucket_ts)
        b = buckets.get(start_at)
        if b is None:
            buckets[start_at] = {"o": pr, "h": pr, "l": pr, "c": pr, "v": vol}
        else:
            b["h"] = max(b["h"], pr)
            b["l"] = min(b["l"], pr)
            b["c"] = pr
            b["v"] += vol

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
    return items


def persist_candles_for_token(token_id: int, intervals: Iterable[str] = ("1m", "5m", "1h"), window: str = "24h") -> int:
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
        since = now - timedelta(days=1)

    n = 0
    for iv in intervals:
        items = aggregate_candles_from_trades(token_id, iv, since)
        for it in items:
            try:
                ts = datetime.fromisoformat(it["t"].replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue
            # Upsert by (token_id, interval, ts)
            row = (
                OHLCCandle.query
                .filter_by(token_id=token_id, interval=iv, ts=ts)
                .first()
            )
            if not row:
                row = OHLCCandle(token_id=token_id, interval=iv, ts=ts, o=Decimal(it["o"]), h=Decimal(it["h"]), l=Decimal(it["l"]), c=Decimal(it["c"]), v=Decimal(str(it.get("v") or 0)))
                db.session.add(row)
            else:
                row.o = Decimal(it["o"]) ; row.h = Decimal(it["h"]) ; row.l = Decimal(it["l"]) ; row.c = Decimal(it["c"]) ; row.v = Decimal(str(it.get("v") or 0))
                db.session.add(row)
            n += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return n


def persist_candles_all_tokens(intervals: Iterable[str] = ("1m", "5m", "1h"), window: str = "24h") -> int:
    tokens = Token.query.all()
    total = 0
    for t in tokens:
        try:
            total += persist_candles_for_token(t.id, intervals, window)
        except Exception:
            db.session.rollback()
            continue
    return total
