from __future__ import annotations

from typing import Optional
from flask import current_app, request, has_request_context
from ..extensions import db
from ..models import AuditLog


def log_action(user_id: Optional[int], action: str, meta: Optional[str] = None) -> None:
    """Record an audit entry.

    If a request context exists, automatically append request metadata
    (client IP, user-agent) and, when available, acting admin identifiers
    from g.admin_user to the meta string. This avoids schema changes while
    still capturing useful context.
    """
    try:
        enriched_meta = meta or ""
        if has_request_context():
            # Prefer X-Forwarded-For if present (first hop)
            xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            ip = xff or (request.remote_addr or "")
            ua = getattr(request.user_agent, "string", "") or ""
            if enriched_meta:
                enriched_meta += " "
            enriched_meta += f"ip={ip} ua={ua}"
            try:
                from flask import g

                admin_user = getattr(g, "admin_user", None)
                if admin_user is not None:
                    enriched_meta += f" admin_id={admin_user.id} admin_npub={admin_user.npub or ''}"
            except Exception:
                # best-effort enrichment
                pass

        entry = AuditLog(user_id=user_id, action=action, meta=enriched_meta or None)
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.debug("[audit] failed to log action %s: %s", action, e)
