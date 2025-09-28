from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Optional
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, abort, flash, g

from ..extensions import db, cache
from ..models import (
    User,
    Token,
    AlertRule,
    AlertEvent,
    AuditLog,
    SwapPool,
    FeeDistributionRule,
    FeePayout,
    TokenInfo,
    BurnEvent,
    ProviderLog,
    LightningInvoice,
    LightningWithdrawal,
    AccountBalance,
    LedgerEntry,
    IdempotencyKey,
    FeatureFlag,
)
from ..web import get_jwt_from_cookie, _fee_summary_for_pool_cached
from ..services.audit import log_action
from sqlalchemy import select, or_, case, func, exists, and_
from ..services.lightning import LNBitsClient
from ..services.reconcile import reconcile_invoices_once, reconcile_withdrawals_once, _get_or_create_balance
from ..services.metrics import get_request_stats, get_sse_counts, db_health

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def require_admin(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        payload = get_jwt_from_cookie()
        if not payload:
            return redirect(url_for("web.home"))
        uid = payload.get("uid")
        user: Optional[User] = db.session.get(User, uid) if isinstance(uid, int) else None
        if not user or not user.is_admin:
            abort(403)
        g.admin_user = user
        return f(*args, **kwargs)

    return wrapper


@admin_bp.route("/")
@require_admin
def dashboard():
    users_count = User.query.count()
    tokens_count = Token.query.count()
    alerts_count = AlertRule.query.count()
    events_count = AlertEvent.query.count()
    audit_count = AuditLog.query.count()
    return render_template(
        "admin/dashboard.html",
        users_count=users_count,
        tokens_count=tokens_count,
        alerts_count=alerts_count,
        events_count=events_count,
        audit_count=audit_count,
    )


@admin_bp.route("/payments")
@require_admin
def payments():
    # Summary stats
    pending_invoices = db.session.query(func.count(LightningInvoice.id)).filter(LightningInvoice.status == "pending").scalar() or 0
    pending_withdrawals = db.session.query(func.count(LightningWithdrawal.id)).filter(LightningWithdrawal.status == "pending").scalar() or 0

    # Provider log success rate last 1h
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(hours=1)
    logs_1h = ProviderLog.query.filter(ProviderLog.created_at >= since)
    success_1h = logs_1h.filter(ProviderLog.success == True).count()  # noqa: E712
    total_1h = logs_1h.count()
    success_rate_1h = (success_1h / total_1h * 100.0) if total_1h else None

    # Ledger vs account invariant
    total_balance = db.session.query(func.coalesce(func.sum(AccountBalance.balance_sats), 0)).scalar() or 0
    ledger_sum = db.session.query(func.coalesce(func.sum(LedgerEntry.delta_sats), 0)).scalar() or 0
    invariant_diff = int(ledger_sum) - int(total_balance)

    # Anti-fraud quick checks
    negative_balances = db.session.query(func.count(AccountBalance.id)).filter(AccountBalance.balance_sats < 0).scalar() or 0
    negative_items = (
        AccountBalance.query
        .filter(AccountBalance.balance_sats < 0)
        .order_by(AccountBalance.balance_sats.asc())
        .limit(50)
        .all()
    )
    uncredited_paid_q = LightningInvoice.query.filter(
        LightningInvoice.status == "paid", LightningInvoice.credited == False  # noqa: E712
    )
    uncredited_paid = uncredited_paid_q.count() or 0
    uncredited_ids = [row.id for row in uncredited_paid_q.with_entities(LightningInvoice.id).limit(100).all()]
    # Confirmed withdrawals missing a fee ledger entry
    fee_exists = (
        db.session.query(LedgerEntry.id)
        .filter(and_(LedgerEntry.ref_type == "withdrawal", LedgerEntry.entry_type == "fee", LedgerEntry.ref_id == LightningWithdrawal.id))
        .exists()
    )
    missing_fee_q = (
        LightningWithdrawal.query
        .filter(LightningWithdrawal.status == "confirmed")
        .filter(~fee_exists)
    )
    confirmed_missing_fee = (missing_fee_q.count() or 0)
    missing_fee_withdrawal_ids = [row.id for row in missing_fee_q.with_entities(LightningWithdrawal.id).limit(100).all()]

    # Duplicate idempotency across users (same scope+key)
    dupes = (
        db.session.query(IdempotencyKey.scope, IdempotencyKey.key, func.count(func.distinct(IdempotencyKey.user_id)).label("users"))
        .group_by(IdempotencyKey.scope, IdempotencyKey.key)
        .having(func.count(func.distinct(IdempotencyKey.user_id)) > 1)
        .order_by(func.count(func.distinct(IdempotencyKey.user_id)).desc())
        .limit(50)
        .all()
    )

    # Filters for provider logs
    action = request.args.get("action", type=str)
    success_q = request.args.get("success", type=str)
    ref_type = request.args.get("ref_type", type=str)
    q = request.args.get("q", type=str)  # ref_id or payment_hash
    start_s = request.args.get("start", type=str)
    end_s = request.args.get("end", type=str)

    def _parse_dt(val):
        if not val:
            return None
        s = val.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    start_dt = _parse_dt(start_s)
    end_dt = _parse_dt(end_s)
    if end_dt and (end_s and len(end_s) == 10):
        end_dt = end_dt + timedelta(days=1)

    stmt = select(ProviderLog).order_by(ProviderLog.created_at.desc())
    if action:
        like = f"%{action}%"
        stmt = stmt.where(ProviderLog.action.ilike(like))
    if ref_type:
        stmt = stmt.where(ProviderLog.ref_type == ref_type)
    if success_q in {"0", "1"}:
        stmt = stmt.where(ProviderLog.success == (success_q == "1"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ProviderLog.ref_id.ilike(like), ProviderLog.response_payload.ilike(like)))
    if start_dt:
        stmt = stmt.where(ProviderLog.created_at >= start_dt)
    if end_dt:
        if end_s and len(end_s) == 10:
            stmt = stmt.where(ProviderLog.created_at < end_dt)
        else:
            stmt = stmt.where(ProviderLog.created_at <= end_dt)

    page = max(1, request.args.get("page", default=1, type=int))
    per = min(200, request.args.get("per", default=50, type=int))
    logs_p = db.paginate(stmt, page=page, per_page=per)

    # Recent invoices/withdrawals
    invoices = LightningInvoice.query.order_by(LightningInvoice.created_at.desc()).limit(25).all()
    withdrawals = LightningWithdrawal.query.order_by(LightningWithdrawal.created_at.desc()).limit(25).all()

    return render_template(
        "admin/payments.html",
        pending_invoices=int(pending_invoices or 0),
        pending_withdrawals=int(pending_withdrawals or 0),
        success_rate_1h=success_rate_1h,
        total_balance=int(total_balance or 0),
        ledger_sum=int(ledger_sum or 0),
        invariant_diff=int(invariant_diff or 0),
        negative_balances=int(negative_balances or 0),
        uncredited_paid=int(uncredited_paid or 0),
        confirmed_missing_fee=int(confirmed_missing_fee or 0),
        uncredited_ids=uncredited_ids,
        missing_fee_withdrawal_ids=missing_fee_withdrawal_ids,
        negative_items=negative_items,
        dupes=dupes,
        logs_p=logs_p,
        action=action or "",
        success_q=success_q or "",
        ref_type=ref_type or "",
        q=q or "",
        start=start_s or "",
        end=end_s or "",
        invoices=invoices,
        withdrawals=withdrawals,
    )


@admin_bp.route("/payments/reconcile", methods=["POST"])
@require_admin
def payments_reconcile():
    op = (request.form.get("op") or "").strip().lower()
    count = 0
    try:
        if op == "invoices":
            count = reconcile_invoices_once()
        elif op == "withdrawals":
            count = reconcile_withdrawals_once()
        else:
            flash("Invalid reconcile op", "error")
            return redirect(url_for("admin.payments"))
        flash(f"Reconciled {count} {op}", "success")
    except Exception as e:
        flash(f"Reconcile failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/balance/adjust", methods=["POST"])
@require_admin
def payments_balance_adjust():
    user_id = request.form.get("user_id", type=int)
    delta_sats = request.form.get("delta_sats", type=int)
    note = request.form.get("note", type=str) or None
    if not user_id or delta_sats is None:
        flash("Missing user_id or delta_sats", "error")
        return redirect(url_for("admin.payments"))
    try:
        bal = _get_or_create_balance(user_id)
        bal.balance_sats = int(bal.balance_sats) + int(delta_sats)
        db.session.add(bal)
        db.session.add(LedgerEntry(
            user_id=user_id,
            entry_type="adjustment",
            delta_sats=int(delta_sats),
            ref_type="admin",
            ref_id=str(g.admin_user.id),
            meta=note,
        ))
        db.session.commit()
        log_action(g.admin_user.id, "balance_adjust", meta=f"user_id={user_id} delta_sats={delta_sats}")
        flash("Balance adjusted", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Adjust failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/invoice/credit", methods=["POST"])
@require_admin
def payments_invoice_credit():
    inv_id = request.form.get("id")
    if not inv_id:
        flash("Missing invoice id", "error")
        return redirect(url_for("admin.payments"))
    try:
        inv = (
            LightningInvoice.query.filter_by(id=inv_id)
            .with_for_update()
            .first()
        )
        if not inv:
            flash("Invoice not found", "error")
            return redirect(url_for("admin.payments"))
        if inv.status != "paid":
            flash("Invoice not paid", "error")
            return redirect(url_for("admin.payments"))
        if inv.credited:
            flash("Invoice already credited", "info")
            return redirect(url_for("admin.payments"))
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
        log_action(g.admin_user.id, "invoice_credit", meta=f"invoice_id={inv.id}")
        flash("Invoice credited", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Credit failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/withdrawal/add_fee", methods=["POST"])
@require_admin
def payments_withdrawal_add_fee():
    wid = request.form.get("id")
    if not wid:
        flash("Missing withdrawal id", "error")
        return redirect(url_for("admin.payments"))
    try:
        w = (
            LightningWithdrawal.query.filter_by(id=wid)
            .with_for_update()
            .first()
        )
        if not w:
            flash("Withdrawal not found", "error")
            return redirect(url_for("admin.payments"))
        if w.status != "confirmed":
            flash("Withdrawal not confirmed", "error")
            return redirect(url_for("admin.payments"))
        # Avoid duplicate fee
        has_fee = db.session.query(func.count(LedgerEntry.id)).filter_by(ref_type="withdrawal", ref_id=w.id, entry_type="fee").scalar()
        if has_fee:
            flash("Fee already recorded", "info")
            return redirect(url_for("admin.payments"))
        client = LNBitsClient()
        if not w.payment_hash:
            flash("Missing payment_hash", "error")
            return redirect(url_for("admin.payments"))
        ok, res = client.get_payment_status(w.payment_hash)
        if not ok or not res.get("paid"):
            flash("Provider not paid yet", "error")
            return redirect(url_for("admin.payments"))
        fee = res.get("fee")
        if not isinstance(fee, int) or fee <= 0:
            flash("No positive fee reported", "error")
            return redirect(url_for("admin.payments"))
        w.fee_sats = int(fee)
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
        log_action(g.admin_user.id, "withdraw_add_fee", meta=f"withdrawal_id={w.id} fee={int(fee)}")
        flash("Fee recorded", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Add fee failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/fix", methods=["POST"])
@require_admin
def payments_fix():
    op = (request.form.get("op") or "").strip().lower()
    fixed = 0
    try:
        if op == "credit_uncredited":
            items = LightningInvoice.query.filter(LightningInvoice.status == "paid", LightningInvoice.credited == False).limit(200).all()  # noqa: E712
            for inv in items:
                if inv.credited:
                    continue
                bal = _get_or_create_balance(inv.user_id)
                bal.balance_sats = int(bal.balance_sats) + int(inv.amount_sats)
                db.session.add(bal)
                db.session.add(LedgerEntry(user_id=inv.user_id, entry_type="deposit", delta_sats=int(inv.amount_sats), ref_type="invoice", ref_id=inv.id))
                inv.credited = True
                db.session.add(inv)
                fixed += 1
            db.session.commit()
            log_action(g.admin_user.id, "payments_fix_credit_uncredited", meta=f"count={fixed}")
            flash(f"Credited {fixed} invoices", "success")
        elif op == "fees_missing":
            client = LNBitsClient()
            fee_exists = (
                db.session.query(LedgerEntry.id)
                .filter(and_(LedgerEntry.ref_type == "withdrawal", LedgerEntry.entry_type == "fee", LedgerEntry.ref_id == LightningWithdrawal.id))
                .exists()
            )
            items = (
                LightningWithdrawal.query
                .filter(LightningWithdrawal.status == "confirmed")
                .filter(~fee_exists)
                .limit(200)
                .all()
            )
            for w in items:
                if not w.payment_hash:
                    continue
                ok, res = client.get_payment_status(w.payment_hash)
                if not ok or not res.get("paid"):
                    continue
                fee = res.get("fee")
                if isinstance(fee, int) and fee > 0:
                    w.fee_sats = int(fee)
                    db.session.add(LedgerEntry(user_id=w.user_id, entry_type="fee", delta_sats=-int(fee), ref_type="withdrawal", ref_id=w.id, meta="network_fee"))
                    db.session.add(w)
                    fixed += 1
            db.session.commit()
            log_action(g.admin_user.id, "payments_fix_fees_missing", meta=f"count={fixed}")
            flash(f"Added fee entries for {fixed} withdrawals", "success")
        else:
            flash("Invalid fix op", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Fix failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/repoll", methods=["POST"])
@require_admin
def payments_repoll():
    typ = (request.form.get("type") or "").strip().lower()  # invoice|withdrawal
    rid = request.form.get("id")
    if not typ or not rid:
        flash("Missing type or id", "error")
        return redirect(url_for("admin.payments"))
    try:
        client = LNBitsClient()
        if typ == "invoice":
            inv = db.session.get(LightningInvoice, rid)
            if not inv:
                flash("Invoice not found", "error")
                return redirect(url_for("admin.payments"))
            if not inv.payment_hash:
                flash("Invoice missing payment_hash", "error")
                return redirect(url_for("admin.payments"))
            ok, res = client.get_payment_status(inv.payment_hash)
            if ok and res.get("paid"):
                from datetime import datetime as _dt
                inv.status = "paid"
                inv.paid_at = inv.paid_at or _dt.utcnow()
                if not getattr(inv, "credited", False):
                    bal = AccountBalance.query.filter_by(user_id=inv.user_id, asset="BTC").with_for_update().first()
                    if not bal:
                        bal = AccountBalance(user_id=inv.user_id, asset="BTC", balance_sats=0)
                        db.session.add(bal)
                        db.session.flush()
                    bal.balance_sats = int(bal.balance_sats) + int(inv.amount_sats)
                    db.session.add(bal)
                    db.session.add(LedgerEntry(user_id=inv.user_id, entry_type="deposit", delta_sats=int(inv.amount_sats), ref_type="invoice", ref_id=inv.id))
                    inv.credited = True
                db.session.add(inv)
                db.session.commit()
                flash("Invoice credited", "success")
            else:
                flash("Not paid yet", "info")
        elif typ == "withdrawal":
            w = db.session.get(LightningWithdrawal, rid)
            if not w:
                flash("Withdrawal not found", "error")
                return redirect(url_for("admin.payments"))
            if not w.payment_hash:
                flash("Withdrawal missing payment_hash", "error")
                return redirect(url_for("admin.payments"))
            ok, res = client.get_payment_status(w.payment_hash)
            if ok and res.get("paid"):
                from datetime import datetime as _dt
                w.status = "confirmed"
                w.processed_at = w.processed_at or _dt.utcnow()
                fee = res.get("fee")
                if isinstance(fee, int) and fee > 0:
                    w.fee_sats = int(fee)
                    has_fee = db.session.query(func.count(LedgerEntry.id)).filter_by(ref_type="withdrawal", ref_id=w.id, entry_type="fee").scalar()
                    if not has_fee:
                        db.session.add(LedgerEntry(user_id=w.user_id, entry_type="fee", delta_sats=-int(fee), ref_type="withdrawal", ref_id=w.id, meta="network_fee"))
                db.session.add(w)
                db.session.commit()
                flash("Withdrawal confirmed", "success")
            else:
                flash("Not confirmed yet", "info")
        else:
            flash("Invalid type", "error")
    except Exception as e:
        db.session.rollback()
        flash(f"Repoll failed: {e}", "error")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/logs/export.csv")
@require_admin
def payments_logs_export():
    from flask import Response
    action = request.args.get("action", type=str)
    success_q = request.args.get("success", type=str)
    ref_type = request.args.get("ref_type", type=str)
    q = request.args.get("q", type=str)
    start_s = request.args.get("start", type=str)
    end_s = request.args.get("end", type=str)

    from datetime import datetime as _dt
    def _parse_dt(val):
        if not val:
            return None
        s = val.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.strptime(s, fmt)
            except Exception:
                pass
        try:
            return _dt.fromisoformat(s)
        except Exception:
            return None

    start_dt = _parse_dt(start_s)
    end_dt = _parse_dt(end_s)
    if end_dt and (end_s and len(end_s) == 10):
        from datetime import timedelta as _td
        end_dt = end_dt + _td(days=1)

    stmt = select(ProviderLog).order_by(ProviderLog.created_at.desc())
    if action:
        stmt = stmt.where(ProviderLog.action.ilike(f"%{action}%"))
    if ref_type:
        stmt = stmt.where(ProviderLog.ref_type == ref_type)
    if success_q in {"0", "1"}:
        stmt = stmt.where(ProviderLog.success == (success_q == "1"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ProviderLog.ref_id.ilike(like), ProviderLog.response_payload.ilike(like)))
    if start_dt:
        stmt = stmt.where(ProviderLog.created_at >= start_dt)
    if end_dt:
        if end_s and len(end_s) == 10:
            stmt = stmt.where(ProviderLog.created_at < end_dt)
        else:
            stmt = stmt.where(ProviderLog.created_at <= end_dt)

    rows = ["id,action,success,ref_type,ref_id,status,created_at"]
    for log in db.session.execute(stmt).scalars():
        rows.append(f"{log.id},{log.action},{1 if log.success else 0},{log.ref_type or ''},{log.ref_id or ''},{log.response_status or ''},{(log.created_at.isoformat() + 'Z')} ")
    csv_data = "\n".join(rows) + "\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=provider_logs.csv"})


@admin_bp.route("/fees")
@require_admin
def fees():
    pools = (
        SwapPool.query
        .order_by(SwapPool.id.asc())
        .all()
    )
    # Attach token symbols for display
    items = []
    for p in pools:
        ta = Token.query.get(p.token_a_id)
        tb = Token.query.get(p.token_b_id)
        items.append({
            "id": p.id,
            "token_a": ta.symbol if ta else p.token_a_id,
            "token_b": tb.symbol if tb else p.token_b_id,
        })
    return render_template("admin/fees.html", pools=items)


@admin_bp.route("/fees/<int:pool_id>", methods=["GET", "POST"])
@require_admin
def fees_detail(pool_id: int):
    from decimal import Decimal

    pool = db.session.get(SwapPool, pool_id)
    if not pool:
        abort(404)
    # Get tokens
    ta = db.session.get(Token, pool.token_a_id)
    tb = db.session.get(Token, pool.token_b_id)
    # Current rule
    rule = FeeDistributionRule.query.filter_by(pool_id=pool.id).first()

    if request.method == "POST":
        op = (request.form.get("op") or "").strip().lower()
        if op == "save_rule":
            try:
                bps_creator = int(request.form.get("bps_creator", 5000))
                bps_minter = int(request.form.get("bps_minter", 3000))
                bps_treasury = int(request.form.get("bps_treasury", 2000))
                if bps_creator < 0 or bps_minter < 0 or bps_treasury < 0 or (bps_creator + bps_minter + bps_treasury) != 10000:
                    raise ValueError("bps must sum to 10000")
                creator_user_id = request.form.get("creator_user_id")
                minter_user_id = request.form.get("minter_user_id")
                treasury_account = request.form.get("treasury_account")
                if not rule:
                    rule = FeeDistributionRule(pool_id=pool.id)
                    db.session.add(rule)
                rule.creator_user_id = int(creator_user_id) if creator_user_id else None
                rule.minter_user_id = int(minter_user_id) if minter_user_id else None
                rule.treasury_account = treasury_account or None
                rule.bps_creator = bps_creator
                rule.bps_minter = bps_minter
                rule.bps_treasury = bps_treasury
                db.session.add(rule)
                db.session.commit()
                try:
                    cache.delete_memoized(_fee_summary_for_pool_cached, pool.id)
                except Exception:
                    pass
                flash("Fee rule saved", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Failed to save rule: {e}", "error")
            return redirect(url_for("admin.fees_detail", pool_id=pool.id))

        if op == "payout":
            try:
                entity = request.form.get("entity")
                asset = request.form.get("asset")
                amount_s = request.form.get("amount")
                if entity not in {"creator", "minter", "treasury"} or asset not in {"A", "B"}:
                    raise ValueError("invalid payout params")
                amount = Decimal(str(amount_s))
                if amount <= 0:
                    raise ValueError("amount must be positive")
                # compute pending
                bps_c = int(rule.bps_creator if rule else 5000)
                bps_m = int(rule.bps_minter if rule else 3000)
                bps_t = int(rule.bps_treasury if rule else 2000)
                fa = Decimal(pool.fee_accum_a or 0)
                fb = Decimal(pool.fee_accum_b or 0)
                def allocs(bps: int):
                    return {"A": (fa * Decimal(bps) / Decimal(10000)), "B": (fb * Decimal(bps) / Decimal(10000))}
                def paid(ent: str):
                    rows = FeePayout.query.filter_by(pool_id=pool.id, entity=ent).all()
                    totA = Decimal("0"); totB = Decimal("0")
                    for p in rows:
                        if p.asset == "A": totA += Decimal(p.amount or 0)
                        elif p.asset == "B": totB += Decimal(p.amount or 0)
                    return {"A": totA, "B": totB}
                bps_map = {"creator": bps_c, "minter": bps_m, "treasury": bps_t}
                a = allocs(bps_map[entity])
                p = paid(entity)
                pending = a[asset] - p[asset]
                if amount > pending:
                    raise ValueError(f"amount exceeds pending ({float(pending):.8f})")
                payout = FeePayout(pool_id=pool.id, entity=entity, asset=asset, amount=amount, note=(request.form.get("note") or None))
                db.session.add(payout)
                db.session.commit()
                try:
                    cache.delete_memoized(_fee_summary_for_pool_cached, pool.id)
                except Exception:
                    pass
                log_action(g.admin_user.id, "fees_payout", meta=f"pool_id={pool.id} entity={entity} asset={asset} amount={amount}")
                flash("Payout recorded", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Failed to payout: {e}", "error")
            return redirect(url_for("admin.fees_detail", pool_id=pool.id))

        if op == "force_payout":
            try:
                entity = request.form.get("entity")
                asset = request.form.get("asset")
                amount_s = request.form.get("amount")
                if entity not in {"creator", "minter", "treasury"} or asset not in {"A", "B"}:
                    raise ValueError("invalid payout params")
                amount = Decimal(str(amount_s))
                if amount <= 0:
                    raise ValueError("amount must be positive")
                payout = FeePayout(pool_id=pool.id, entity=entity, asset=asset, amount=amount, note=(request.form.get("note") or None))
                db.session.add(payout)
                db.session.commit()
                try:
                    cache.delete_memoized(_fee_summary_for_pool_cached, pool.id)
                except Exception:
                    pass
                log_action(g.admin_user.id, "fees_force_payout", meta=f"pool_id={pool.id} entity={entity} asset={asset} amount={amount}")
                flash("Force payout recorded", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Failed to force payout: {e}", "error")
            return redirect(url_for("admin.fees_detail", pool_id=pool.id))

    # Compute summary (alloc, paid, pending) for display
    def _summary_for(pool: SwapPool, rule: FeeDistributionRule | None):
        bps_c = int(rule.bps_creator if rule else 5000)
        bps_m = int(rule.bps_minter if rule else 3000)
        bps_t = int(rule.bps_treasury if rule else 2000)
        fa = Decimal(pool.fee_accum_a or 0)
        fb = Decimal(pool.fee_accum_b or 0)
        def allocs(bps: int):
            return {"A": (fa * Decimal(bps) / Decimal(10000)), "B": (fb * Decimal(bps) / Decimal(10000))}
        def paid(entity: str):
            rows = FeePayout.query.filter_by(pool_id=pool.id, entity=entity).all()
            totA = Decimal("0"); totB = Decimal("0")
            for p in rows:
                if p.asset == "A": totA += Decimal(p.amount or 0)
                elif p.asset == "B": totB += Decimal(p.amount or 0)
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

    summary = _summary_for(pool, rule)

    # Render detail
    return render_template("admin/fee_detail.html", pool=pool, token_a=ta, token_b=tb, rule=rule, summary=summary)


@admin_bp.route("/users")
@require_admin
def users():
    q = request.args.get("q", type=str)
    page = max(1, request.args.get("page", default=1, type=int))
    per = min(200, request.args.get("per", default=50, type=int))
    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.npub.ilike(like), User.pubkey_hex.ilike(like), User.display_name.ilike(like)))
    stmt = stmt.order_by(User.created_at.desc())
    users_p = db.paginate(stmt, page=page, per_page=per)
    return render_template("admin/users.html", users_p=users_p, q=q or "")


@admin_bp.route("/users/toggle_admin/<int:user_id>", methods=["POST"])
@require_admin
def users_toggle_admin(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    user.is_admin = not bool(user.is_admin)
    try:
        db.session.commit()
        log_action(g.admin_user.id, "toggle_admin", meta=f"user_id={user_id} is_admin={user.is_admin}")
        flash("Updated user admin flag", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update user", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/toggle_withdraw/<int:user_id>", methods=["POST"])
@require_admin
def users_toggle_withdraw(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    user.withdraw_frozen = not bool(getattr(user, "withdraw_frozen", False))
    try:
        db.session.commit()
        log_action(g.admin_user.id, "toggle_withdraw_freeze", meta=f"user_id={user_id} withdraw_frozen={user.withdraw_frozen}")
        flash("Updated user withdraw freeze", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update user", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/tokens")
@require_admin
def tokens():
    q = request.args.get("q", type=str)
    page = max(1, request.args.get("page", default=1, type=int))
    per = min(500, request.args.get("per", default=50, type=int))
    stmt = select(Token)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Token.symbol.ilike(like), Token.name.ilike(like)))
    stmt = stmt.order_by(
        case((Token.market_cap == None, 1), else_=0),  # noqa: E711
        Token.market_cap.desc(),
    )
    tokens_p = db.paginate(stmt, page=page, per_page=per)
    # Prefetch TokenInfo for visible page to show categories inline
    try:
        ids = [t.id for t in tokens_p.items]
        infos = TokenInfo.query.filter(TokenInfo.token_id.in_(ids)).all() if ids else []
        infos_by_token_id = {inf.token_id: inf for inf in infos}
    except Exception:
        infos_by_token_id = {}
    return render_template("admin/tokens.html", tokens_p=tokens_p, q=q or "", infos_by_token_id=infos_by_token_id)


@admin_bp.route("/tokens/save", methods=["POST"])
@require_admin
def tokens_save():
    symbol = (request.form.get("symbol") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    price_s = (request.form.get("price") or "").strip()
    mcap_s = (request.form.get("market_cap") or "").strip()
    change_s = (request.form.get("change_24h") or "").strip()
    if not symbol or not name:
        flash("Symbol and name are required", "error")
        return redirect(url_for("admin.tokens"))

    tok = Token.query.filter_by(symbol=symbol).first()
    if tok is None:
        tok = Token(symbol=symbol, name=name)
        db.session.add(tok)
    tok.name = name

    def parse_dec(s: str, default: Optional[Decimal] = None) -> Optional[Decimal]:
        try:
            return Decimal(s) if s != "" else default
        except (InvalidOperation, ValueError):
            return default

    p = parse_dec(price_s)
    mc = parse_dec(mcap_s)
    ch = parse_dec(change_s)
    if p is not None:
        tok.price = p
    if mc is not None:
        tok.market_cap = mc
    if ch is not None:
        tok.change_24h = ch
    try:
        db.session.commit()
        log_action(g.admin_user.id, "token_save", meta=f"symbol={symbol}")
        flash("Token saved", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to save token", "error")
    return redirect(url_for("admin.tokens"))


@admin_bp.route("/tokens/<int:token_id>/categories", methods=["POST"])
@require_admin
def tokens_set_categories(token_id: int):
    tok = db.session.get(Token, token_id)
    if not tok:
        flash("Token not found", "error")
        return redirect(url_for("admin.tokens"))
    raw = (request.form.get("categories") or "").strip()
    # Normalize: split by comma, trim, dedupe, lower-case for storage consistency
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # keep original case for display but collapse duplicates case-insensitively
    seen = set()
    norm = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        norm.append(p)
    cats = ",".join(norm)[:255] if norm else None
    try:
        info = TokenInfo.query.filter_by(token_id=token_id).first()
        if not info:
            info = TokenInfo(token_id=token_id)
            db.session.add(info)
        info.categories = cats
        db.session.add(info)
        db.session.commit()
        log_action(g.admin_user.id, "token_set_categories", meta=f"token_id={token_id} categories={cats}")
        flash("Categories updated", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to update categories: {e}", "error")
    return redirect(url_for("admin.tokens"))


@admin_bp.route("/tokens/export.csv")
@require_admin
def tokens_export():
    from flask import Response

    items = Token.query.order_by(Token.symbol.asc()).all()
    rows = ["symbol,name,price,market_cap,change_24h"]
    for t in items:
        rows.append(
            f"{t.symbol},{t.name},{float(t.price or 0):.8f},{float(t.market_cap or 0):.2f},{float(t.change_24h or 0):.4f}"
        )
    csv_data = "\n".join(rows) + "\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=admin_tokens.csv"})


@admin_bp.route("/tokens/import", methods=["POST"])
@require_admin
def tokens_import():
    csv_text = request.form.get("csv", "")
    if not csv_text:
        flash("No CSV provided", "error")
        return redirect(url_for("admin.tokens"))
    f = StringIO(csv_text)
    header = f.readline()
    count = 0
    for line in f:
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) < 2:
            continue
        symbol, name = parts[0], parts[1]
        price_s = parts[2] if len(parts) > 2 else ""
        mcap_s = parts[3] if len(parts) > 3 else ""
        change_s = parts[4] if len(parts) > 4 else ""
        tok = Token.query.filter_by(symbol=symbol).first()
        if tok is None:
            tok = Token(symbol=symbol, name=name)
            db.session.add(tok)
        tok.name = name
        def parse_dec(s: str, default: Optional[Decimal] = None) -> Optional[Decimal]:
            try:
                return Decimal(s) if s != "" else default
            except (InvalidOperation, ValueError):
                return default
        p = parse_dec(price_s)
        mc = parse_dec(mcap_s)
        ch = parse_dec(change_s)
        if p is not None:
            tok.price = p
        if mc is not None:
            tok.market_cap = mc
        if ch is not None:
            tok.change_24h = ch
        count += 1
    try:
        db.session.commit()
        log_action(g.admin_user.id, "tokens_import", meta=f"count={count}")
        flash(f"Imported/updated {count} tokens", "success")
    except Exception:
        db.session.rollback()
        flash("Import failed", "error")
    return redirect(url_for("admin.tokens"))


@admin_bp.route("/alerts")
@require_admin
def alerts_admin():
    page = max(1, request.args.get("page", default=1, type=int))
    per = min(200, request.args.get("per", default=50, type=int))
    e_page = max(1, request.args.get("e_page", default=1, type=int))
    e_per = min(200, request.args.get("e_per", default=50, type=int))
    rules_p = db.paginate(select(AlertRule).order_by(AlertRule.created_at.desc()), page=page, per_page=per)
    events_p = db.paginate(select(AlertEvent).order_by(AlertEvent.triggered_at.desc()), page=e_page, per_page=e_per)
    return render_template("admin/alerts.html", rules_p=rules_p, events_p=events_p)


@admin_bp.route("/audit")
@require_admin
def audit():
    user_id = request.args.get("user_id", type=int)
    action = request.args.get("action", type=str)
    start_s = request.args.get("start", type=str)
    end_s = request.args.get("end", type=str)
    page = max(1, request.args.get("page", default=1, type=int))
    per = min(500, request.args.get("per", default=50, type=int))

    def _parse_dt(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        s = val.strip()
        # support date-only (YYYY-MM-DD) and datetime-local (YYYY-MM-DDTHH:MM)
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt
            except ValueError:
                continue
        # try ISO 8601 fallback
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    start_dt = _parse_dt(start_s)
    end_dt = _parse_dt(end_s)
    # If end is date-only, include the full day by adding 1 day and using < next_day
    if end_dt and (end_s and len(end_s) == 10):  # YYYY-MM-DD
        end_dt = end_dt + timedelta(days=1)

    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action:
        like = f"%{action}%"
        stmt = stmt.where(AuditLog.action.ilike(like))
    if start_dt:
        stmt = stmt.where(AuditLog.created_at >= start_dt)
    if end_dt:
        # use strict less-than if we normalized to next day
        if end_s and len(end_s) == 10:
            stmt = stmt.where(AuditLog.created_at < end_dt)
        else:
            stmt = stmt.where(AuditLog.created_at <= end_dt)

    logs_p = db.paginate(stmt, page=page, per_page=per)
    return render_template(
        "admin/audit.html",
        logs_p=logs_p,
        user_id=user_id or "",
        action=action or "",
        start=start_s or "",
        end=end_s or "",
    )


@admin_bp.route("/audit/export.csv")
@require_admin
def audit_export():
    from flask import Response

    user_id = request.args.get("user_id", type=int)
    action = request.args.get("action", type=str)
    start_s = request.args.get("start", type=str)
    end_s = request.args.get("end", type=str)

    def _parse_dt(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        s = val.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    start_dt = _parse_dt(start_s)
    end_dt = _parse_dt(end_s)
    if end_dt and (end_s and len(end_s) == 10):
        end_dt = end_dt + timedelta(days=1)

    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action:
        like = f"%{action}%"
        stmt = stmt.where(AuditLog.action.ilike(like))
    if start_dt:
        stmt = stmt.where(AuditLog.created_at >= start_dt)
    if end_dt:
        if end_s and len(end_s) == 10:
            stmt = stmt.where(AuditLog.created_at < end_dt)
        else:
            stmt = stmt.where(AuditLog.created_at <= end_dt)

    rows = ["id,user_id,action,meta,created_at"]
    for log in db.session.execute(stmt).scalars():
        safe_meta = (log.meta or "").replace("\n", " ").replace("\r", " ")
        ts = log.created_at.isoformat() + "Z"
        rows.append(f"{log.id},{log.user_id or ''},{log.action},{safe_meta},{ts}")
    csv_data = "\n".join(rows) + "\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=audit_logs.csv"})


@admin_bp.route("/alerts/toggle/<int:rule_id>", methods=["POST"])
@require_admin
def alerts_toggle(rule_id: int):
    rule = db.session.get(AlertRule, rule_id)
    if not rule:
        abort(404)
    rule.active = not bool(rule.active)
    try:
        db.session.commit()
        log_action(g.admin_user.id, "alerts_toggle", meta=f"rule_id={rule_id} active={rule.active}")
        flash("Updated rule", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update rule", "error")
    return redirect(url_for("admin.alerts_admin"))


# ---- Admin: Token flags: hidden/frozen and moderation ----
@admin_bp.route("/tokens/toggle_hidden/<int:token_id>", methods=["POST"])
@require_admin
def tokens_toggle_hidden(token_id: int):
    tok = db.session.get(Token, token_id)
    if not tok:
        abort(404)
    tok.hidden = not bool(getattr(tok, "hidden", False))
    try:
        db.session.commit()
        log_action(g.admin_user.id, "token_toggle_hidden", meta=f"token_id={token_id} hidden={tok.hidden}")
        flash("Updated token hidden flag", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update token", "error")
    return redirect(url_for("admin.tokens"))


@admin_bp.route("/tokens/toggle_frozen/<int:token_id>", methods=["POST"])
@require_admin
def tokens_toggle_frozen(token_id: int):
    tok = db.session.get(Token, token_id)
    if not tok:
        abort(404)
    tok.frozen = not bool(getattr(tok, "frozen", False))
    try:
        db.session.commit()
        log_action(g.admin_user.id, "token_toggle_frozen", meta=f"token_id={token_id} frozen={tok.frozen}")
        flash("Updated token frozen flag", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update token", "error")
    return redirect(url_for("admin.tokens"))


@admin_bp.route("/tokens/moderate/<int:token_id>", methods=["POST"])
@require_admin
def tokens_moderate(token_id: int):
    tok = db.session.get(Token, token_id)
    if not tok:
        abort(404)
    info = TokenInfo.query.filter_by(token_id=token_id).first()
    if not info:
        info = TokenInfo(token_id=token_id)
        db.session.add(info)
    status = (request.form.get("status") or "").strip().lower()
    notes = request.form.get("notes") or None
    if status not in {"visible", "hidden", "flagged"}:
        flash("Invalid moderation status", "error")
        return redirect(url_for("admin.tokens"))
    info.moderation_status = status
    info.moderation_notes = notes
    try:
        db.session.add(info)
        db.session.commit()
        log_action(g.admin_user.id, "token_moderate", meta=f"token_id={token_id} status={status}")
        flash("Moderation updated", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to update moderation", "error")
    return redirect(url_for("admin.tokens"))


# ---- Admin: Feature flags ----
@admin_bp.route("/flags")
@require_admin
def flags_list():
    flags = FeatureFlag.query.order_by(FeatureFlag.key.asc()).all()
    return render_template("admin/flags.html", flags=flags)


@admin_bp.route("/flags/save", methods=["POST"])
@require_admin
def flags_save():
    fid = request.form.get("id")
    key = (request.form.get("key") or "").strip()
    val = (request.form.get("value") or None)
    en = request.form.get("enabled")
    enabled = True if (en in ("1", "true", "on", "yes")) else False
    if not key:
        flash("Key is required", "error")
        return redirect(url_for("admin.flags_list"))
    try:
        if fid and str(fid).isdigit():
            flag = db.session.get(FeatureFlag, int(fid))
            if not flag:
                flash("Flag not found", "error")
                return redirect(url_for("admin.flags_list"))
        else:
            flag = FeatureFlag(key=key)
            db.session.add(flag)
        flag.key = key
        flag.value = val
        flag.enabled = enabled
        db.session.add(flag)
        db.session.commit()
        log_action(g.admin_user.id, "flag_save", meta=f"key={key} enabled={enabled}")
        flash("Flag saved", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to save flag: {e}", "error")
    return redirect(url_for("admin.flags_list"))


@admin_bp.route("/flags/toggle/<int:flag_id>", methods=["POST"])
@require_admin
def flags_toggle(flag_id: int):
    flag = db.session.get(FeatureFlag, flag_id)
    if not flag:
        abort(404)
    flag.enabled = not bool(flag.enabled)
    try:
        db.session.commit()
        log_action(g.admin_user.id, "flag_toggle", meta=f"id={flag_id} enabled={flag.enabled}")
        flash("Flag toggled", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to toggle flag", "error")
    return redirect(url_for("admin.flags_list"))


@admin_bp.route("/flags/delete/<int:flag_id>", methods=["POST"])
@require_admin
def flags_delete(flag_id: int):
    flag = db.session.get(FeatureFlag, flag_id)
    if not flag:
        abort(404)
    try:
        db.session.delete(flag)
        db.session.commit()
        log_action(g.admin_user.id, "flag_delete", meta=f"id={flag_id}")
        flash("Flag deleted", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to delete flag", "error")
    return redirect(url_for("admin.flags_list"))


# ---- Admin: Metrics ----
@admin_bp.route("/metrics")
@require_admin
def metrics_view():
    req_5m = get_request_stats(window_seconds=300)
    req_15m = get_request_stats(window_seconds=900)
    sse = get_sse_counts()
    dbh = db_health()
    # Provider success last 1h
    since = datetime.utcnow() - timedelta(hours=1)
    logs = ProviderLog.query.filter(ProviderLog.created_at >= since)
    tot = logs.count()
    succ = logs.filter(ProviderLog.success == True).count()  # noqa: E712
    success_rate = (succ / tot) if tot else None
    return render_template(
        "admin/metrics.html",
        req_5m=req_5m,
        req_15m=req_15m,
        sse=sse,
        dbh=dbh,
        provider_success=success_rate,
        log_total=tot,
    )


@admin_bp.route("/sse")
@require_admin
def sse_status():
    now = datetime.utcnow()
    since_15 = now - timedelta(minutes=15)
    since_60 = now - timedelta(hours=1)

    # Launch summaries
    recent_launches = (
        TokenInfo.query
        .filter(TokenInfo.launch_at != None)  # noqa: E711
        .order_by(TokenInfo.launch_at.desc())
        .limit(50)
        .all()
    )
    launches = []
    for info in recent_launches:
        tok = db.session.get(Token, info.token_id)
        creator = db.session.get(User, info.launch_user_id) if info.launch_user_id else None
        launches.append({
            "symbol": tok.symbol if tok else None,
            "name": tok.name if tok else None,
            "time": (info.launch_at.isoformat() + "Z") if info.launch_at else None,
            "creator": (creator.display_name or creator.npub or creator.pubkey_hex) if creator else None,
        })
    launch_15 = db.session.query(db.func.count(TokenInfo.id)).filter(TokenInfo.launch_at != None, TokenInfo.launch_at >= since_15).scalar()  # noqa: E711
    launch_60 = db.session.query(db.func.count(TokenInfo.id)).filter(TokenInfo.launch_at != None, TokenInfo.launch_at >= since_60).scalar()  # noqa: E711

    # Burn/stage summaries
    gusd = Token.query.filter_by(symbol="GUSD").first() or Token.query.filter_by(symbol="gUSD").first()
    burns_q = (
        db.session.query(BurnEvent, SwapPool)
        .join(SwapPool, BurnEvent.pool_id == SwapPool.id)
        .order_by(BurnEvent.created_at.desc())
        .limit(50)
        .all()
    )
    burns = []
    for ev, pool in burns_q:
        tokA = db.session.get(Token, pool.token_a_id)
        tokB = db.session.get(Token, pool.token_b_id)
        disp = tokA
        if gusd and tokA and tokA.id == gusd.id:
            disp = tokB
        elif gusd and tokB and tokB.id == gusd.id:
            disp = tokA
        burns.append({
            "symbol": disp.symbol if disp else (tokA.symbol if tokA else None),
            "stage": int(ev.stage),
            "amount": float(ev.amount or 0),
            "time": ev.created_at.isoformat() + "Z",
            "pool_id": pool.id,
        })
    burns_15 = db.session.query(db.func.count(BurnEvent.id)).filter(BurnEvent.created_at >= since_15).scalar()
    burns_60 = db.session.query(db.func.count(BurnEvent.id)).filter(BurnEvent.created_at >= since_60).scalar()

    return render_template(
        "admin/sse.html",
        launch_15=int(launch_15 or 0),
        launch_60=int(launch_60 or 0),
        burns_15=int(burns_15 or 0),
        burns_60=int(burns_60 or 0),
        launches=launches,
        burns=burns,
    )


@admin_bp.route("/alerts/bulk", methods=["POST"])
@require_admin
def alerts_bulk():
    from werkzeug.datastructures import MultiDict

    form: MultiDict = request.form
    op = (form.get("op") or "").strip().lower()  # enable|disable|delete
    ids = form.getlist("rule_ids")
    rule_ids = [int(x) for x in ids if str(x).isdigit()]
    if not rule_ids or op not in {"enable", "disable", "delete"}:
        flash("No rules selected or invalid operation", "error")
        return redirect(url_for("admin.alerts_admin"))

    q = AlertRule.query.filter(AlertRule.id.in_(rule_ids))
    count = 0
    try:
        if op == "delete":
            # Use ORM deletes to honor relationship cascades and avoid FK issues
            rules = q.all()
            for r in rules:
                db.session.delete(r)
            count = len(rules)
        else:
            active_val = (op == "enable")
            count = q.update({AlertRule.active: active_val}, synchronize_session=False)
        db.session.commit()
        log_action(g.admin_user.id, "alerts_bulk", meta=f"op={op} count={count} ids={rule_ids}")
        flash(f"Bulk {op} applied to {count} rules", "success")
    except Exception:
        db.session.rollback()
        flash("Bulk operation failed", "error")
    return redirect(url_for("admin.alerts_admin"))
