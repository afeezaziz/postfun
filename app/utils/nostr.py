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
    import sys
    try:
        print(f"[DEBUG] Verifying signature for event: {json.dumps(event, indent=2)}", file=sys.stderr)

        event_id = compute_event_id(event)
        print(f"[DEBUG] Computed event_id: {event_id}", file=sys.stderr)
        print(f'[DEBUG] Event has id: {event.get("id")}', file=sys.stderr)

        if event.get("id") != event_id:
            print(f'[DEBUG] Event ID mismatch: computed {event_id}, event has {event.get("id")}', file=sys.stderr)
            return False, ""

        sig_hex = event.get("sig")
        pub_hex = event.get("pubkey")
        print(f"[DEBUG] Signature: {sig_hex}", file=sys.stderr)
        print(f"[DEBUG] Public key: {pub_hex}", file=sys.stderr)

        if not sig_hex or not pub_hex:
            print(f"[DEBUG] Missing signature or pubkey: sig={sig_hex}, pubkey={pub_hex}", file=sys.stderr)
            return False, ""

        sig = bytes.fromhex(sig_hex)
        pub = bytes.fromhex(pub_hex)

        # Try both event ID hash and raw serialized data for verification
        # Standard Nostr signs the serialized event data, not just the hash
        pubkey = event.get("pubkey")
        created_at = event.get("created_at")
        kind = event.get("kind")
        tags = event.get("tags", [])
        content = event.get("content", "")
        data = [0, pubkey, created_at, kind, tags, content]
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        msg_raw = serialized.encode("utf-8")
        msg_hash = bytes.fromhex(event_id)

        print(f"[DEBUG] Serialized event length: {len(msg_raw)}", file=sys.stderr)
        print(f"[DEBUG] Serialized event: {serialized}", file=sys.stderr)

        # Try with hash first (current method)
        msg = msg_hash

        print(f"[DEBUG] Signature bytes length: {len(sig)}", file=sys.stderr)
        print(f"[DEBUG] Message bytes length: {len(msg)}", file=sys.stderr)
        print(f"[DEBUG] Public key bytes length: {len(pub)}", file=sys.stderr)

        # Debug signature format analysis
        print(f"[DEBUG] Signature hex: {sig_hex}", file=sys.stderr)
        print(f"[DEBUG] Signature first 4 bytes: {sig[:4].hex()}", file=sys.stderr)
        print(f"[DEBUG] Signature last 4 bytes: {sig[-4:].hex()}", file=sys.stderr)
        print(f"[DEBUG] Public key first 4 bytes: {pub[:4].hex()}", file=sys.stderr)
        print(f"[DEBUG] Public key last 4 bytes: {pub[-4:].hex()}", file=sys.stderr)

        # Try standard schnorr verification first
        ok = schnorr_verify(sig=sig, msg=msg, pubkey=pub)
        print(f"[DEBUG] Standard schnorr verification result: {ok}", file=sys.stderr)

        if ok:
            return True, pub_hex

        # Try with raw serialized data (some wallets sign the raw data instead of hash)
        try:
            print(f"[DEBUG] Trying verification with raw serialized data", file=sys.stderr)
            ok_raw = schnorr_verify(sig=sig, msg=msg_raw, pubkey=pub)
            print(f"[DEBUG] Raw serialized data verification result: {ok_raw}", file=sys.stderr)
            if ok_raw:
                print(f"[DEBUG] Success with raw serialized data verification", file=sys.stderr)
                return True, pub_hex
        except Exception as e:
            print(f"[DEBUG] Raw serialized data verification failed: {e}", file=sys.stderr)

        # If standard verification fails, try alternative approaches for wallet compatibility
        print(f"[DEBUG] Standard verification failed, trying wallet compatibility checks", file=sys.stderr)

        # Try with different signature byte order (some wallets use little-endian)
        try:
            sig_le = sig[::-1]  # Reverse byte order
            ok_le = schnorr_verify(sig=sig_le, msg=msg, pubkey=pub)
            print(f"[DEBUG] Little-endian signature verification result: {ok_le}", file=sys.stderr)
            if ok_le:
                print(f"[DEBUG] Success with little-endian signature format", file=sys.stderr)
                return True, pub_hex
        except Exception as e:
            print(f"[DEBUG] Little-endian attempt failed: {e}", file=sys.stderr)

        # Try with different message encoding (add prefix if missing)
        try:
            # Some wallets expect a message prefix according to BIP-340
            msg_with_prefix = b"\x18" + b"BIP0340/challenge" + b"\x00" + msg
            ok_prefix = schnorr_verify(sig=sig, msg=msg_with_prefix, pubkey=pub)
            print(f"[DEBUG] BIP-340 prefixed message verification result: {ok_prefix}", file=sys.stderr)
            if ok_prefix:
                print(f"[DEBUG] Success with BIP-340 message prefix", file=sys.stderr)
                return True, pub_hex
        except Exception as e:
            print(f"[DEBUG] BIP-340 prefix attempt failed: {e}", file=sys.stderr)

        # Try pynostr library for Nostr-specific verification
        try:
            from pynostr.event import Event
            print(f"[DEBUG] Trying pynostr library fallback", file=sys.stderr)
            # Create a pynostr Event object and use its built-in verification
            nostr_event = Event()
            nostr_event.id = event.get("id")
            nostr_event.pubkey = event.get("pubkey")
            nostr_event.created_at = event.get("created_at")
            nostr_event.kind = event.get("kind")
            nostr_event.tags = event.get("tags", [])
            nostr_event.content = event.get("content", "")
            nostr_event.sig = event.get("sig")

            # Use pynostr's built-in verification method
            ok_pynostr = nostr_event.verify()
            print(f"[DEBUG] pynostr verification result: {ok_pynostr}", file=sys.stderr)
            if ok_pynostr:
                print(f"[DEBUG] Success with pynostr library", file=sys.stderr)
                return True, pub_hex
        except ImportError:
            print(f"[DEBUG] pynostr library not available", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] pynostr verification attempt failed: {e}", file=sys.stderr)

        # Try alternative schnorr libraries if available
        try:
            import secp256k1
            print(f"[DEBUG] Trying secp256k1 library fallback", file=sys.stderr)
            # Use secp256k1 library properly with raw 32-byte pubkey
            secp = secp256k1.PublicKey()
            # First deserialize the public key from 32 bytes
            secp.deserialize(pub)
            ok_secp = secp.schnorr_verify(msg, sig, raw=True)
            print(f"[DEBUG] secp256k1 verification result: {ok_secp}", file=sys.stderr)
            if ok_secp:
                print(f"[DEBUG] Success with secp256k1 library", file=sys.stderr)
                return True, pub_hex
        except ImportError:
            print(f"[DEBUG] secp256k1 library not available", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] secp256k1 verification attempt failed: {e}", file=sys.stderr)

        print(f"[DEBUG] All verification methods failed", file=sys.stderr)
        return False, ""

    except Exception as e:
        print(f"[DEBUG] Signature verification failed with exception: {e}", file=sys.stderr)
        print(f"[DEBUG] Exception details: {type(e).__name__}", file=sys.stderr)
        import traceback
        print(f"[DEBUG] Traceback: {traceback.format_exc()}", file=sys.stderr)
        return False, ""


def validate_login_event(event: Dict[str, Any], expected_challenge_id: str, expected_challenge: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Validate a signed login event contains our challenge payload and a valid signature.

    Returns (ok, pubkey_hex, content_obj)
    """
    print(f"[DEBUG] Starting event validation for {expected_challenge_id}")

    ok, pub_hex = verify_nostr_event_signature(event)
    print(f"[DEBUG] Signature verification result: ok={ok}, pub_hex={pub_hex}")
    if not ok:
        print(f"[DEBUG] Signature verification failed for event: {json.dumps(event, indent=2)}")
        return False, "", None

    try:
        content_obj = json.loads(event.get("content", "{}"))
        print(f"[DEBUG] Content parsed successfully: {json.dumps(content_obj, indent=2)}")
    except Exception as e:
        print(f'[DEBUG] Failed to parse content: {event.get("content")}, error: {e}')
        return False, "", None

    # Basic schema checks
    challenge_id = content_obj.get("challenge_id")
    challenge = content_obj.get("challenge")
    print(f"[DEBUG] Content challenge_id: {challenge_id}, expected: {expected_challenge_id}")
    print(f"[DEBUG] Content challenge: {challenge}, expected: {expected_challenge}")

    if challenge_id != expected_challenge_id:
        print(f"[DEBUG] Challenge ID mismatch: got {challenge_id}, expected {expected_challenge_id}")
        return False, "", None
    if challenge != expected_challenge:
        print(f"[DEBUG] Challenge mismatch: got {challenge}, expected {expected_challenge}")
        return False, "", None

    # Domain and expiry hints (optional, but we validate if present)
    now = int(time.time())
    exp = content_obj.get("exp")
    print(f"[DEBUG] Expiry check: exp={exp}, now={now}")
    if isinstance(exp, int) and exp < now - int(current_app.config.get("AUTH_MAX_CLOCK_SKEW", 300)):
        print(f"[DEBUG] Event expired: exp={exp}, now={now}")
        return False, "", None

    domain = content_obj.get("domain")
    expected_domain = current_app.config.get("LOGIN_DOMAIN") or "postfun"
    print(f"[DEBUG] Domain check: domain={domain}, expected={expected_domain}")
    if domain and domain != expected_domain:
        print(f"[DEBUG] Domain mismatch: got {domain}, expected {expected_domain}")
        return False, "", None

    print(f"[DEBUG] Event validation successful for {pub_hex}")
    return True, pub_hex, content_obj
