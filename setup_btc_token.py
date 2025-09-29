#!/usr/bin/env python3
"""
Setup BTC token and integrate with wallet system
"""

import sys
sys.path.append('/Users/afeez/Projects/Postfun/website')

from decimal import Decimal
from app import create_app
from app.extensions import db
from app.models import Token, TokenInfo

def setup_btc_token():
    """Create BTC token if it doesn't exist"""
    app = create_app()

    with app.app_context():
        # Check if BTC token already exists
        btc_token = Token.query.filter_by(symbol='BTC').first()

        if btc_token:
            print(f"BTC token already exists: {btc_token.symbol} - {btc_token.name}")
            return btc_token

        # Create BTC token
        btc_token = Token(
            symbol='BTC',
            name='Bitcoin',
            price=Decimal('50000.00'),  # Will be updated by market data
            market_cap=None,
            change_24h=None,
            hidden=False,
            frozen=False
        )

        db.session.add(btc_token)
        db.session.flush()  # Get the ID

        # Create TokenInfo for BTC
        btc_info = TokenInfo(
            token_id=btc_token.id,
            description='Bitcoin - The original cryptocurrency',
            logo_url=None,
            website=None,
            twitter=None,
            telegram=None,
            discord=None,
            total_supply=Decimal('21000000'),  # 21 million BTC
            launch_user_id=None,
            launch_at=None,
            moderation_status='visible',
            moderation_notes=None,
            categories='bitcoin,cryptocurrency'
        )

        db.session.add(btc_info)
        db.session.commit()

        print(f"Created BTC token: {btc_token.symbol} (ID: {btc_token.id})")
        print(f"Created TokenInfo for BTC")

        return btc_token

if __name__ == "__main__":
    setup_btc_token()