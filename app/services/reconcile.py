from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from apscheduler.schedulers.background import BackgroundScheduler

from ..extensions import db
from sqlalchemy import func, and_
from ..models import (
    LightningInvoice,
    LightningWithdrawal,
    LedgerEntry,
    ProviderLog,
)
from .lightning import LNBitsClient


# TODO: AccountBalance has been removed - rewrite these functions to use User.sats instead
# def _get_or_create_balance(user_id: int, asset: str = "BTC") -> AccountBalance:
#     bal = (
#         AccountBalance.query
#         .filter_by(user_id=user_id, asset=asset)
#         .with_for_update()
#         .first()
#     )
#     if not bal:
#         bal = AccountBalance(user_id=user_id, asset=asset, balance_sats=0)
#         db.session.add(bal)
#         db.session.flush()
#     return bal


def reconcile_invoices_once() -> int:
    """Poll pending invoices and credit balances when paid."""
    now = datetime.utcnow()
    age_sec = int(current_app.config.get("RECONCILE_INVOICES_MIN_AGE_SEC", 30))
    cutoff = now - timedelta(seconds=age_sec)
    rows = (
        LightningInvoice.query
        .filter(LightningInvoice.status == "pending", LightningInvoice.created_at <= cutoff)
        .order_by(LightningInvoice.created_at.asc())
        .limit(50)
        .all()
    )
    n = 0
    if not rows:
        return n
    client = None
    try:
        client = LNBitsClient()
    except Exception:
        return 0
    for inv in rows:
        try:
            ok, res = client.get_payment_status(inv.payment_hash)
            if not ok:
                continue
            paid = bool(res.get("paid"))
            if paid:
                if inv.status != "paid":
                    inv.status = "paid"
                    inv.paid_at = datetime.utcnow()
                if not inv.credited:
                    bal = _get_or_create_balance(inv.user_id)
                    bal.balance_sats = int(bal.balance_sats) + int(inv.amount_sats)
                    db.session.add(bal)
                    db.session.add(LedgerEntry(
                        user_id=inv.user_id,
                        entry_type="deposit",
                        delta_sats=int(inv.amount_sats),
                        ref_type="invoice",
                        ref_id=inv.id,
                    ))
                    inv.credited = True
                    db.session.add(inv)
                    db.session.commit()
                    n += 1
        except Exception:
            db.session.rollback()
            continue
    return n


def _has_fee_ledger(ref_id: str) -> bool:
    row = (
        LedgerEntry.query
        .filter_by(ref_type="withdrawal", ref_id=ref_id, entry_type="fee")
        .first()
    )
    return bool(row)


def reconcile_withdrawals_once() -> int:
    """Poll pending withdrawals and mark confirmed; add fee ledger if available."""
    now = datetime.utcnow()
    age_sec = int(current_app.config.get("RECONCILE_WITHDRAW_MIN_AGE_SEC", 30))
    cutoff = now - timedelta(seconds=age_sec)
    rows = (
        LightningWithdrawal.query
        .filter(LightningWithdrawal.status == "pending", LightningWithdrawal.created_at <= cutoff)
        .order_by(LightningWithdrawal.created_at.asc())
        .limit(50)
        .all()
    )
    n = 0
    if not rows:
        return n
    client = None
    try:
        client = LNBitsClient()
    except Exception:
        return 0
    for w in rows:
        try:
            if not w.payment_hash:
                continue
            ok, res = client.get_payment_status(w.payment_hash)
            if not ok:
                continue
            paid = bool(res.get("paid"))
            if paid and w.status != "confirmed":
                w.status = "confirmed"
                w.processed_at = datetime.utcnow()
                fee = res.get("fee")
                if isinstance(fee, int) and fee > 0:
                    w.fee_sats = int(fee)
                    # Add a fee ledger if not already recorded
                    if not _has_fee_ledger(w.id):
                        db.session.add(LedgerEntry(
                            user_id=w.user_id,
                            entry_type="fee",
                            delta_sats=-int(fee),
                            ref_type="withdrawal",
                            ref_id=w.id,
                            meta="network_fee",
                        ))
                db.session.add(w)
                db.session.commit()
                n += 1
        except Exception:
            db.session.rollback()
            continue
    return n


def start_scheduler(app) -> Optional[BackgroundScheduler]:
    enabled = str(app.config.get("SCHEDULER_ENABLED", "0")).lower() in ("1", "true", "yes")
    if not enabled:
        return None
    interval = int(app.config.get("RECONCILE_INTERVAL_SECONDS", 60))

    scheduler = BackgroundScheduler(daemon=True)

    def _job_invoices():
        with app.app_context():
            try:
                reconcile_invoices_once()
            except Exception:
                pass

    def _job_withdrawals():
        with app.app_context():
            try:
                reconcile_withdrawals_once()
            except Exception:
                pass

    scheduler.add_job(_job_invoices, "interval", seconds=interval, id="reconcile_invoices")
    scheduler.add_job(_job_withdrawals, "interval", seconds=interval, id="reconcile_withdrawals")

    # Optional: ops alerts job
    try:
        alerts_enabled = str(app.config.get("OP_ALERTS_ENABLED", "0")).lower() in ("1", "true", "yes")
        if alerts_enabled:
            import requests  # lightweight dependency already present
            alerts_every = int(app.config.get("OP_ALERTS_INTERVAL_SECONDS", 300))

            def _job_ops_alerts():
                from datetime import datetime as _dt, timedelta as _td
                with app.app_context():
                    try:
                        webhook = app.config.get("OP_ALERTS_WEBHOOK_URL")
                        if not webhook:
                            return
                        # 1) Provider success in last 15m
                        since = _dt.utcnow() - _td(minutes=15)
                        q = ProviderLog.query.filter(ProviderLog.created_at >= since)
                        tot = q.count()
                        succ = q.filter(ProviderLog.success == True).count()  # noqa: E712
                        rate = (succ / tot) if tot else None
                        min_rate = float(app.config.get("OP_ALERTS_MIN_SUCCESS_15M", 0.8))

                        # 2) Ledger vs account invariant - TODO: rewrite using User.sats instead of AccountBalance
                        # total_balance = db.session.query(func.coalesce(func.sum(AccountBalance.balance_sats), 0)).scalar() or 0
                        # ledger_sum = db.session.query(func.coalesce(func.sum(LedgerEntry.delta_sats), 0)).scalar() or 0
                        # delta = int(ledger_sum) - int(total_balance)
                        # tol = int(app.config.get("OP_ALERTS_INVARIANT_TOL_SATS", 0))

                        # 3) Negative balances - TODO: rewrite using User.sats instead of AccountBalance
                        # neg_count = AccountBalance.query.filter(AccountBalance.balance_sats < 0).count()

                        # 4) Uncredited invoices and missing fee withdrawals
                        uncredited_paid = LightningInvoice.query.filter(
                            LightningInvoice.status == "paid",
                            LightningInvoice.credited == False,
                        ).count()  # noqa: E712
                        fee_exists = (
                            db.session.query(LedgerEntry.id)
                            .filter(and_(LedgerEntry.ref_type == "withdrawal", LedgerEntry.entry_type == "fee", LedgerEntry.ref_id == LightningWithdrawal.id))
                            .exists()
                        )
                        miss_fee = (
                            LightningWithdrawal.query
                            .filter(LightningWithdrawal.status == "confirmed")
                            .filter(~fee_exists)
                            .count()
                        )

                        should_alert = False
                        reasons = []
                        if rate is not None and rate < min_rate:
                            should_alert = True
                            reasons.append(f"provider_success_15m={rate:.2%} (<{min_rate:.0%})")
                        # TODO: uncomment when invariant check is reworked for User.sats
                        # if tol >= 0 and abs(delta) > tol:
                        #     should_alert = True
                        #     reasons.append(f"invariant_delta={delta} (> {tol})")
                        # TODO: uncomment when negative balance check is reworked for User.sats
                        # if neg_count > 0:
                        #     should_alert = True
                        #     reasons.append(f"negative_balances={neg_count}")
                        if uncredited_paid > 0:
                            should_alert = True
                            reasons.append(f"uncredited_paid={uncredited_paid}")
                        if miss_fee > 0:
                            should_alert = True
                            reasons.append(f"missing_withdraw_fees={miss_fee}")

                        if not should_alert:
                            return

                        payload = {
                            "source": "postfun-backend",
                            "ts": _dt.utcnow().isoformat() + "Z",
                            "reasons": reasons,
                            "metrics": {
                                "provider_success_15m": (float(rate) if rate is not None else None),
                                "invariant_delta_sats": int(delta),
                                "negative_balances": int(neg_count),
                                "uncredited_paid": int(uncredited_paid),
                                "missing_withdraw_fees": int(miss_fee),
                            },
                        }
                        try:
                            requests.post(webhook, json=payload, timeout=5)
                        except Exception:
                            pass
                    except Exception:
                        # Never crash the scheduler on alert failures
                        pass

            scheduler.add_job(_job_ops_alerts, "interval", seconds=max(60, alerts_every), id="ops_alerts")
    except Exception:
        pass

    # Optional: OHLC aggregation job
    try:
        ohlc_enabled = str(app.config.get("OHLC_AGGREGATION_ENABLED", "1")).lower() in ("1", "true", "yes")
        if ohlc_enabled:
            every = int(app.config.get("OHLC_AGGREGATE_EVERY_SECONDS", 60))
            intervals_csv = app.config.get("OHLC_INTERVALS", "1m,5m,1h")
            default_window = app.config.get("OHLC_WINDOW_DEFAULT", "24h")

            def _job_ohlc():
                from .market_data import persist_candles_all_tokens
                with app.app_context():
                    try:
                        intervals = [s.strip() for s in str(intervals_csv).split(',') if s.strip()]
                        persist_candles_all_tokens(intervals=intervals, window=default_window)
                    except Exception:
                        pass

            scheduler.add_job(_job_ohlc, "interval", seconds=max(30, every), id="ohlc_aggregate")
    except Exception:
        pass

    scheduler.start()
    return scheduler
