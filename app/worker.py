import os
import time
from datetime import datetime
from decimal import Decimal

from app import create_app
from app.extensions import db
from app.models import Token


app = create_app()


def heartbeat_job():
    app.logger.info("[worker] heartbeat at %s", datetime.utcnow().isoformat() + "Z")


def sample_price_drift_job():
    """Demo job: nudge token prices slightly to simulate movement."""
    with app.app_context():
        tokens = Token.query.all()
        if not tokens:
            return
        for t in tokens:
            try:
                # drift by +/- 0.1%
                if t.price is not None:
                    t.price = t.price * Decimal("1.001")
            except Exception as e:
                app.logger.warning("Price drift error for %s: %s", t.symbol, e)
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning("Commit error: %s", e)


if __name__ == "__main__":
    interval = int(os.getenv("WORKER_INTERVAL_SECONDS", "15"))
    while True:
        try:
            heartbeat_job()
            sample_price_drift_job()
        except Exception as e:
            app.logger.error("Worker loop error: %s", e)
        time.sleep(interval)
