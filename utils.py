"""
utils.py — Shared utilities for authentication, authorization, validation, and caching.
"""

import json
import logging
import os
from functools import wraps
from flask import session, jsonify, request

log = logging.getLogger("SkillRadar")

# ── Response Format Helpers ──────────────────────────────────────────────────

def success_response(data=None, message="Success"):
    """Standardized success response."""
    return jsonify({
        "success": True,
        "data": data,
        "error": None,
        "message": message
    })


def error_response(error, status_code=400):
    """Standardized error response."""
    return jsonify({
        "success": False,
        "error": error
    }), status_code


# ── Authentication Decorators ────────────────────────────────────────────────

def require_auth(allowed_roles=None):
    """Decorator to require authentication and optionally check role."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                log.warning("Unauthorized access attempt to %s from IP=%s",
                            request.path, request.remote_addr)
                return error_response("Unauthorized", 401)

            if allowed_roles and session.get('role') not in allowed_roles:
                log.warning(
                    "Access denied: user_id=%s role=%s tried %s",
                    session.get('user_id'), session.get('role'), request.path
                )
                return error_response("Access denied for your role", 403)

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_student(f):
    """Require authenticated student."""
    return require_auth(allowed_roles=['student'])(f)


def require_faculty(f):
    """Require authenticated faculty (teacher or faculty role)."""
    return require_auth(allowed_roles={'faculty', 'teacher'})(f)


def require_authenticated(f):
    """Require authenticated user (any role)."""
    return require_auth()(f)


# ── Input Validation ─────────────────────────────────────────────────────────

def validate_json(*required_fields):
    """Decorator to validate JSON request data."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                return error_response("Request must be JSON", 400)
            try:
                data = request.get_json(silent=True)
            except Exception as e:
                return error_response(f"Invalid JSON: {str(e)}", 400)

            if data is None:
                return error_response("Empty or malformed request body", 400)

            for field in required_fields:
                if field not in data:
                    return error_response(f"Missing required field: {field}", 400)
                value = data[field]
                if isinstance(value, str) and not value.strip():
                    return error_response(f"Field '{field}' cannot be empty", 400)
                if value is None:
                    return error_response(f"Field '{field}' cannot be null", 400)

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def validate_field(field_name, field_type, min_length=None, max_length=None):
    """
    Validate a single field from the current JSON request body.

    Returns (value, None) on success, (None, error_message) on failure.
    Relies on request context being active.
    """
    data = request.get_json(silent=True) or {}
    value = data.get(field_name)

    if value is None:
        return None, f"Missing field: {field_name}"

    if not isinstance(value, field_type):
        return None, f"Field '{field_name}' must be {field_type.__name__}"

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None, f"Field '{field_name}' cannot be empty"
        if min_length is not None and len(value) < min_length:
            return None, f"Field '{field_name}' must be at least {min_length} characters"
        if max_length is not None and len(value) > max_length:
            return None, f"Field '{field_name}' must be at most {max_length} characters"

    if isinstance(value, (list, dict)) and min_length is not None:
        if len(value) < min_length:
            return None, f"Field '{field_name}' must have at least {min_length} items"

    return value, None


# ── Authorization Helpers ────────────────────────────────────────────────────

def check_ownership(resource_owner_id, current_user_id=None):
    """Check if current user owns the resource."""
    user_id = current_user_id or session.get('user_id')
    if resource_owner_id != user_id:
        log.warning(
            "Authorization failed: user_id=%s tried resource owned by %s",
            user_id, resource_owner_id
        )
        return False
    return True


def check_project_ownership(db, project_id, faculty_id):
    """Check if faculty owns the project."""
    project = db.execute(
        "SELECT id, faculty_id, required_skills, title FROM projects WHERE id = ?",
        (project_id,)
    ).fetchone()

    if not project:
        return None, "Project not found"

    if project['faculty_id'] != faculty_id:
        log.warning(
            "Authorization failed: faculty_id=%s tried project %s owned by %s",
            faculty_id, project_id, project['faculty_id']
        )
        return None, "Access denied"

    return project, None


# ── Caching Utilities ────────────────────────────────────────────────────────

class _InMemoryLRU:
    """Tiny LRU cache used when Redis is unavailable."""

    def __init__(self, max_size=1000):
        self._cache: dict = {}
        self._order: list = []
        self._max = max_size

    def get(self, key):
        if key in self._cache:
            self._order.remove(key)
            self._order.append(key)
            return self._cache[key]
        return None

    def set(self, key, value):
        if key in self._cache:
            self._order.remove(key)
        elif len(self._cache) >= self._max:
            evict = self._order.pop(0)
            del self._cache[evict]
        self._cache[key] = value
        self._order.append(key)

    def clear(self):
        self._cache.clear()
        self._order.clear()


class RedisCache:
    """Redis-backed cache with transparent in-memory LRU fallback."""

    def __init__(self, host='localhost', port=6379, db=0, password=None,
                 max_memory='256mb', max_size=1000):
        self._redis = None
        self._fallback = _InMemoryLRU(max_size)
        try:
            import redis as redis_lib
            client = redis_lib.Redis(
                host=host, port=port, db=db,
                password=password,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            client.ping()
            # Best-effort memory policy config (may fail on managed Redis)
            try:
                client.config_set('maxmemory', max_memory)
                client.config_set('maxmemory-policy', 'allkeys-lru')
            except Exception:
                pass
            self._redis = client
            log.info("RedisCache connected on %s:%s db=%s", host, port, db)
        except Exception as e:
            log.warning("RedisCache: Redis unavailable (%s), using in-memory LRU", e)

    def get(self, key):
        if self._redis:
            try:
                value = self._redis.get(key)
                return json.loads(value) if value else None
            except Exception as e:
                log.warning("RedisCache.get error: %s", e)
        return self._fallback.get(key)

    def set(self, key, value, ttl=3600):
        """Cache value. ttl defaults to 1 hour."""
        if self._redis:
            try:
                self._redis.set(key, json.dumps(value), ex=ttl)
                return
            except Exception as e:
                log.warning("RedisCache.set error: %s", e)
        self._fallback.set(key, value)

    def clear(self):
        if self._redis:
            try:
                self._redis.flushdb()
            except Exception as e:
                log.warning("RedisCache.clear error: %s", e)
        self._fallback.clear()


# ── Global cache instances ────────────────────────────────────────────────────
_redis_host = os.getenv('REDIS_HOST', 'localhost')
_redis_port = int(os.getenv('REDIS_PORT', 6379))
_redis_password = os.getenv('REDIS_PASSWORD')

embedding_cache = RedisCache(
    host=_redis_host, port=_redis_port, password=_redis_password, db=0
)
similarity_cache = RedisCache(
    host=_redis_host, port=_redis_port, password=_redis_password, db=1
)


def get_cached_similarity(query: str, corpus_key: str):
    """Return cached similarity scores or None."""
    return similarity_cache.get(f"{query}|{corpus_key}")


def set_cached_similarity(query: str, corpus_key: str, scores, ttl: int = 3600):
    """Cache similarity scores with a default 1-hour TTL."""
    similarity_cache.set(f"{query}|{corpus_key}", scores, ttl=ttl)


# ── Data Sanitization ────────────────────────────────────────────────────────

def sanitize_string(value, max_length=500):
    """Sanitize string input — strip whitespace and hard-cap length."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:max_length] if len(value) > max_length else value


def sanitize_list(value, max_items=100):
    """Sanitize list input — ensure it's a list and cap its length."""
    if not isinstance(value, list):
        return []
    return value[:max_items]


# ── Logging Helpers ──────────────────────────────────────────────────────────

def log_auth_event(user_id, action, status="success", details=None):
    log.info(
        "[AUTH_EVENT] user_id=%s action=%s status=%s details=%s",
        user_id, action, status, details or ""
    )


def log_api_action(user_id, endpoint, method, status="success", details=None):
    log.info(
        "[API_ACTION] user_id=%s endpoint=%s method=%s status=%s details=%s",
        user_id, endpoint, method, status, details or ""
    )
