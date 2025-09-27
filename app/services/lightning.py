import json
import time
import requests
from typing import Optional, Tuple, Dict, Any
from flask import current_app
from ..extensions import db
from ..models import ProviderLog


class LNBitsClient:
    def __init__(self,
                 api_url: Optional[str] = None,
                 invoice_key: Optional[str] = None,
                 admin_key: Optional[str] = None):
        cfg = current_app.config
        self.base = (api_url or cfg.get("LNBITS_API_URL", "")).rstrip("/")
        self.invoice_key = invoice_key or cfg.get("LNBITS_INVOICE_KEY", "")
        self.admin_key = admin_key or cfg.get("LNBITS_ADMIN_KEY", "")
        # Optional failover
        self.alt_base = (cfg.get("LNBITS_ALT_API_URL", "") or "").rstrip("/")
        self.alt_invoice_key = cfg.get("LNBITS_ALT_INVOICE_KEY", "")
        self.alt_admin_key = cfg.get("LNBITS_ALT_ADMIN_KEY", "")
        # Retries
        self.retry_attempts = int(cfg.get("LNBITS_RETRY_ATTEMPTS", 2))
        self.retry_backoff_ms = int(cfg.get("LNBITS_RETRY_BACKOFF_MS", 300))
        if not self.base:
            raise RuntimeError("LNBITS_API_URL is not configured")

    def _hdr(self, key: str) -> Dict[str, str]:
        return {"X-Api-Key": key, "Content-Type": "application/json"}

    def _request_with_retry(self, method: str, url: str, headers: Dict[str, str], json_body: Dict[str, Any], timeout: int) -> Tuple[bool, Dict[str, Any], int, str]:
        attempts = max(1, int(self.retry_attempts))
        backoff = max(0, int(self.retry_backoff_ms)) / 1000.0
        last_status = 0
        last_text = ""
        for i in range(attempts):
            try:
                r = requests.request(method=method.upper(), url=url, headers=headers, json=json_body, timeout=timeout)
                last_status = r.status_code
                last_text = r.text
                if r.status_code < 500:
                    # success or client error; do not retry further
                    try:
                        return True, r.json(), r.status_code, r.text
                    except Exception:
                        return False, {"status": r.status_code, "error": "invalid_json"}, r.status_code, r.text
            except Exception as e:
                last_text = str(e)
            # retry on server error/exception
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
        return False, {"status": last_status or 0, "error": last_text or "request_failed"}, last_status or 0, last_text

    def _maybe_log(self, action: str, req: Dict[str, Any], status: int, resp_text: str, success: bool, ref_type: Optional[str] = None, ref_id: Optional[str] = None):
        try:
            pl = ProviderLog(
                provider="lnbits",
                action=action,
                request_payload=json.dumps(req) if req is not None else None,
                response_status=int(status) if status is not None else None,
                response_payload=resp_text[:2000] if isinstance(resp_text, str) else str(resp_text)[:2000],
                success=bool(success),
                ref_type=ref_type,
                ref_id=ref_id,
            )
            db.session.add(pl)
            db.session.commit()
        except Exception:
            db.session.rollback()

    def create_invoice(self, amount_sats: int, memo: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        # POST /api/v1/payments with { out: false, amount, memo }
        memo = memo or current_app.config.get("LNBITS_DEFAULT_MEMO", "Deposit")
        data = {"out": False, "amount": int(amount_sats), "memo": memo}
        # Try primary
        url = f"{self.base}/api/v1/payments"
        ok, j, status, text = self._request_with_retry("POST", url, self._hdr(self.invoice_key), data, timeout=20)
        self._maybe_log("create_invoice", data, status, text, ok)
        if not ok and self.alt_base:
            url2 = f"{self.alt_base}/api/v1/payments"
            ok, j, status, text = self._request_with_retry("POST", url2, self._hdr(self.alt_invoice_key or self.invoice_key), data, timeout=20)
            self._maybe_log("create_invoice", data, status, text, ok)
        return ok, j

    def get_payment_status(self, payment_hash: str) -> Tuple[bool, Dict[str, Any]]:
        # GET /api/v1/payments/{payment_hash}
        url = f"{self.base}/api/v1/payments/{payment_hash}"
        ok, j, status, text = self._request_with_retry("GET", url, self._hdr(self.invoice_key or self.admin_key), None, timeout=20)
        self._maybe_log("get_status", {"payment_hash": payment_hash}, status, text, ok, ref_type=None, ref_id=payment_hash)
        if not ok and self.alt_base:
            url2 = f"{self.alt_base}/api/v1/payments/{payment_hash}"
            ok, j, status, text = self._request_with_retry("GET", url2, self._hdr(self.alt_invoice_key or self.alt_admin_key or self.invoice_key or self.admin_key), None, timeout=20)
            self._maybe_log("get_status", {"payment_hash": payment_hash}, status, text, ok, ref_type=None, ref_id=payment_hash)
        return ok, j

    def pay_invoice(self, bolt11: str, max_fee_sats: Optional[int] = None) -> Tuple[bool, Dict[str, Any]]:
        # POST /api/v1/payments with { out: true, bolt11, max_fee }
        url = f"{self.base}/api/v1/payments"
        max_fee = int(max_fee_sats or current_app.config.get("LNBITS_MAX_FEE_SATS", 20))
        data = {"out": True, "bolt11": bolt11, "max_fee": max_fee}
        ok, j, status, text = self._request_with_retry("POST", url, self._hdr(self.admin_key), data, timeout=30)
        self._maybe_log("pay_invoice", data, status, text, ok)
        if not ok and self.alt_base:
            url2 = f"{self.alt_base}/api/v1/payments"
            ok, j, status, text = self._request_with_retry("POST", url2, self._hdr(self.alt_admin_key or self.admin_key), data, timeout=30)
            self._maybe_log("pay_invoice", data, status, text, ok)
        return ok, j
