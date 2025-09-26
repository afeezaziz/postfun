from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from ..extensions import db
from ..models import AlertRule, AlertEvent, Token, User
from .nostr_dm import send_dm


MIN_EVENT_INTERVAL = timedelta(minutes=5)


def _should_trigger(condition: str, *, price: Decimal, threshold: Decimal, mcap: Decimal | None = None, pct_change: Decimal | None = None) -> bool:
    if condition == "price_above":
        return price > threshold
    if condition == "price_below":
        return price < threshold
    if condition == "market_cap_above" and mcap is not None:
        return mcap > threshold
    if condition == "market_cap_below" and mcap is not None:
        return mcap < threshold
    if condition == "pct_change_above" and pct_change is not None:
        return pct_change > threshold
    if condition == "pct_change_below" and pct_change is not None:
        return pct_change < threshold
    return False


def evaluate_alerts(now: datetime | None = None) -> int:
    """Evaluate active alert rules and create AlertEvent rows when triggered.

    Returns the number of events recorded.
    """
    now = now or datetime.utcnow()
    rules = AlertRule.query.filter_by(active=True).all()
    if not rules:
        return 0

    # Preload prices to reduce queries
    token_ids = {r.token_id for r in rules}
    tokens = Token.query.filter(Token.id.in_(token_ids)).all()
    price_by_id: dict[int, Decimal] = {}
    mcap_by_id: dict[int, Decimal] = {}
    change_by_id: dict[int, Decimal] = {}
    for t in tokens:
        try:
            price_by_id[t.id] = Decimal(t.price or 0)
        except Exception:
            price_by_id[t.id] = Decimal(0)
        try:
            mcap_by_id[t.id] = Decimal(t.market_cap or 0)
        except Exception:
            mcap_by_id[t.id] = Decimal(0)
        try:
            change_by_id[t.id] = Decimal(t.change_24h or 0)
        except Exception:
            change_by_id[t.id] = Decimal(0)

    events_created = 0
    for r in rules:
        price = price_by_id.get(r.token_id, Decimal(0))
        try:
            threshold = Decimal(r.threshold)
        except Exception:
            continue
        if not _should_trigger(
            r.condition,
            price=price,
            threshold=threshold,
            mcap=mcap_by_id.get(r.token_id),
            pct_change=change_by_id.get(r.token_id),
        ):
            continue
        # rate-limit events per rule
        if r.last_triggered_at and now - r.last_triggered_at < MIN_EVENT_INTERVAL:
            continue
        # create event
        ev = AlertEvent(rule_id=r.id, triggered_at=now, price=price)
        db.session.add(ev)
        r.last_triggered_at = now
        try:
            db.session.commit()
            events_created += 1
        except Exception:
            db.session.rollback()
            continue
        # Send Nostr DM best-effort
        try:
            # Resolve user pubkey_hex from rule
            user = db.session.get(User, r.user_id)
            token = db.session.get(Token, r.token_id)
            if user and user.pubkey_hex and token:
                msg = (
                    f"Alert triggered: {token.symbol} {r.condition.replace('_', ' ')} threshold={threshold} "
                    f"price={price}"
                )
                send_dm(user.pubkey_hex, msg)
        except Exception:
            # best-effort only
            pass
    return events_created
