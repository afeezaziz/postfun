import os
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app import create_app
from app.services.market_data import refresh_all_tokens


app = create_app()


def heartbeat_job():
    app.logger.info("[worker] heartbeat at %s", datetime.utcnow().isoformat() + "Z")


def refresh_prices_job():
    with app.app_context():
        n = refresh_all_tokens()
        app.logger.info("[worker] refreshed prices for %d tokens", n)


if __name__ == "__main__":
    hb_interval = int(os.getenv("WORKER_INTERVAL_SECONDS", "30"))
    refresh_interval = int(os.getenv("MARKET_REFRESH_SECONDS", "30"))
    scheduler = BackgroundScheduler()
    scheduler.add_job(heartbeat_job, "interval", seconds=hb_interval, id="heartbeat")
    scheduler.add_job(refresh_prices_job, "interval", seconds=refresh_interval, id="refresh_prices")
    scheduler.start()
    app.logger.info("[worker] scheduler started (hb=%ss, refresh=%ss)", hb_interval, refresh_interval)
    try:
        while True:
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        app.logger.info("[worker] scheduler stopped")
