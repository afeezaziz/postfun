from flask import Blueprint

# Create blueprint for creator routes
creator_bp = Blueprint("creator", __name__)

# Import routes at the end to avoid circular imports
from . import routes