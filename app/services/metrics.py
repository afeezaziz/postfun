from __future__ import annotations

import time
from threading import Lock
from typing import Dict, Tuple
from sqlalchemy import text
from ..extensions import db

# Simple in-memory metrics (per-process). For multi-worker, use Prometheus/StatsD.
_lock = Lock()
_request_buckets: Dict[int, Dict[str, int]] = {}  # minute_ts -> {"req": int, "err": int}
_sse_clients: Dict[str, int] = {}


def record_response(status_code: int) -> None:
    now_min = int(time.time() // 60)
    with _lock:
        b = _request_buckets.setdefault(now_min, {"req": 0, "err": 0})
        b["req"] += 1
        if int(status_code) >= 400:
            b["err"] += 1
        # prune old buckets beyond ~20 minutes
        cutoff = now_min - 20
        old_keys = [k for k in _request_buckets.keys() if k < cutoff]
        for k in old_keys:
            _request_buckets.pop(k, None)


def get_request_stats(window_seconds: int = 300) -> Dict[str, float]:
    now_min = int(time.time() // 60)
    minutes = max(1, int(window_seconds // 60))
    total_req = 0
    total_err = 0
    with _lock:
        for m in range(now_min - minutes + 1, now_min + 1):
            b = _request_buckets.get(m)
            if not b:
                continue
            total_req += int(b.get("req", 0))
            total_err += int(b.get("err", 0))
    error_rate = (total_err / total_req) if total_req else 0.0
    rpm = total_req / minutes
    return {
        "requests": float(total_req),
        "errors": float(total_err),
        "error_rate": float(error_rate),
        "rpm": float(rpm),
    }


def inc_sse(endpoint: str) -> None:
    with _lock:
        _sse_clients[endpoint] = int(_sse_clients.get(endpoint, 0)) + 1


def dec_sse(endpoint: str) -> None:
    with _lock:
        _sse_clients[endpoint] = max(0, int(_sse_clients.get(endpoint, 0)) - 1)


def get_sse_counts() -> Dict[str, int]:
    with _lock:
        return dict(_sse_clients)


def db_health() -> Dict[str, float | bool | str]:
    start = time.perf_counter()
    try:
        db.session.execute(text("SELECT 1"))
        _ = db.session.scalar(text("SELECT 1"))
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {"ok": True, "latency_ms": float(latency_ms)}
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {"ok": False, "latency_ms": float(latency_ms), "error": str(e)}
