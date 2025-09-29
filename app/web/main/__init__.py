from flask import Blueprint

# Create blueprint for main routes (home page, etc.)
main_bp = Blueprint("main", __name__)

# Import routes at the end to avoid circular imports
from . import routes