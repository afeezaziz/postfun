"""
Wallet service for managing sats balance and BTC token integration
"""

from decimal import Decimal, getcontext
from typing import Optional, Tuple, Dict
from datetime import datetime, timedelta
from sqlalchemy import func
import logging

from ..extensions import db
from ..models import (
    User, Token, TokenBalance, LightningInvoice, LightningWithdrawal, LedgerEntry
)

print("[DEBUG] Wallet service module loaded at", datetime.utcnow())
print("[DEBUG] EDITED VERSION - this should appear!")

getcontext().prec = 18  # High precision for BTC calculations

# Constants
SATS_PER_BTC = 100_000_000
BTC_TOKEN_SYMBOL = 'BTC'


class WalletService:
    """Service for managing wallet balances and BTC integration"""

    @staticmethod
    def get_btc_token() -> Optional[Token]:
        """Get the BTC token from database"""
        return Token.query.filter_by(symbol=BTC_TOKEN_SYMBOL).first()

    @staticmethod
    def get_user_sats_balance(user_id: int) -> int:
        """Get user's sats balance from User table (converts millisats to sats)"""
        user = db.session.get(User, user_id)
        if not user:
            return 0

        # Convert millisats to sats
        balance_sats = int(user.sats // 1000)

        
        return balance_sats

    @staticmethod
    def get_user_btc_token_balance(user_id: int) -> Decimal:
        """Get user's BTC token balance (converts from sats)"""
        sats_balance = WalletService.get_user_sats_balance(user_id)
        btc_balance = Decimal(sats_balance) / Decimal(SATS_PER_BTC)
        return btc_balance.quantize(Decimal('0.00000001'))

    @staticmethod
    def update_user_btc_token_balance(user_id: int) -> Tuple[bool, str]:
        """Update user's BTC token balance to match their sats balance"""
        btc_token = WalletService.get_btc_token()
        if not btc_token:
            return False, "BTC token not found"

        user = db.session.get(User, user_id)
        if not user:
            return False, "User not found"

        # Convert millisats to BTC
        btc_balance = Decimal(user.sats) / Decimal(100_000_000_000)  # millisats to BTC

        # Get or create BTC token balance
        token_balance = TokenBalance.query.filter_by(
            user_id=user_id,
            token_id=btc_token.id
        ).first()

        if not token_balance:
            token_balance = TokenBalance(
                user_id=user_id,
                token_id=btc_token.id,
                amount=btc_balance
            )
            db.session.add(token_balance)
        else:
            token_balance.amount = btc_balance

        db.session.commit()
        return True, f"Updated BTC balance to {btc_balance} BTC"

    @staticmethod
    def can_afford_sats(user_id: int, amount_sats: int) -> bool:
        """Check if user has enough sats for a transaction"""
        available_balance = WalletService.get_user_sats_balance(user_id)
        return available_balance >= amount_sats

    @staticmethod
    def reserve_sats_for_trade(user_id: int, amount_sats: int) -> Tuple[bool, str]:
        """Reserve sats for an upcoming trade"""
        if not WalletService.can_afford_sats(user_id, amount_sats):
            return False, "Insufficient sats balance"

        # For now, we just check balance. In a real system, you'd track reserved amounts
        return True, "Sats reserved for trade"

    @staticmethod
    def execute_sats_to_token_trade(user_id: int, amount_sats: int, token_out_id: int) -> Tuple[bool, str]:
        """Execute trade from sats to another token"""
        # This would integrate with the AMM service
        # For now, just update the balance
        return True, f"Trade executed: {amount_sats} sats to token {token_out_id}"

    @staticmethod
    def execute_token_to_sats_trade(user_id: int, token_in_id: int, amount_tokens: Decimal) -> Tuple[bool, str]:
        """Execute trade from token to sats"""
        # This would integrate with the AMM service
        # For now, just return success
        return True, f"Trade executed: {amount_tokens} tokens to sats"

    @staticmethod
    def credit_lightning_invoice(invoice_id: str) -> Tuple[bool, str]:
        """Credit user's balance when a lightning invoice is paid"""
        try:
            print(f"[DEBUG] credit_lightning_invoice called with invoice_id: {invoice_id}")
            invoice = LightningInvoice.query.get(invoice_id)
            if not invoice:
                print(f"[DEBUG] Invoice {invoice_id} not found!")
                return False, "Invoice not found"

            print(f"[DEBUG] Found invoice {invoice_id}: status={invoice.status}, credited={invoice.credited}, amount={invoice.amount_sats}")

            if invoice.credited:
                print(f"[DEBUG] Invoice {invoice_id} already credited!")
                return False, "Invoice already credited"

            if invoice.status != 'paid':
                print(f"[DEBUG] Invoice {invoice_id} not marked as paid! status={invoice.status}")
                return False, "Invoice not marked as paid"

            # Add to user's sats balance (convert sats to millisats)
            user = db.session.get(User, invoice.user_id)
            if user:
                old_balance = user.sats
                user.sats += int(invoice.amount_sats) * 1000
                new_balance = user.sats
                print(f"[DEBUG] Crediting invoice {invoice.id}: User {invoice.user_id} sats {old_balance} -> {new_balance} (+{int(invoice.amount_sats) * 1000} millisats)")
                db.session.add(user)
            else:
                print(f"[DEBUG] ERROR: User {invoice.user_id} not found for invoice {invoice.id}")
                return False, "User not found"

            # Update BTC token balance
            success, message = WalletService.update_user_btc_token_balance(invoice.user_id)
            if not success:
                return False, f"Failed to update BTC balance: {message}"

            # Create ledger entry
            ledger_entry = LedgerEntry(
                user_id=invoice.user_id,
                entry_type='deposit',
                delta_sats=invoice.amount_sats,
                ref_type='invoice',
                ref_id=invoice.id,
                meta=f'Lightning deposit: {invoice.amount_sats} sats'
            )
            db.session.add(ledger_entry)

            # Mark invoice as credited
            invoice.credited = True
            db.session.add(invoice)

            db.session.commit()

            return True, f"Credited {invoice.amount_sats} sats to user {invoice.user_id}"

        except Exception as e:
            db.session.rollback()
            return False, f"Error crediting invoice: {str(e)}"

    @staticmethod
    def debit_lightning_withdrawal(withdrawal_id: str) -> Tuple[bool, str]:
        """Debit user's balance when a lightning withdrawal is confirmed"""
        try:
            withdrawal = LightningWithdrawal.query.get(withdrawal_id)
            if not withdrawal:
                return False, "Withdrawal not found"

            if withdrawal.status != 'confirmed':
                return False, "Withdrawal not confirmed"

            # Subtract from user's sats balance (convert sats to millisats)
            user = db.session.get(User, withdrawal.user_id)
            if user:
                old_balance = user.sats
                user.sats -= int(withdrawal.amount_sats) * 1000
                new_balance = user.sats
                print(f"[DEBUG] Debiting withdrawal {withdrawal.id}: User {withdrawal.user_id} sats {old_balance} -> {new_balance} (-{int(withdrawal.amount_sats) * 1000} millisats)")
                db.session.add(user)
            else:
                print(f"[DEBUG] ERROR: User {withdrawal.user_id} not found for withdrawal {withdrawal.id}")
                return False, "User not found"

            # Update BTC token balance
            success, message = WalletService.update_user_btc_token_balance(withdrawal.user_id)
            if not success:
                return False, f"Failed to update BTC balance: {message}"

            # Create ledger entry
            ledger_entry = LedgerEntry(
                user_id=withdrawal.user_id,
                entry_type='withdrawal',
                delta_sats=-withdrawal.amount_sats,
                ref_type='withdrawal',
                ref_id=withdrawal.id,
                meta=f'Lightning withdrawal: {withdrawal.amount_sats} sats'
            )
            db.session.add(ledger_entry)
            db.session.commit()

            return True, f"Debited {withdrawal.amount_sats} sats from user {withdrawal.user_id}"

        except Exception as e:
            db.session.rollback()
            return False, f"Error debiting withdrawal: {str(e)}"

    @staticmethod
    def check_pending_invoice_status(invoice: LightningInvoice) -> Tuple[bool, str]:
        """Check the status of a pending lightning invoice"""
        try:
            print(f"[DEBUG] check_pending_invoice_status called for invoice {invoice.id}, status={invoice.status}, amount={invoice.amount_sats}")
            if invoice.status != 'pending':
                print(f"[DEBUG] Invoice {invoice.id} is not pending, status={invoice.status}")
                return False, "Invoice is not pending"

            # Check if invoice has expired (24 hours default)
            if hasattr(invoice, 'expires_at') and invoice.expires_at:
                if datetime.utcnow() > invoice.expires_at:
                    invoice.status = 'expired'
                    db.session.commit()
                    return True, "Invoice expired"
            elif invoice.created_at and (datetime.utcnow() - invoice.created_at) > timedelta(hours=24):
                invoice.status = 'expired'
                db.session.commit()
                return True, "Invoice expired"

            # Check with lightning node to see if invoice has been paid
            if invoice.payment_hash:
                try:
                    from .lightning import LNBitsClient
                    client = LNBitsClient()
                    print(f"[DEBUG] Checking payment status for invoice {invoice.id} with payment_hash {invoice.payment_hash}")
                    success, payment_data = client.get_payment_status(invoice.payment_hash)
                    print(f"[DEBUG] LNBits response for invoice {invoice.id}: success={success}, payment_data={payment_data}")

                    if success and payment_data:
                        # Check if payment is paid
                        if payment_data.get('paid') == True:
                            print(f"[DEBUG] Invoice {invoice.id} is paid! Processing credit...")
                            # Mark as paid but don't set credited yet
                            invoice.status = 'paid'
                            db.session.commit()

                            # Credit the user's balance (this will set credited=True)
                            credit_success, credit_message = WalletService.credit_lightning_invoice(invoice.id)
                            if credit_success:
                                print(f"[DEBUG] Invoice {invoice.id} credited successfully: {credit_message}")
                                return True, f"Invoice paid and credited: {credit_message}"
                            else:
                                print(f"[DEBUG] Invoice {invoice.id} credit failed: {credit_message}")
                                return True, f"Invoice paid but credit failed: {credit_message}"

                        # Check if payment is still pending
                        elif payment_data.get('details', {}).get('status') == 'open':
                            print(f"[DEBUG] Invoice {invoice.id} still open with lightning node")
                            return False, "Invoice still pending with lightning node"

                        # Other statuses
                        else:
                            status = payment_data.get('details', {}).get('status', 'unknown')
                            print(f"[DEBUG] Invoice {invoice.id} has status: {status}")
                            return True, f"Invoice status updated to: {status}"
                    else:
                        print(f"[DEBUG] LNBits check failed for invoice {invoice.id}: success={success}")

                except Exception as lightning_error:
                    print(f"[DEBUG] Lightning service error for invoice {invoice.id}: {str(lightning_error)}")
                    # Continue with basic time-based checking if lightning service fails

            # Basic fallback - just check if it's past reasonable time limits
            return False, "Invoice still pending"

        except Exception as e:
            return False, f"Error checking invoice status: {str(e)}"

    @staticmethod
    def check_pending_withdrawal_status(withdrawal: LightningWithdrawal) -> Tuple[bool, str]:
        """Check the status of a pending lightning withdrawal"""
        try:
            if withdrawal.status != 'pending':
                return False, "Withdrawal is not pending"

            # Check if withdrawal has expired (typically 1 hour for lightning payments)
            if hasattr(withdrawal, 'expires_at') and withdrawal.expires_at:
                if datetime.utcnow() > withdrawal.expires_at:
                    withdrawal.status = 'expired'
                    db.session.commit()
                    return True, "Withdrawal expired"
            elif withdrawal.created_at and (datetime.utcnow() - withdrawal.created_at) > timedelta(hours=2):
                withdrawal.status = 'expired'
                db.session.commit()
                return True, "Withdrawal expired"

            # Check with lightning node to see if withdrawal has been completed
            if withdrawal.payment_hash:
                try:
                    from .lightning import LNBitsClient
                    client = LNBitsClient()
                    success, payment_data = client.get_payment_status(withdrawal.payment_hash)

                    if success and payment_data:
                        # Check if payment is confirmed/complete
                        if payment_data.get('paid') == True:
                            # Mark as confirmed and debit the user's balance
                            withdrawal.status = 'confirmed'
                            withdrawal.processed_at = datetime.utcnow()
                            db.session.commit()

                            # Debit the user's balance
                            debit_success, debit_message = WalletService.debit_lightning_withdrawal(withdrawal.id)
                            if debit_success:
                                return True, f"Withdrawal confirmed and debited: {debit_message}"
                            else:
                                return True, f"Withdrawal confirmed but debit failed: {debit_message}"

                        # Check if payment is still pending
                        elif payment_data.get('details', {}).get('status') in ['open', 'pending']:
                            return False, "Withdrawal still pending with lightning node"

                        # Check if payment failed
                        elif payment_data.get('details', {}).get('status') == 'failed':
                            withdrawal.status = 'failed'
                            db.session.commit()
                            return True, "Withdrawal failed with lightning node"

                        # Other statuses
                        else:
                            status = payment_data.get('details', {}).get('status', 'unknown')
                            return True, f"Withdrawal status updated to: {status}"

                except Exception as lightning_error:
                    print(f"Lightning service error: {str(lightning_error)}")
                    # Continue with basic time-based checking if lightning service fails

            # Basic fallback - just check time limits
            return False, "Withdrawal still pending"

        except Exception as e:
            return False, f"Error checking withdrawal status: {str(e)}"

    @staticmethod
    def update_user_pending_transactions(user_id: int) -> Dict[str, int]:
        """Update status of all pending transactions for a user"""
        updated_count = {'invoices': 0, 'withdrawals': 0}

        try:
            logger = logging.getLogger(__name__)
            logger.warning(f"[DEBUG] update_user_pending_transactions START for user {user_id}")

            # First, check for paid but uncredited invoices (recovery case)
            logger.warning(f"[DEBUG] Querying for paid but uncredited invoices for user {user_id}")
            paid_uncredited_invoices = LightningInvoice.query.filter_by(
                user_id=user_id,
                status='paid',
                credited=False
            ).all()

            logger.warning(f"[DEBUG] Found {len(paid_uncredited_invoices)} paid but uncredited invoices for user {user_id}")
            for invoice in paid_uncredited_invoices:
                print(f"[DEBUG] Processing paid but uncredited invoice {invoice.id}")
                try:
                    credit_success, credit_message = WalletService.credit_lightning_invoice(invoice.id)
                    if credit_success:
                        print(f"[DEBUG] Successfully credited paid invoice {invoice.id}: {credit_message}")
                        updated_count['invoices'] += 1
                    else:
                        print(f"[DEBUG] Failed to credit paid invoice {invoice.id}: {credit_message}")
                except Exception as e:
                    print(f"[DEBUG] Exception processing invoice {invoice.id}: {str(e)}")
                    import traceback
                    print(f"[DEBUG] Traceback: {traceback.format_exc()}")

            # Check pending invoices
            pending_invoices = LightningInvoice.query.filter_by(
                user_id=user_id,
                status='pending'
            ).all()

            print(f"[DEBUG] Found {len(pending_invoices)} pending invoices for user {user_id}")
            for invoice in pending_invoices:
                print(f"[DEBUG] Checking invoice {invoice.id} - status: {invoice.status}, amount: {invoice.amount_sats}")
                updated, message = WalletService.check_pending_invoice_status(invoice)
                if updated:
                    print(f"[DEBUG] Invoice {invoice.id} updated: {message}")
                    updated_count['invoices'] += 1
                else:
                    print(f"[DEBUG] Invoice {invoice.id} not updated: {message}")

            # Check pending withdrawals
            pending_withdrawals = LightningWithdrawal.query.filter_by(
                user_id=user_id,
                status='pending'
            ).all()

            for withdrawal in pending_withdrawals:
                updated, message = WalletService.check_pending_withdrawal_status(withdrawal)
                if updated:
                    updated_count['withdrawals'] += 1

        except Exception as e:
            logger.error(f"Error updating pending transactions: {str(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

        logger.warning(f"[DEBUG] update_user_pending_transactions END for user {user_id}, returning: {updated_count}")
        # Add a test value to verify this function is being called
        updated_count['test'] = 'test_value'
        updated_count['edited'] = True
        print(f"[DEBUG] RETURNING: {updated_count}")
        return updated_count

    @staticmethod
    def process_lightning_webhook(payment_hash: str, status: str, amount_sats: Optional[int] = None) -> Tuple[bool, str]:
        """Process webhook notifications from lightning service for real-time updates"""
        try:
            # Check if this payment hash corresponds to an invoice
            invoice = LightningInvoice.query.filter_by(payment_hash=payment_hash).first()
            if invoice and invoice.status == 'pending':
                if status == 'paid' or status == 'complete':
                    # Mark invoice as paid but don't set credited yet
                    invoice.status = 'paid'
                    db.session.commit()

                    # Credit the user's balance (this will set credited=True)
                    credit_success, credit_message = WalletService.credit_lightning_invoice(invoice.id)
                    if credit_success:
                        return True, f"Invoice paid and credited via webhook: {credit_message}"
                    else:
                        return True, f"Invoice paid via webhook but credit failed: {credit_message}"

                elif status == 'expired':
                    invoice.status = 'expired'
                    db.session.commit()
                    return True, "Invoice expired via webhook"

                elif status == 'failed':
                    invoice.status = 'failed'
                    db.session.commit()
                    return True, "Invoice failed via webhook"

            # Check if this payment hash corresponds to a withdrawal
            withdrawal = LightningWithdrawal.query.filter_by(payment_hash=payment_hash).first()
            if withdrawal and withdrawal.status == 'pending':
                if status == 'paid' or status == 'complete':
                    # Mark withdrawal as confirmed
                    withdrawal.status = 'confirmed'
                    withdrawal.processed_at = datetime.utcnow()
                    if amount_sats:
                        withdrawal.amount_sats = amount_sats
                    db.session.commit()

                    # Debit the user's balance
                    debit_success, debit_message = WalletService.debit_lightning_withdrawal(withdrawal.id)
                    if debit_success:
                        return True, f"Withdrawal confirmed and debited via webhook: {debit_message}"
                    else:
                        return True, f"Withdrawal confirmed via webhook but debit failed: {debit_message}"

                elif status == 'expired':
                    withdrawal.status = 'expired'
                    db.session.commit()
                    return True, "Withdrawal expired via webhook"

                elif status == 'failed':
                    withdrawal.status = 'failed'
                    db.session.commit()
                    return True, "Withdrawal failed via webhook"

            return False, "No matching transaction found for webhook"

        except Exception as e:
            db.session.rollback()
            return False, f"Error processing webhook: {str(e)}"

    @staticmethod
    def get_wallet_summary(user_id: int) -> dict:
        """Get complete wallet summary for a user"""
        # Update pending transactions first
        WalletService.update_user_pending_transactions(user_id)

        sats_balance = WalletService.get_user_sats_balance(user_id)
        btc_balance = WalletService.get_user_btc_token_balance(user_id)
        btc_token = WalletService.get_btc_token()

        # Get recent activity
        recent_invoices = LightningInvoice.query.filter_by(
            user_id=user_id
        ).order_by(LightningInvoice.created_at.desc()).limit(5).all()

        recent_withdrawals = LightningWithdrawal.query.filter_by(
            user_id=user_id
        ).order_by(LightningWithdrawal.created_at.desc()).limit(5).all()

        return {
            'sats_balance': sats_balance,
            'btc_balance': float(btc_balance),
            'btc_token_id': btc_token.id if btc_token else None,
            'recent_invoices': [inv.to_dict() for inv in recent_invoices],
            'recent_withdrawals': [wd.to_dict() for wd in recent_withdrawals],
            'total_transactions': len(recent_invoices) + len(recent_withdrawals)
        }