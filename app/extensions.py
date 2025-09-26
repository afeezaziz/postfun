from flask_sqlalchemy import SQLAlchemy
from flask_seasurf import SeaSurf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache

# Global extensions

db = SQLAlchemy()
csrf = SeaSurf()
limiter = Limiter(key_func=get_remote_address)
cache = Cache()
