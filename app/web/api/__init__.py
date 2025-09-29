from flask import Blueprint

# Create blueprint for API routes
api_bp = Blueprint("api", __name__)

# Import routes at the end to avoid circular imports
from . import routes