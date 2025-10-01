#!/usr/bin/env python3

import requests
import json
import base64
import time
from hashlib import sha256

def create_nostr_event(pubkey_hex: str, privkey_hex: str) -> dict:
    """Create a simple Nostr event for authentication"""
    created_at = int(time.time())
    event_data = {
        "id": "",
        "pubkey": pubkey_hex,
        "created_at": created_at,
        "kind": 27235,  # HTTP authentication
        "tags": [],
        "content": "Authentication: {}".format(created_at),
        "sig": ""
    }

    # Create event ID (SHA256 of serialized event)
    event_json = json.dumps([
        0,
        event_data["pubkey"],
        event_data["created_at"],
        event_data["kind"],
        event_data["tags"],
        event_data["content"]
    ], separators=(',', ':'))

    event_id = sha256(event_json.encode()).hexdigest()
    event_data["id"] = event_id

    # TODO: Add proper signature - for now just return unsigned event
    # This will need to be properly signed for actual use
    return event_data

def test_withdrawal():
    # Test withdrawal with a sample BOLT11 invoice
    # Using a test invoice from lnbits

    # You'll need to provide:
    # 1. Your nostr pubkey/privkey for authentication
    # 2. A valid BOLT11 invoice to pay

    pubkey = "YOUR_NOSTR_PUBKEY_HEX"  # Replace with actual pubkey
    privkey = "YOUR_NOSTR_PRIVKEY_HEX"  # Replace with actual privkey

    # Sample bolt11 (replace with actual invoice you want to pay)
    bolt11_invoice = "lnbc1600n1pjlusppp5..."

    # Create authentication event
    event = create_nostr_event(pubkey, privkey)

    # Base64 encode the event
    event_b64 = base64.b64encode(json.dumps(event).encode()).decode()

    # Make withdrawal request
    url = "http://localhost:8000/api/lightning/withdraw"
    headers = {
        "Authorization": f"Nostr {event_b64}",
        "Content-Type": "application/json"
    }

    data = {
        "bolt11": bolt11_invoice,
        "amount_sats": 100  # Amount to withdraw
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    print("This script needs to be configured with proper Nostr keys and BOLT11 invoice")
    print("Please update the pubkey, privkey, and bolt11_invoice variables")