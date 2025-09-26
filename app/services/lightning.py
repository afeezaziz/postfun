import requests
from typing import Optional, Tuple, Dict, Any
from flask import current_app


class LNBitsClient:
    def __init__(self,
                 api_url: Optional[str] = None,
                 invoice_key: Optional[str] = None,
                 admin_key: Optional[str] = None):
        cfg = current_app.config
        self.base = (api_url or cfg.get("LNBITS_API_URL", "")).rstrip("/")
        self.invoice_key = invoice_key or cfg.get("LNBITS_INVOICE_KEY", "")
        self.admin_key = admin_key or cfg.get("LNBITS_ADMIN_KEY", "")
        if not self.base:
            raise RuntimeError("LNBITS_API_URL is not configured")

    def _hdr(self, key: str) -> Dict[str, str]:
        return {"X-Api-Key": key, "Content-Type": "application/json"}

    def create_invoice(self, amount_sats: int, memo: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        # POST /api/v1/payments with { out: false, amount, memo }
        url = f"{self.base}/api/v1/payments"
        memo = memo or current_app.config.get("LNBITS_DEFAULT_MEMO", "Deposit")
        data = {"out": False, "amount": int(amount_sats), "memo": memo}
        r = requests.post(url, headers=self._hdr(self.invoice_key), json=data, timeout=20)
        if r.status_code >= 400:
            return False, {"status": r.status_code, "error": r.text}
        try:
            j = r.json()
        except Exception:
            return False, {"status": r.status_code, "error": "invalid_json"}
        return True, j

    def get_payment_status(self, payment_hash: str) -> Tuple[bool, Dict[str, Any]]:
        # GET /api/v1/payments/{payment_hash}
        url = f"{self.base}/api/v1/payments/{payment_hash}"
        # Either key works for read
        r = requests.get(url, headers=self._hdr(self.invoice_key or self.admin_key), timeout=20)
        if r.status_code >= 400:
            return False, {"status": r.status_code, "error": r.text}
        try:
            j = r.json()
        except Exception:
            return False, {"status": r.status_code, "error": "invalid_json"}
        return True, j

    def pay_invoice(self, bolt11: str, max_fee_sats: Optional[int] = None) -> Tuple[bool, Dict[str, Any]]:
        # POST /api/v1/payments with { out: true, bolt11, max_fee }
        url = f"{self.base}/api/v1/payments"
        max_fee = int(max_fee_sats or current_app.config.get("LNBITS_MAX_FEE_SATS", 20))
        data = {"out": True, "bolt11": bolt11, "max_fee": max_fee}
        r = requests.post(url, headers=self._hdr(self.admin_key), json=data, timeout=30)
        if r.status_code >= 400:
            return False, {"status": r.status_code, "error": r.text}
        try:
            j = r.json()
        except Exception:
            return False, {"status": r.status_code, "error": "invalid_json"}
        return True, j
