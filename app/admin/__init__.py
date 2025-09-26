from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Optional
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, abort, flash, g

from ..extensions import db
from ..models import User, Token, AlertRule, AlertEvent, AuditLog
from ..web import get_jwt_from_cookie
from ..services.audit import log_action
from sqlalchemy import select, or_

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
    stmt = stmt.order_by(Token.market_cap.desc().nullslast())
    tokens_p = db.paginate(stmt, page=page, per_page=per)
    return render_template("admin/tokens.html", tokens_p=tokens_p, q=q or "")


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
