from __future__ import annotations

import os
import time
from typing import Optional

from flask import current_app

try:
    from pynostr.key import PrivateKey  # type: ignore
    from pynostr.event import Event as NostrEvent  # type: ignore
    from pynostr.relay import Relay  # type: ignore
except Exception:  # pragma: no cover
    PrivateKey = None  # type: ignore
    NostrEvent = None  # type: ignore
    Relay = None  # type: ignore


def send_dm(recipient_pubkey_hex: str, message: str) -> bool:
    """Send a Nostr direct message (NIP-04 style) to recipient.

    This is best-effort. Requires env:
      - NOSTR_SECRET_KEY_HEX: server's private key in hex
      - NOSTR_RELAY_URL: relay URL (e.g., wss://relay.damus.io)

    Returns True if the send was attempted and likely succeeded.
    """
    if PrivateKey is None or NostrEvent is None or Relay is None:
        current_app.logger.info("[nostr_dm] pynostr not available; skipping DM: %s", message)
        return False

    seckey_hex = os.getenv("NOSTR_SECRET_KEY_HEX")
    relay_url = os.getenv("NOSTR_RELAY_URL")
    if not seckey_hex or not relay_url:
        current_app.logger.info("[nostr_dm] missing NOSTR_SECRET_KEY_HEX or NOSTR_RELAY_URL; skipping DM")
        return False

    try:
        sk = PrivateKey.from_nsec_or_hex(seckey_hex)
        # Kind 4 is Encrypted Direct Messages (NIP-04);
        # pynostr Event supports set_content with encryption using recipient pubkey if available.
        ev = NostrEvent(kind=4)
        ev.public_key = sk.public_key.hex()
        ev.created_at = int(time.time())
        ev.tags = [["p", recipient_pubkey_hex]]
        ev.set_content(message, sk, recipient_pubkey_hex)
        ev.sign(sk.hex())
        relay = Relay(relay_url)
        relay.connect()
        relay.publish(ev)
        relay.close()
        current_app.logger.info("[nostr_dm] sent alert DM to %s", recipient_pubkey_hex)
        return True
    except Exception as e:
        current_app.logger.warning("[nostr_dm] failed to send DM: %s", e)
        return False
