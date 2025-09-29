import os
from datetime import timedelta


class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # Database
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL:
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
    else:
        MARIADB_HOST = os.getenv("MARIADB_HOST")
        MARIADB_PORT = os.getenv("MARIADB_PORT", "3306")
        MARIADB_USER = os.getenv("MARIADB_USER")
        MARIADB_PASSWORD = os.getenv("MARIADB_PASSWORD")
        MARIADB_DB = os.getenv("MARIADB_DB")
        if MARIADB_HOST and MARIADB_USER and MARIADB_DB:
            SQLALCHEMY_DATABASE_URI = (
                f"mysql+pymysql://{MARIADB_USER}:{MARIADB_PASSWORD or ''}@{MARIADB_HOST}:{MARIADB_PORT}/{MARIADB_DB}"
            )
        else:
            # Fallback to SQLite for local dev
            SQLALCHEMY_DATABASE_URI = os.getenv(
                "SQLITE_URI", f"sqlite:///{os.path.abspath('postfun.db')}"
            )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # CORS
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

    # JWT
    JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-change-me")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRES_DELTA = int(os.getenv("JWT_EXPIRES_SECONDS", str(24 * 3600)))  # 1 day

    # Auth challenge
    AUTH_CHALLENGE_TTL = int(os.getenv("AUTH_CHALLENGE_TTL", str(10 * 60)))  # 10 minutes
    AUTH_MAX_CLOCK_SKEW = int(os.getenv("AUTH_MAX_CLOCK_SKEW", str(5 * 60)))  # 5 minutes

    # Rate limiting
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "100 per hour")
    RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "10 per minute")
    # Flask-Limiter storage (use Redis in prod if available)
    # Examples:
    #   REDIS_URL=redis://localhost:6379/0
    #   RATELIMIT_STORAGE_URI=redis://:pass@host:6379/1
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI") or os.getenv("REDIS_URL") or "memory://"

    # CSRF (Flask-SeaSurf uses cookie 'csrf_token' by default)
    CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "csrf_token")

    # AMM parameters
    AMM_DEFAULT_MAX_SLIPPAGE_BPS = int(os.getenv("AMM_DEFAULT_MAX_SLIPPAGE_BPS", "500"))  # 5%
    AMM_MIN_TRADE_OUTPUT = os.getenv("AMM_MIN_TRADE_OUTPUT", "0.00000001")
    AMM_MIN_RESERVE = os.getenv("AMM_MIN_RESERVE", "0.000001")

    # Lightning (LNbits provider)
    LNBITS_API_URL = os.getenv("LNBITS_API_URL", "")  # e.g. https://legend.lnbits.com
    # Use invoice/read key to create invoices, admin key to pay invoices.
    LNBITS_INVOICE_KEY = os.getenv("LNBITS_INVOICE_KEY", "")
    LNBITS_ADMIN_KEY = os.getenv("LNBITS_ADMIN_KEY", "")
    LNBITS_DEFAULT_MEMO = os.getenv("LNBITS_DEFAULT_MEMO", "Postfun deposit")
    # Optional fee cap for withdrawals
    LNBITS_MAX_FEE_SATS = int(os.getenv("LNBITS_MAX_FEE_SATS", "20"))

    # Optional LNbits failover + retry
    LNBITS_ALT_API_URL = os.getenv("LNBITS_ALT_API_URL", "")
    LNBITS_ALT_INVOICE_KEY = os.getenv("LNBITS_ALT_INVOICE_KEY", "")
    LNBITS_ALT_ADMIN_KEY = os.getenv("LNBITS_ALT_ADMIN_KEY", "")
    LNBITS_RETRY_ATTEMPTS = int(os.getenv("LNBITS_RETRY_ATTEMPTS", "2"))
    LNBITS_RETRY_BACKOFF_MS = int(os.getenv("LNBITS_RETRY_BACKOFF_MS", "300"))

    # Reconciliation scheduler
    SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "0")
    RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "60"))
    RECONCILE_INVOICES_MIN_AGE_SEC = int(os.getenv("RECONCILE_INVOICES_MIN_AGE_SEC", "30"))
    RECONCILE_WITHDRAW_MIN_AGE_SEC = int(os.getenv("RECONCILE_WITHDRAW_MIN_AGE_SEC", "30"))

    # Withdraw controls
    WITHDRAW_MAX_SINGLE_SATS = int(os.getenv("WITHDRAW_MAX_SINGLE_SATS", "100000"))
    WITHDRAW_DAILY_MAX_SATS = int(os.getenv("WITHDRAW_DAILY_MAX_SATS", "500000"))
    WITHDRAW_DAILY_MAX_COUNT = int(os.getenv("WITHDRAW_DAILY_MAX_COUNT", "10"))

    # Ops alerts
    OP_ALERTS_ENABLED = os.getenv("OP_ALERTS_ENABLED", "0")
    OP_ALERTS_WEBHOOK_URL = os.getenv("OP_ALERTS_WEBHOOK_URL", "")
    OP_ALERTS_INTERVAL_SECONDS = int(os.getenv("OP_ALERTS_INTERVAL_SECONDS", "300"))
    OP_ALERTS_MIN_SUCCESS_15M = float(os.getenv("OP_ALERTS_MIN_SUCCESS_15M", "0.8"))  # fraction
    OP_ALERTS_INVARIANT_TOL_SATS = int(os.getenv("OP_ALERTS_INVARIANT_TOL_SATS", "0"))

    # Twitter OAuth2
    TWITTER_CLIENT_ID = os.getenv("TWITTER_CLIENT_ID", "")
    TWITTER_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")

