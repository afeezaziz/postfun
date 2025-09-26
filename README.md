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

### Migrations (Alembic via Flask-Migrate)
We use Alembic (through Flask-Migrate) to manage schema changes.

Common commands:

```bash
# 1) Initialize migrations folder (first time only)
uv run python -m flask --app wsgi db init

# 2) Create a migration from model changes
uv run python -m flask --app wsgi db migrate -m "add lightning tables"

# 3) Apply the migration
uv run python -m flask --app wsgi db upgrade

# (Optional) Downgrade one revision
uv run python -m flask --app wsgi db downgrade -1
```

## Virtual Pool AMM (Token Swaps)
This backend includes a simple constant-product virtual pool AMM with stage-based fee halving and burn events.

- Four stages (1..4). When cumulative volume crosses configured thresholds, the pool progresses to the next stage.
- On each stage progression, a burn event is recorded for a configured burn token and amount.
- Trading fee is in basis points (bps) and halves at each stage:
  - Stage 1: base fee_bps
  - Stage 2: base/2
  - Stage 3: base/4
  - Stage 4: base/8
- Fees are accumulated per pool (fee_accum_a/fee_accum_b) outside reserves for accounting.

Tables: `swap_pools`, `swap_trades`, `burn_events`, `token_balances`.

Endpoints:

- `POST /api/amm/pools` (admin only)
  - Body supports token ids or symbols:
  - `{ "symbol_a":"gBTC", "symbol_b":"gUSD", "reserve_a": 1000, "reserve_b": 65000000, "fee_bps_base": 30, "stage1_threshold": 1000, "stage2_threshold": 2000, "stage3_threshold": 5000, "burn_token_id": 4, "burn_stage1_amount": 100, "burn_stage2_amount": 80, "burn_stage3_amount": 60, "burn_stage4_amount": 40 }`
  - Returns pool.

- `GET /api/amm/pools`
  - Lists pools

- `GET /api/amm/pools/<pool_id>`
  - Gets pool details

- `POST /api/amm/quote` (auth)
  - Body: `{ "pool_id": 1, "side": "AtoB"|"BtoA", "amount_in": 10 }`
  - Returns: `amount_out`, `fee_bps`, `fee_amount`, `effective_in`.

- `POST /api/amm/swap` (auth)
  - Body: `{ "pool_id": 1, "side": "AtoB", "amount_in": 10 }`
  - Executes swap, updates `token_balances`, `swap_pools` reserves, records `swap_trades` and stage burns.

- `GET /api/amm/balances` (auth)
  - Returns the caller's token balances across pools.

Example flow (assuming you are admin and have a JWT):

```bash
# Create a pool for gBTC/gUSD with base 0.30% fee and stage thresholds
curl -X POST -H "Content-Type: application/json" \
     -H "Authorization: Bearer $JWT" \
     -d '{
           "symbol_a":"gBTC", "symbol_b":"gUSD",
           "reserve_a": 1000, "reserve_b": 65000000,
           "fee_bps_base": 30,
           "stage1_threshold": 1000, "stage2_threshold": 2000, "stage3_threshold": 5000,
           "burn_token_id": 4,
           "burn_stage1_amount": 100,
           "burn_stage2_amount": 80,
           "burn_stage3_amount": 60,
           "burn_stage4_amount": 40
         }' \
     http://localhost:8000/api/amm/pools

# Quote a swap of 1 gBTC -> gUSD
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $JWT" \
     -d '{"pool_id":1,"side":"AtoB","amount_in":1}' \
     http://localhost:8000/api/amm/quote

# Execute the swap (requires you to have token balances; set up via admin or faucet)
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $JWT" \
     -d '{"pool_id":1,"side":"AtoB","amount_in":1}' \
     http://localhost:8000/api/amm/swap

# Check your token balances
curl -H "Authorization: Bearer $JWT" http://localhost:8000/api/amm/balances
```

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
  
- LNbits Lightning provider (for deposits/withdrawals):
  - `LNBITS_API_URL` – e.g. `https://legend.lnbits.com` or your LNbits instance
  - `LNBITS_INVOICE_KEY` – invoice/read key for creating invoices
  - `LNBITS_ADMIN_KEY` – admin key for paying invoices (withdrawals)
  - `LNBITS_DEFAULT_MEMO` – default memo for generated invoices (optional)
  - `LNBITS_MAX_FEE_SATS` – max fee sats to pay on withdrawals (default `20`)

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
LNBITS_API_URL=
LNBITS_INVOICE_KEY=
LNBITS_ADMIN_KEY=
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
## Notes
- Login uses a standard NIP-01 event signed by the user; the backend verifies the signature with BIP340, matches the challenge, and returns a JWT.
- This backend intentionally keeps protocol logic out. For DeFi/AMM flows, follow `docs/gateway/` and build a separate solver service as outlined there.
- For local development without MariaDB, the app will create SQLite tables automatically.

## Lightning API (Deposit & Withdraw BTC over Lightning)
All endpoints require a valid JWT (`Authorization: Bearer <jwt>`) and expect/return JSON.

- `GET /api/lightning/balance`
  - Returns the per-user BTC balance in sats.
  - Response: `{ "user_id", "asset": "BTC", "balance_sats" }`

- `POST /api/lightning/deposit`
  - Body: `{ "amount_sats": <int>, "memo?": <string> }`
  - Creates an invoice using LNbits.
  - Response: `{ id, user_id, amount_sats, payment_request, payment_hash, status }`

- `GET /api/lightning/invoices/<id>`
  - Polls LNbits for payment status; when paid, it credits the user's internal balance and writes a ledger entry.
  - Response mirrors `LightningInvoice.to_dict()`.

- `POST /api/lightning/withdraw`
  - Body: `{ "bolt11": <string>, "amount_sats": <int> }`
  - Deducts the amount from the internal balance and pays the invoice via LNbits with a max fee cap.
  - On provider failure, the amount is refunded and status becomes `failed`.
  - Response: `{ id, user_id, amount_sats, fee_sats?, status }`

- `GET /api/lightning/withdrawals/<id>`
  - Checks withdrawal status; if confirmed, records fee (if provided by provider) as a negative ledger entry.
  - Response mirrors `LightningWithdrawal.to_dict()`.

### Curl examples
```bash
# Get balance
curl -H "Authorization: Bearer $JWT" http://localhost:8000/api/lightning/balance

# Create deposit invoice (10k sats)
curl -X POST -H "Content-Type: application/json" \
     -H "Authorization: Bearer $JWT" \
     -d '{"amount_sats":10000}' \
     http://localhost:8000/api/lightning/deposit

# Check invoice
curl -H "Authorization: Bearer $JWT" http://localhost:8000/api/lightning/invoices/$INVOICE_ID

# Withdraw 5k sats to BOLT11
curl -X POST -H "Content-Type: application/json" \
     -H "Authorization: Bearer $JWT" \
     -d '{"bolt11":"lnbc50u1...","amount_sats":5000}' \
     http://localhost:8000/api/lightning/withdraw

# Check withdrawal
curl -H "Authorization: Bearer $JWT" http://localhost:8000/api/lightning/withdrawals/$WITHDRAW_ID
```

## License
MIT (or project license)
