from flask import Blueprint

# Create blueprint for tournament routes
tournament_bp = Blueprint("tournament", __name__)

# Import routes at the end to avoid circular imports
from . import routes