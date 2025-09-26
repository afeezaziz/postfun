import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

import jwt
from flask import current_app, request, jsonify, g


def create_jwt(payload: Dict[str, Any], expires_in: Optional[int] = None) -> str:
    secret = current_app.config["JWT_SECRET"]
    algo = current_app.config.get("JWT_ALGORITHM", "HS256")
    now = int(time.time())
    exp = now + int(expires_in or current_app.config.get("JWT_EXPIRES_DELTA", 24 * 3600))
    data = {
        **payload,
        "iat": now,
        "exp": exp,
        "iss": "postfun",
    }
    return jwt.encode(data, secret, algorithm=algo)


def verify_jwt(token: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    secret = current_app.config["JWT_SECRET"]
    algo = current_app.config.get("JWT_ALGORITHM", "HS256")
    try:
        payload = jwt.decode(token, secret, algorithms=[algo])
        return True, payload
    except Exception:
        return False, None


def require_auth(f: Callable) -> Callable:
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing_bearer_token"}), 401
        token = auth_header.split(" ", 1)[1]
        ok, payload = verify_jwt(token)
        if not ok or not payload:
            return jsonify({"error": "invalid_or_expired_token"}), 401
        g.jwt_payload = payload
        return f(*args, **kwargs)

    return wrapper
