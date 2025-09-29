import os
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app import create_app
from app.services.market_data import refresh_all_tokens
from app.services.alerts import evaluate_alerts
from app.services.lightning import LNBitsClient
from app.services.wallet import WalletService
from app.extensions import db
from app.models import LightningInvoice


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


def check_lightning_payments_job():
    """Check for pending lightning invoice payments and update their status."""
    with app.app_context():
        try:
            # Get pending invoices that are not expired
            pending_invoices = LightningInvoice.query.filter(
                LightningInvoice.status == "pending",
                LightningInvoice.expires_at > datetime.utcnow(),
                LightningInvoice.credited == False
            ).all()

            if not pending_invoices:
                return

            client = LNBitsClient()
            updated_count = 0

            for invoice in pending_invoices:
                try:
                    # Check payment status with LNBits
                    ok, result = client.get_payment_status(invoice.payment_hash)

                    if ok and result.get("paid"):
                        # Update invoice status
                        invoice.status = "paid"
                        invoice.paid_at = datetime.utcnow()
                        invoice.credited = True

                        # Credit user's BTC balance
                        success, message = WalletService.credit_lightning_invoice(invoice.id)
                        if success:
                            updated_count += 1
                            app.logger.info(f"[worker] Credited invoice {invoice.id[:8]} as paid ({invoice.amount_sats} sats)")
                        else:
                            app.logger.error(f"[worker] Failed to credit invoice {invoice.id[:8]}: {message}")

                    elif ok and result.get("details", {}).get("status") == "expired":
                        # Mark expired invoices
                        invoice.status = "expired"
                        app.logger.info(f"[worker] Marked invoice {invoice.id[:8]} as expired")

                except Exception as e:
                    app.logger.error(f"[worker] Error checking invoice {invoice.id[:8]}: {str(e)}")
                    continue

            if updated_count > 0:
                db.session.commit()
                app.logger.info(f"[worker] Updated {updated_count} lightning invoices to paid status")

        except Exception as e:
            app.logger.error(f"[worker] Error in lightning payment check: {str(e)}")
            db.session.rollback()


if __name__ == "__main__":
    hb_interval = int(os.getenv("WORKER_INTERVAL_SECONDS", "30"))
    refresh_interval = int(os.getenv("MARKET_REFRESH_SECONDS", "30"))
    lightning_check_interval = int(os.getenv("LIGHTNING_CHECK_INTERVAL_SECONDS", "30"))
    scheduler = BackgroundScheduler()
    scheduler.add_job(heartbeat_job, "interval", seconds=hb_interval, id="heartbeat")
    scheduler.add_job(refresh_prices_job, "interval", seconds=refresh_interval, id="refresh_prices")
    scheduler.add_job(evaluate_alerts_job, "interval", seconds=int(os.getenv("ALERT_EVAL_SECONDS", "60")), id="evaluate_alerts")
    scheduler.add_job(check_lightning_payments_job, "interval", seconds=lightning_check_interval, id="check_lightning_payments")
    scheduler.start()
    app.logger.info("[worker] scheduler started (hb=%ss, refresh=%ss, lightning=%ss)", hb_interval, refresh_interval, lightning_check_interval)
    try:
        while True:
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        app.logger.info("[worker] scheduler stopped")
