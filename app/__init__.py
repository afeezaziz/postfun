import os
from datetime import timedelta
from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from .config import Config
from .extensions import db, csrf, limiter, cache, migrate
from .auth import auth_bp
from . import models  # ensure models are imported for db.create_all()
from .web import web_bp
from .admin import admin_bp
from .api import api_bp
from flask_wtf.csrf import generate_csrf
from .services.reconcile import start_scheduler
from .services.metrics import record_response


def create_app(config_class: type = Config) -> Flask:
    # Load environment variables from .env if present
    load_dotenv()
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Flask-Limiter 3.x: configure default limits via config
    app.config.setdefault("RATELIMIT_DEFAULT", app.config.get("RATE_LIMIT_DEFAULT", "100 per hour"))

    # CORS
    CORS(
        app,
        resources={r"/*": {"origins": app.config.get("CORS_ORIGINS", "*")}},
        supports_credentials=True,
    )

    # Extensions
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    migrate.init_app(app, db)
    # Cache: Redis if available, else SimpleCache
    cache_config = {}
    redis_url = os.getenv("REDIS_URL") or os.getenv("CACHE_REDIS_URL")
    if redis_url:
        cache_config.update({
            "CACHE_TYPE": "RedisCache",
            "CACHE_REDIS_URL": redis_url,
        })
    else:
        cache_config.update({
            "CACHE_TYPE": os.getenv("CACHE_TYPE", "SimpleCache"),
        })
    default_timeout = int(os.getenv("CACHE_DEFAULT_TIMEOUT", str(60)))
    cache_config["CACHE_DEFAULT_TIMEOUT"] = default_timeout
    app.config.update(cache_config)
    cache.init_app(app)

    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    # Template helper: csrf_token() for forms
    try:
        app.jinja_env.globals["csrf_token"] = generate_csrf
    except Exception:
        pass

    @app.route("/health", methods=["GET"])  # simple health check
    def health():
        return jsonify({"status": "ok"})

    @app.route("/favicon.ico")  # handle favicon requests
    def favicon():
        return "", 204

    # Ensure a JS-readable CSRF cookie is present for client-side requests
    # This emulates Flask-SeaSurf's behavior so existing frontend code continues to work.
    @app.after_request
    def set_csrf_cookie(response):
        try:
            token = generate_csrf()
            cookie_name = app.config.get("CSRF_COOKIE_NAME", "csrf_token")
            secure = os.getenv("JWT_COOKIE_SECURE", "0") in ("1", "true", "True")
            # 7-day expiry for convenience; token is also session-bound internally
            response.set_cookie(
                cookie_name,
                token,
                max_age=7 * 24 * 3600,
                httponly=False,
                samesite="Lax",
                secure=secure,
                path="/",
            )
        except Exception as e:
            app.logger.debug(f"CSRF cookie set skipped: {e}")
        return response

    # Capture simple request metrics (per-process)
    @app.after_request
    def capture_metrics(response):
        try:
            record_response(getattr(response, "status_code", 200))
        except Exception:
            pass
        return response

    # Auto-create tables in dev (SQLite fallback) to keep onboarding simple
    with app.app_context():
        try:
            db.create_all()
            # Seed tokens if none exist
            try:
                from .models import Token
                if Token.query.count() == 0:
                    from decimal import Decimal
                    samples = [
                        Token(symbol="gBTC", name="Gateway BTC", price=Decimal("65000.00"), market_cap=Decimal("100000000.00"), change_24h=Decimal("1.23")),
                        Token(symbol="gUSD", name="Gateway USD", price=Decimal("1.00"), market_cap=Decimal("5000000.00"), change_24h=Decimal("0.02")),
                        Token(symbol="LP-gBTC-gUSD", name="LP gBTC/gUSD", price=Decimal("10.00"), market_cap=Decimal("250000.00"), change_24h=Decimal("-0.50")),
                        Token(symbol="PFUN", name="Postfun", price=Decimal("0.10"), market_cap=Decimal("1000000.00"), change_24h=Decimal("5.00")),
                    ]
                    db.session.add_all(samples)
                    db.session.commit()
            except Exception as seed_err:
                app.logger.warning(f"Token seed skipped: {seed_err}")
        except Exception as e:
            # On MariaDB without schema privileges, skip auto-create
            app.logger.warning(f"DB create_all skipped: {e}")

    # Start background reconciliation scheduler (if enabled)
    try:
        start_scheduler(app)
    except Exception as e:
        app.logger.warning(f"Scheduler start failed: {e}")

    return app
