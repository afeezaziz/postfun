from flask import Blueprint

# Create blueprint for reward routes
reward_bp = Blueprint("reward", __name__)

# Import routes at the end to avoid circular imports
from . import routes