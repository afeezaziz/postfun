from flask import Blueprint

# Create blueprint for trading-related routes
trading_bp = Blueprint("trading", __name__)

# Import routes at the end to avoid circular imports
from . import routes