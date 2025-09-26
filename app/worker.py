import os
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app import create_app
from app.services.market_data import refresh_all_tokens
from app.services.alerts import evaluate_alerts


app = create_app()


def heartbeat_job():
    app.logger.info("[worker] heartbeat at %s", datetime.utcnow().isoformat() + "Z")


def refresh_prices_job():
    with app.app_context():
        n = refresh_all_tokens()
        app.logger.info("[worker] refreshed prices for %d tokens", n)


def evaluate_alerts_job():
    with app.app_context():
        n = evaluate_alerts()
        if n:
            app.logger.info("[worker] created %d alert events", n)


if __name__ == "__main__":
    hb_interval = int(os.getenv("WORKER_INTERVAL_SECONDS", "30"))
    refresh_interval = int(os.getenv("MARKET_REFRESH_SECONDS", "30"))
    scheduler = BackgroundScheduler()
    scheduler.add_job(heartbeat_job, "interval", seconds=hb_interval, id="heartbeat")
    scheduler.add_job(refresh_prices_job, "interval", seconds=refresh_interval, id="refresh_prices")
    scheduler.add_job(evaluate_alerts_job, "interval", seconds=int(os.getenv("ALERT_EVAL_SECONDS", "60")), id="evaluate_alerts")
    scheduler.start()
    app.logger.info("[worker] scheduler started (hb=%ss, refresh=%ss)", hb_interval, refresh_interval)
    try:
        while True:
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        app.logger.info("[worker] scheduler stopped")
