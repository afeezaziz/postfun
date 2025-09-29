from flask import Blueprint

# Create blueprint for token-related routes
tokens_bp = Blueprint("tokens", __name__)

# Import routes at the end to avoid circular imports
from . import routes