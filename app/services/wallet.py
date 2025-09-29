"""
Wallet service for managing sats balance and BTC token integration
"""

from decimal import Decimal, getcontext
from typing import Optional, Tuple
from datetime import datetime
from sqlalchemy import func

from ..extensions import db
from ..models import (
    User, Token, TokenBalance, LightningInvoice, LightningWithdrawal, LedgerEntry
)

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
        """Calculate user's available sats balance"""
        # Sum of paid invoices (deposits)
        total_deposits = db.session.query(
            func.sum(LightningInvoice.amount_sats)
        ).filter(
            LightningInvoice.user_id == user_id,
            LightningInvoice.status == 'paid',
            LightningInvoice.credited == True
        ).scalar() or 0

        # Sum of confirmed withdrawals (sent payments)
        total_withdrawals = db.session.query(
            func.sum(LightningWithdrawal.amount_sats)
        ).filter(
            LightningWithdrawal.user_id == user_id,
            LightningWithdrawal.status == 'confirmed'
        ).scalar() or 0

        return int(total_deposits - total_withdrawals)

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

        sats_balance = WalletService.get_user_sats_balance(user_id)
        btc_balance = WalletService.get_user_btc_token_balance(user_id)

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
            invoice = LightningInvoice.query.get(invoice_id)
            if not invoice:
                return False, "Invoice not found"

            if invoice.status != 'paid' or invoice.credited:
                return False, "Invoice not ready for crediting"

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
    def get_wallet_summary(user_id: int) -> dict:
        """Get complete wallet summary for a user"""
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