# Postfun Backend (Flask + Nostr Auth)

Postfun is a fun, retro-styled social finance platform. This folder contains a minimal Flask backend scaffold with Nostr-based authentication, SQLAlchemy (MariaDB) persistence, and JWT issuance. It’s designed to pair with the gateway docs in `docs/gateway/` and can be extended into a complete app.

## Stack
- Python 3.11
- Flask 3, Flask-CORS
- SQLAlchemy 2 (+ Flask-SQLAlchemy)
- MariaDB via PyMySQL (SQLite fallback for local dev)
- JWT via PyJWT
- Nostr signature verify via coincurve (BIP340 Schnorr)
- Nostr utils via pynostr and bech32 helpers
- Gunicorn for production serving

## Directory
- `app/`
  - `__init__.py` – app factory, CORS, health route
  - `config.py` – env-driven config (DB/JWT/CORS)
  - `extensions.py` – SQLAlchemy init
  - `models.py` – `User`, `AuthChallenge`
  - `auth/` – auth blueprint (`/auth/*`)
  - `utils/nostr.py` – npub/hex helpers, NIP-01 event id & Schnorr verify
  - `utils/jwt_utils.py` – JWT helpers + `require_auth` decorator
- `wsgi.py` – WSGI entry (Gunicorn target)
- `docs/` – platform docs: gateway, solver, defi guides

## Setup
Ensure Python 3.11 is active (see `.python-version`). With uv (recommended):

```bash
# Create virtual env and install deps
uv venv
source .venv/bin/activate
uv pip install -e .
```

Alternatively with pip:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration
Set environment variables as needed (SQLite fallback is automatic):

- `SECRET_KEY` – Flask secret (default dev value)
- `DATABASE_URL` – full SQLAlchemy URL (overrides below) e.g. `mysql+pymysql://user:pass@host:3306/dbname`
- `MARIADB_HOST`, `MARIADB_PORT`, `MARIADB_USER`, `MARIADB_PASSWORD`, `MARIADB_DB`
- `CORS_ORIGINS` – default `*`
- `JWT_SECRET`, `JWT_ALGORITHM` (default HS256), `JWT_EXPIRES_SECONDS` (default 86400)
- `AUTH_CHALLENGE_TTL` (default 600 seconds)
- `AUTH_MAX_CLOCK_SKEW` (default 300 seconds)
- `LOGIN_DOMAIN` (optional, UX/context hint)

Example `.env`:
```env
SECRET_KEY=dev-secret-change-me
JWT_SECRET=dev-jwt-change-me
MARIADB_HOST=127.0.0.1
MARIADB_PORT=3306
MARIADB_USER=postfun
MARIADB_PASSWORD=changeme
MARIADB_DB=postfun
CORS_ORIGINS=*
```

## Run
- Dev server (Flask):
```bash
uv run python -m flask --app wsgi run --debug --host 0.0.0.0 --port 8000
```

- Production (Gunicorn):
```bash
uv run gunicorn -w 2 -k gthread -b 0.0.0.0:8000 wsgi:app
```

## Auth API (Nostr-based)
- `POST /auth/challenge`
  - Body (optional): `{ "pubkey": "<hex>", "npub": "<npub>" }`
  - Response: `{ "challenge_id", "challenge", "expires_at", "ttl_seconds" }`

- `POST /auth/verify`
  - Body: `{ "event": <nostr_event_object> }`
  - The Nostr event must be a standard NIP-01 event; its `content` must be JSON including the fields below.
  - Required `content` fields: `{ "challenge_id", "challenge", "domain?", "exp?" }`
  - Response: `{ "token": "<jwt>", "token_type": "Bearer", "expires_in": <seconds>, "user": { ... } }`

- `GET /auth/me`
  - Header: `Authorization: Bearer <jwt>`
  - Response: `{ "user": { id, pubkey, npub, display_name, created_at } }`

- `POST /auth/logout`
  - Header: `Authorization: Bearer <jwt>`
  - Response: `{ "ok": true }` (stateless JWT; clients discard token)

## NIP-07 Login Example (Browser)
```html
<script>
async function nostrLogin() {
  // 1) Fetch challenge
  const chRes = await fetch('/auth/challenge', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  const ch = await chRes.json();
  const gatewayDomain = 'postfun';

  // 2) Build NIP-01 event with challenge in content
  const pubkey = await window.nostr.getPublicKey();
  const content = JSON.stringify({
    challenge_id: ch.challenge_id,
    challenge: ch.challenge,
    domain: gatewayDomain,
    exp: Math.floor(Date.now() / 1000) + 10 * 60
  });
  const evt = {
    kind: 1, // standard NIP-01 event
    content,
    tags: [],
    created_at: Math.floor(Date.now() / 1000),
    pubkey,
  };

  // 3) Sign and send to backend (no relay publish required for login)
  const signed = await window.nostr.signEvent(evt);
  const v = await fetch('/auth/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event: signed })
  });
  const out = await v.json();
  if (out.token) {
    // Store JWT and proceed
    localStorage.setItem('postfun_jwt', out.token);
    console.log('Logged in as', out.user);
  } else {
    console.error('Login failed', out);
  }
}
</script>
```

## Notes
- Login uses a standard NIP-01 event signed by the user; the backend verifies the signature with BIP340, matches the challenge, and returns a JWT.
- This backend intentionally keeps protocol logic out. For DeFi/AMM flows, follow `docs/gateway/` and build a separate solver service as outlined there.
- For local development without MariaDB, the app will create SQLite tables automatically.

## License
MIT (or project license)
