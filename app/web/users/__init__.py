from flask import Blueprint

# Create blueprint for user-related routes
users_bp = Blueprint("users", __name__)

# Import routes at the end to avoid circular imports
from . import routes