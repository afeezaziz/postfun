import json
import hashlib
import time
from typing import Any, Dict, Optional, Tuple

from flask import current_app

try:
    # bech32 reference implementation
    from bech32 import bech32_decode, bech32_encode, convertbits  # type: ignore
except Exception:  # pragma: no cover - fallback if convertbits not available
    from typing import List

    def bech32_decode(addr: str) -> Tuple[Optional[str], Optional[list]]:
        raise RuntimeError("bech32 library not available")

    def bech32_encode(hrp: str, data: bytes) -> str:
        raise RuntimeError("bech32 library not available")

    def convertbits(data: List[int], frombits: int, tobits: int, pad: bool = True) -> Optional[bytes]:
        acc = 0
        bits = 0
        ret = []
        maxv = (1 << tobits) - 1
        max_acc = (1 << (frombits + tobits - 1)) - 1
        for value in data:
            if value < 0 or (value >> frombits):
                return None
            acc = ((acc << frombits) | value) & max_acc
            bits += frombits
            while bits >= tobits:
                bits -= tobits
                ret.append((acc >> bits) & maxv)
        if pad and bits:
            ret.append((acc << (tobits - bits)) & maxv)
        elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
            return None
        return bytes(ret)

# Robust import for schnorr verification across coincurve versions
try:
    # Preferred path in some versions
    from coincurve.schnorr import verify as schnorr_verify  # type: ignore
except Exception:  # pragma: no cover - fallback for environments without submodule
    try:
        # Module-level schnorr object in other versions
        from coincurve import schnorr  # type: ignore

        def schnorr_verify(sig: bytes, msg: bytes, pubkey: bytes) -> bool:
            try:
                return bool(schnorr.verify(signature=sig, message=msg, public_key=pubkey))
            except Exception:
                return False
    except Exception:  # pragma: no cover - final fallback using PublicKey API
        try:
            from coincurve import PublicKey  # type: ignore

            def schnorr_verify(sig: bytes, msg: bytes, pubkey: bytes) -> bool:
                try:
                    pk = PublicKey(pubkey)
                    # In recent coincurve versions, this verifies BIP340-style signatures
                    return bool(pk.schnorr_verify(sig, msg))
                except Exception:
                    return False
        except Exception:  # pragma: no cover - if coincurve entirely missing
            def schnorr_verify(sig: bytes, msg: bytes, pubkey: bytes) -> bool:  # type: ignore
                return False


def npub_to_hex(npub: str) -> str:
    """Convert an npub bech32 string to hex pubkey."""
    try:
        hrp, data = bech32_decode(npub)  # type: ignore[arg-type]
        if hrp != "npub" or data is None:
            raise ValueError("Invalid npub")
        raw = convertbits(data, 5, 8, False)
        if raw is None:
            raise ValueError("Failed to convert bits")
        return bytes(raw).hex()
    except Exception as e:  # pragma: no cover - depend on external lib
        raise ValueError(f"Invalid npub: {e}")


def hex_to_npub(pubkey_hex: str) -> str:
    """Convert a 32-byte hex pubkey to npub (bech32)."""
    raw = bytes.fromhex(pubkey_hex)
    data5 = convertbits(list(raw), 8, 5, True)
    if data5 is None:
        raise ValueError("Failed to convert bits")
    return bech32_encode("npub", list(data5))


def compute_event_id(event: Dict[str, Any]) -> str:
    """Compute Nostr event id per NIP-01 from fields."""
    pubkey = event.get("pubkey")
    created_at = event.get("created_at")
    kind = event.get("kind")
    tags = event.get("tags", [])
    content = event.get("content", "")
    data = [0, pubkey, created_at, kind, tags, content]
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def verify_nostr_event_signature(event: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify the signature on a Nostr event and return (ok, pubkey_hex)."""
    try:
        event_id = compute_event_id(event)
        if event.get("id") != event_id:
            return False, ""
        sig_hex = event.get("sig")
        pub_hex = event.get("pubkey")
        if not sig_hex or not pub_hex:
            return False, ""
        sig = bytes.fromhex(sig_hex)
        msg = bytes.fromhex(event_id)
        pub = bytes.fromhex(pub_hex)
        ok = schnorr_verify(sig=sig, msg=msg, pubkey=pub)
        return bool(ok), pub_hex if ok else ""
    except Exception:
        return False, ""


def validate_login_event(event: Dict[str, Any], expected_challenge_id: str, expected_challenge: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Validate a signed login event contains our challenge payload and a valid signature.

    Returns (ok, pubkey_hex, content_obj)
    """
    ok, pub_hex = verify_nostr_event_signature(event)
    if not ok:
        return False, "", None

    try:
        content_obj = json.loads(event.get("content", "{}"))
    except Exception:
        return False, "", None

    # Basic schema checks
    if content_obj.get("challenge_id") != expected_challenge_id:
        return False, "", None
    if content_obj.get("challenge") != expected_challenge:
        return False, "", None

    # Domain and expiry hints (optional, but we validate if present)
    now = int(time.time())
    exp = content_obj.get("exp")
    if isinstance(exp, int) and exp < now - int(current_app.config.get("AUTH_MAX_CLOCK_SKEW", 300)):
        return False, "", None

    domain = content_obj.get("domain")
    expected_domain = current_app.config.get("LOGIN_DOMAIN") or "postfun"
    if domain and domain != expected_domain:
        return False, "", None

    return True, pub_hex, content_obj
