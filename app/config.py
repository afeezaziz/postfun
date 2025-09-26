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

    # CSRF (Flask-SeaSurf uses cookie 'csrf_token' by default)
    CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
