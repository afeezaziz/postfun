from flask import Blueprint

# Import modular blueprints
from .main import main_bp
from .tokens import tokens_bp
from .users import users_bp
from .trading import trading_bp
from .api import api_bp

# Main blueprint for organizing sub-blueprints
web_bp = Blueprint("web", __name__)

# Register all blueprints with the main web blueprint
web_bp.register_blueprint(main_bp)
web_bp.register_blueprint(tokens_bp, url_prefix='/tokens')
web_bp.register_blueprint(users_bp, url_prefix='/users')
web_bp.register_blueprint(trading_bp, url_prefix='/trading')
web_bp.register_blueprint(api_bp, url_prefix='/api')