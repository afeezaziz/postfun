from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from ..extensions import db
from ..models import Token


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
