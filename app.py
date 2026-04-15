"""
app.py â€” SkillRadar Phase 2  (pipeline-connected refactor)

Pipeline order for every /analyze call:
  1. PDF text extraction          (pdfminer, inline)
  2. Semantic skill extraction    (analysis_engine.extract_skills_from_text)
  3. GitHub confidence enrichment (github_analysis.verify_github_skills  +
                                   analysis_engine.enrich_with_github)
  4. Weighted gap scoring         (skill_analysis.calculate_skill_gap_from_analysis)
  5. Ranked next-best actions     (improvement_engine.get_next_best_action +
                                   improvement_engine.rank_all_actions)
  6. Adaptive roadmap generation  (roadmap_generator.generate_roadmap_from_analysis)
  7. Progress snapshot            (progress_tracker.record_analysis_snapshot)
  8. Store final result in        DB (user_analysis) + minimal token in session

All Flask routes are preserved exactly.  No UI breakage.
"""

import os
import json
import logging
import sqlite3
import time
import bcrypt
from openai import OpenAI
from flask import Flask, render_template, request, session, redirect, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename
from database import init_db
from services import matching_engine

# â”€â”€ Service imports (full pipeline) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from services.pipeline import run_analysis_pipeline, slim_analysis_for_session
from services.analysis_engine import ROLES_CONFIG

# â”€â”€ Blueprint imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from routes.student_routes import student_bp
from routes.faculty_routes import faculty_bp
from routes.sdp_routes import sdp_bp

# â”€â”€ Utils import â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from utils import error_response

# â”€â”€ Logging setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import logging
from logging.handlers import RotatingFileHandler

os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        RotatingFileHandler(
            'logs/skillradar.log',
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5
        ),
        logging.StreamHandler()
    ]
)

log = logging.getLogger("SkillRadar")

# â”€â”€ Helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def safe_json_loads(data, default=None):
    """Safely parse JSON with fallback."""
    if not data:
        return default
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return default


# â”€â”€ Flask app â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = Flask(__name__)
secret = os.getenv("SECRET_KEY")
if not secret:
    raise RuntimeError("SECRET_KEY not set")
app.secret_key = secret

FLASK_ENV = os.getenv('FLASK_ENV', 'production')
IS_DEV = FLASK_ENV == 'development'

# â”€â”€ Session Security Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app.config['SESSION_COOKIE_SECURE'] = not IS_DEV
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Keep session data small â€” cap the cookie at ~3KB to avoid 4KB browser limit
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB file uploads

# â”€â”€ CSRF Protection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
csrf = CSRFProtect(app)

# â”€â”€ Rate Limiting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
redis_url = os.getenv("REDIS_URL")

storage_uri = "memory://"
if redis_url:
    try:
        import redis
        r = redis.Redis.from_url(redis_url, socket_connect_timeout=2)
        r.ping()
        storage_uri = redis_url
        log.info("Rate limiter connected to Redis")
    except Exception as e:
        log.warning(f"Redis not available ({e}), falling back to in-memory storage")

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    # In dev: no global limits.  In prod: generous but bounded.
    default_limits=[] if IS_DEV else ["3000 per day", "120 per minute"],
    storage_uri=storage_uri,
    enabled=not IS_DEV
)

# â”€â”€ Groq / OpenAI-compatible client â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_groq_api_key = os.getenv("GROQ_API_KEY")
if not _groq_api_key:
    raise ValueError("GROQ_API_KEY environment variable is not set.")

groq_client = OpenAI(
    api_key=_groq_api_key,
    base_url="https://api.groq.com/openai/v1",
)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

init_db()

# â”€â”€ Register blueprints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app.register_blueprint(student_bp)
app.register_blueprint(faculty_bp)
app.register_blueprint(sdp_bp)

# â”€â”€ Exempt API blueprints from CSRF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
csrf.exempt(student_bp)
csrf.exempt(faculty_bp)
csrf.exempt(sdp_bp)


# â”€â”€ Request / Response Hooks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.before_request
def log_request():
    log.info(
        "-> %s %s | IP=%s | user_id=%s",
        request.method, request.path,
        request.remote_addr,
        session.get("user_id", "anon")
    )


@app.after_request
def add_security_headers(response):
    response.headers['Content-Security-Policy'] = (
        "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "script-src * 'unsafe-inline' 'unsafe-eval';"
    )
    # Expose response size for monitoring
    try:
        content_length = response.calculate_content_length()
        if content_length:
            log.debug("<- %s %s | status=%s | size=%d",
                      request.method, request.path, response.status_code, content_length)
    except Exception:
        pass
    return response


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Session helpers â€” keep cookie lean
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Fields stored in the session cookie (must stay < ~3KB total)
_SESSION_FIELDS = {
    "role", "match_score", "matched_skills", "missing_skills",
    "goal_role", "user_level", "ranked_actions", "github_username",
    # Keep a short resume excerpt for role-switching without a re-upload
    "resume_text",
}
_RESUME_TEXT_MAX = 800   # chars kept in cookie


def _slim_for_session(analysis: dict) -> dict:
    """
    Return only the fields that belong in a session cookie.
    Delegates to the pipeline helper but enforces our own hard cap on
    resume_text so we never bloat the cookie.
    """
    try:
        slimmed = slim_analysis_for_session(analysis)
    except Exception:
        slimmed = {k: analysis[k] for k in _SESSION_FIELDS if k in analysis}

    # Hard-cap resume_text
    rt = slimmed.get("resume_text", "")
    if isinstance(rt, str) and len(rt) > _RESUME_TEXT_MAX:
        slimmed["resume_text"] = rt[:_RESUME_TEXT_MAX]

    # Ensure matched / missing skills are plain lists (not dicts) to save space
    for key in ("matched_skills", "missing_skills"):
        val = slimmed.get(key)
        if isinstance(val, dict):
            slimmed[key] = list(val.keys())
        elif not isinstance(val, list):
            slimmed[key] = []

    return slimmed


def _persist_analysis(user_id: int, analysis: dict) -> None:
    """Upsert the full analysis blob to the database (authoritative store)."""
    if not user_id:
        return
    try:
        db = get_db()
        full_json = json.dumps(analysis)
        rows_updated = db.execute(
            "UPDATE user_analysis SET analysis_data = ?, created_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (full_json, user_id)
        ).rowcount
        if rows_updated == 0:
            db.execute(
                "INSERT INTO user_analysis(user_id, analysis_data) VALUES(?, ?)",
                (user_id, full_json)
            )
        db.commit()
        log.info("[_persist_analysis] Saved analysis for user_id=%s (%d chars)", user_id, len(full_json))
    except Exception as e:
        log.error("[_persist_analysis] Failed for user_id=%s: %s", user_id, e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _load_analysis_from_db(user_id: int) -> dict | None:
    """Load the latest full analysis from DB for the given user."""
    if not user_id:
        return None
    try:
        db = get_db()
        row = db.execute(
            "SELECT analysis_data FROM user_analysis WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        db.close()
        if row and row["analysis_data"]:
            return json.loads(row["analysis_data"])
    except Exception as e:
        log.warning("[_load_analysis_from_db] user_id=%s: %s", user_id, e)
    return None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DB helper
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_db():
    from database import get_db as db_get_db
    return db_get_db()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Auth routes
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = (request.form.get("role", "student") or "student").strip().lower()

        # Basic server-side validation
        if not name or not email or not password:
            return render_template("register.html", error="All fields are required")
        if role not in ("student", "teacher", "faculty"):
            return render_template("register.html", error="Invalid role")
        if len(password) < 8:
            return render_template("register.html", error="Password must be at least 8 characters")

        db = get_db()
        try:
            hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
            db.execute(
                "INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)",
                (name, email, hashed_password.decode('utf-8'), role),
            )
            db.commit()
            log.info("User registered: %s (role: %s)", email, role)
            return redirect("/login")
        except Exception as exc:
            log.warning("Registration failed for %s: %s", email, exc)
            return render_template("register.html", error="Email already exists")
        finally:
            db.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("login.html", error="Email and password are required")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE LOWER(TRIM(email)) = ?",
            (email,)
        ).fetchone()
        db.close()

        if user:
            stored_password = user["password"]
            verified = False
            needs_rehash = False

            if stored_password.startswith('$2b$') or stored_password.startswith('$2a$'):
                verified = bcrypt.checkpw(password.encode('utf-8'), stored_password.encode('utf-8'))
            else:
                # Plaintext legacy password
                if stored_password == password:
                    verified = True
                    needs_rehash = True

            if verified:
                if needs_rehash:
                    try:
                        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
                        db2 = get_db()
                        db2.execute("UPDATE users SET password=? WHERE id=?",
                                    (hashed.decode('utf-8'), user["id"]))
                        db2.commit()
                        db2.close()
                        log.info("Password rehashed for: %s", email)
                    except Exception as e:
                        log.warning("Rehash failed for %s: %s", email, e)

                session.clear()
                session["user_id"] = user["id"]
                session["name"] = user["name"]
                session["role"] = (user["role"] or "").strip().lower()

                # Restore minimal analysis into session from DB (avoids lost state after re-login)
                full_analysis = _load_analysis_from_db(user["id"])
                if full_analysis:
                    session["analysis"] = _slim_for_session(full_analysis)
                    log.info("Restored analysis from DB for user_id=%s", user["id"])

                log.info("User logged in: %s (role: %s)", email, user["role"])
                return redirect("/dashboard")
            else:
                log.warning("Failed login attempt for: %s", email)
                return render_template("login.html", error="Invalid email or password")
        else:
            log.warning("Login attempt for unknown email: %s", email)
            return render_template("login.html", error="Invalid email or password")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.errorhandler(404)
def not_found(e):
    # Only redirect to dashboard if the user is logged in; otherwise to login
    if "user_id" in session:
        return redirect("/dashboard")
    return redirect("/login")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Dashboard routing â€” no redirect loops
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

STUDENT_SECTIONS = {"dashboard", "radar", "roadmap", "groups", "discovery"}
TEACHER_SECTIONS = {"dashboard", "requests", "groups", "discovery"}

# Sections that require a completed analysis to render meaningfully
_ANALYSIS_REQUIRED = {"roadmap"}


@app.route("/dashboard")
@app.route("/radar")
@app.route("/roadmap")
@app.route("/groups")
@app.route("/discovery")
@app.route("/requests")
@limiter.exempt
def dashboard():
    section = request.path.strip("/") or "dashboard"

    if "user_id" not in session:
        # Avoid redirect loop: only non-dashboard paths redirect to login
        if section == "dashboard":
            return render_template("index.html")
        return redirect("/login")

    role = session.get("role", "student")

    if role == "student":
        if section not in STUDENT_SECTIONS:
            return redirect("/dashboard")

        analysis_data = session.get("analysis")

        # If analysis is missing from session, try to reload from DB (handles cookie eviction)
        if not analysis_data:
            full = _load_analysis_from_db(session.get("user_id"))
            if full:
                analysis_data = _slim_for_session(full)
                session["analysis"] = analysis_data
                log.info("Re-hydrated session analysis from DB for user_id=%s", session.get("user_id"))

        # Redirect to radar only when analysis is strictly required and genuinely missing
        if not analysis_data and section in _ANALYSIS_REQUIRED:
            return redirect("/radar")

        return render_template("student_dashboard.html", active=section, analysis=analysis_data)

    else:
        # Teacher / faculty
        if section == "requests":
            pass  # allowed
        elif section not in TEACHER_SECTIONS:
            return redirect("/dashboard")
        return render_template("teacher_dashboard.html", active=section)


@app.route("/discovery_old")
def discovery_old():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("discovery.html")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# /analyze  â† THE PIPELINE ENTRYPOINT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Accepts a resume PDF + optional GitHub username + goal role.
    Runs the full 7-step AI pipeline.
    Full result â†’ DB.  Slim token â†’ session cookie.
    """
    goal_role = request.form.get("goal_role", "Web Developer").strip()
    resume_file = request.files.get("resume")
    github_user = request.form.get("github_user", "").strip()
    user_id = session.get("user_id", 0)

    if goal_role not in ROLES_CONFIG:
        log.warning("[analyze] Unknown goal_role=%r, defaulting to Web Developer", goal_role)
        goal_role = "Web Developer"

    # â”€â”€ Extract raw text from PDF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    raw_text = ""
    if resume_file and resume_file.filename:
        filename = secure_filename(resume_file.filename)
        if not filename.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files are supported."}), 400
        path = os.path.join(UPLOAD_FOLDER, filename)
        resume_file.save(path)
        try:
            from pdfminer.high_level import extract_text as pdf_extract
            raw_text = pdf_extract(path) or ""
            log.info("[analyze] PDF extracted: %d chars from '%s'", len(raw_text), filename)
        except Exception as exc:
            log.warning("[analyze] PDF extraction error: %s", exc)

    if not raw_text:
        log.info("[analyze] No resume text â€” GitHub-only or empty analysis")

    if not raw_text and not github_user:
        return jsonify({"error": "Could not extract text from document and no GitHub profile provided."}), 400

    # â”€â”€ Run the full pipeline â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    analysis = run_analysis_pipeline(
        raw_text=raw_text,
        goal_role=goal_role,
        github_user=github_user,
        user_id=user_id,
    )

    # Keep resume_text in the full analysis for role-switching
    analysis["resume_text"] = raw_text

    # Store only the slim version in the cookie
    session["analysis"] = _slim_for_session(analysis)

    log.info(
        "[analyze] Done. match_score=%.1f%%  cookie_keys=%d",
        analysis.get("match_score", 0),
        len(session["analysis"]),
    )

    return redirect("/radar")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# /switch-role  â€” re-run pipeline for a different role
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/switch-role", methods=["POST"])
def switch_role():
    if "analysis" not in session:
        return jsonify({"error": "No analysis exists to switch."}), 400

    new_role = request.form.get("new_role", "").strip()
    if new_role not in ROLES_CONFIG:
        return jsonify({"error": "Invalid role"}), 400

    prev = session["analysis"]
    github_user = prev.get("github_username", "")
    user_id = session.get("user_id", 0)

    # Try to get the full resume text from DB if the cookie only has a truncated version
    resume_text = prev.get("resume_text", "")
    if len(resume_text) <= _RESUME_TEXT_MAX and user_id:
        full = _load_analysis_from_db(user_id)
        if full:
            resume_text = full.get("resume_text", resume_text)

    analysis = run_analysis_pipeline(
        raw_text=resume_text,
        goal_role=new_role,
        github_user=github_user,
        user_id=user_id,
        previous_analysis=prev
    )
    analysis["resume_text"] = resume_text

    session["analysis"] = _slim_for_session(analysis)

    return jsonify({
        "success": True,
        "analysis": {
            "role": analysis["role"],
            "match_score": analysis["match_score"],
            "matched_skills": analysis.get("matched_skills", []),
            "missing_skills": analysis.get("missing_skills", []),
        },
    })


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# AI Mentor chat
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _call_groq(system_prompt: str, messages: list[dict]) -> str:
    """Call Groq's Llama-4 via OpenAI-compatible client."""
    try:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=full_messages,
            max_tokens=600,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except Exception as exc:
        err = str(exc).lower()
        log.error("[Groq] API error: %s", exc)
        if "invalid_api_key" in err or "authentication" in err:
            return "âš ï¸ Invalid Groq API key. Please update the key in your environment."
        if "rate" in err:
            return "âš ï¸ Groq rate limit hit. Please wait a moment and try again."
        return "âš ï¸ AI Mentor is temporarily unavailable. Please try again shortly."


@app.route("/chat", methods=["POST"])
def chat():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # Load analysis â€” prefer DB for richness, fall back to session
    analysis = session.get("analysis")
    if not analysis:
        analysis = _load_analysis_from_db(session.get("user_id"))
    if not analysis:
        return jsonify({"error": "No analysis found. Run a resume scan first."}), 400

    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Empty or invalid JSON body"}), 400

    message = (data.get("message") or "").strip()
    history = data.get("history") or []

    if not isinstance(history, list):
        history = []

    role = analysis.get("role", "Engineer")
    matched_skills = analysis.get("matched_skills", [])
    missing_skills = analysis.get("missing_skills", [])
    match_score = analysis.get("match_score", 0)
    user_level = analysis.get("user_level", "intermediate")

    top_actions_text = ""
    ranked = analysis.get("ranked_actions", [])
    if ranked:
        top_actions_text = "\nTop ranked next steps (by priority): " + "; ".join(
            f"{a['skill']}: {a['task']}" for a in ranked[:3]
        )

    system_prompt = f"""You are a personal career mentor for a candidate targeting: {role}.
Level: {user_level}. Verified skills: {', '.join(matched_skills) if matched_skills else 'none yet'} â€” {match_score:.0f}% role match.
Skill gaps to close: {', '.join(missing_skills) if missing_skills else 'none â€” fully qualified!'}.
{top_actions_text}

Rules:
- Always ground advice in the skill gaps above. Never give generic career tips.
- "what should I learn next" â†’ name the top 1-2 skills from the gap list with a concrete reason.
- "suggest a project" â†’ propose a project that uses at least 2 skills from the gap list.
- "explain [X]" â†’ brief explanation + connect it to their {role} roadmap.
- All else â†’ answer with their {role} context and gap list in mind. Be direct and specific."""

    conversation = []
    for h in history:
        if not isinstance(h, dict):
            continue
        role_label = "user" if h.get("sender") == "user" else "assistant"
        conversation.append({"role": role_label, "content": h.get("text", "")})
    if message:
        conversation.append({"role": "user", "content": message})

    reply = _call_groq(system_prompt, conversation)
    return jsonify({"reply": reply})


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Mentorship
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/request_mentorship", methods=["POST"])
def request_mentorship():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    goal_role = (request.form.get("goal_role") or "").strip()
    project_idea = (request.form.get("project_idea") or "").strip()

    if not goal_role or not project_idea:
        return jsonify({"error": "goal_role and project_idea are required"}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO mentorship_requests(student_id, goal_role, project_idea) VALUES(?,?,?)",
            (session["user_id"], goal_role, project_idea),
        )
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


@app.route("/get_matches")
def get_matches():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    analysis = session.get("analysis", {})
    missing_skills = analysis.get("missing_skills", [])

    matches = matching_engine.match_student_with_teachers(
        user_id=session["user_id"],
        missing_skills=missing_skills or None,
    )
    return jsonify({"matches": matches})


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Groups / Collaboration
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/create_group", methods=["POST"])
def create_group():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    name = (request.form.get("name") or "").strip()
    project_title = (request.form.get("project_title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    user_id = session["user_id"]
    db = get_db()
    cursor = db.execute(
        "INSERT INTO student_groups(name, project_title, description, leader_id) VALUES(?,?,?,?)",
        (name, project_title, description, user_id),
    )
    group_id = cursor.lastrowid
    db.execute("INSERT INTO student_group_members(group_id, student_id) VALUES(?,?)", (group_id, user_id))
    db.commit()
    db.close()
    return jsonify({"success": True, "group_id": group_id})


@app.route("/join_group", methods=["POST"])
def join_group():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    group_id = request.form.get("group_id")
    if not group_id:
        return jsonify({"error": "group_id is required"}), 400
    user_id = session["user_id"]
    db = get_db()
    try:
        db.execute("INSERT INTO student_group_members(group_id, student_id) VALUES(?,?)", (group_id, user_id))
        db.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    db.close()
    return jsonify({"success": success})


@app.route("/update_project_status", methods=["POST"])
def update_project_status():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    group_id = request.form.get("group_id")
    message = (request.form.get("message") or "").strip()
    status = (request.form.get("status") or "").strip()
    if not group_id:
        return jsonify({"error": "group_id is required"}), 400
    db = get_db()
    if status:
        db.execute("UPDATE student_groups SET status=? WHERE id=?", (status, group_id))
    if message:
        db.execute("INSERT INTO project_updates(group_id, message) VALUES(?,?)", (group_id, message))
    db.commit()
    db.close()
    return jsonify({"success": True})


@app.route("/get_groups")
def get_groups():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        groups = db.execute("""
            SELECT g.*, u.name as leader_name FROM student_groups g
            JOIN users u ON u.id = g.leader_id ORDER BY g.created_at DESC
        """).fetchall()
        result = []
        for g in groups:
            g_dict = dict(g)
            members = db.execute("""
                SELECT u.id, u.name FROM student_group_members m
                JOIN users u ON u.id = m.student_id WHERE m.group_id = ?
            """, (g["id"],)).fetchall()
            updates = db.execute("""
                SELECT * FROM project_updates WHERE group_id = ?
                ORDER BY timestamp DESC LIMIT 3
            """, (g["id"],)).fetchall()
            g_dict["members"] = [dict(m) for m in members]
            g_dict["updates"] = [dict(u) for u in updates]
            result.append(g_dict)
        return jsonify({"groups": result})
    finally:
        db.close()


@app.route("/get_students")
def get_students():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        students = db.execute("""
            SELECT u.id, u.name, u.bio, u.avatar, IFNULL(sa.status, 'Available') as availability
            FROM users u LEFT JOIN student_availability sa ON sa.user_id = u.id
            WHERE u.role = 'student'
        """).fetchall()
        return jsonify({"students": [dict(s) for s in students]})
    finally:
        db.close()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Phase 4: Collaboration network
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/student/groups", methods=["POST"])
def create_student_group():
    if "user_id" not in session or session.get("role") != "student":
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request must be JSON"}), 400
    name = (data.get("name") or "").strip()
    project_title = (data.get("project_title") or "").strip()
    project_id = data.get("project_id")
    goal = (data.get("goal") or "").strip()
    description = (data.get("description") or "").strip()
    max_members = data.get("max_members", 5)
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    db = get_db()
    if project_id:
        application = db.execute(
            "SELECT * FROM applications WHERE project_id=? AND student_id=? AND status='accepted'",
            (project_id, session["user_id"]),
        ).fetchone()
        if not application:
            db.close()
            return jsonify({"error": "You must be accepted to the project to create a group"}), 403
    cursor = db.execute("""
        INSERT INTO student_groups(name, project_title, project_id, goal, description, leader_id, max_members, status)
        VALUES(?,?,?,?,?,?,?,'active')
    """, (name, project_title, project_id, goal, description, session["user_id"], max_members))
    group_id = cursor.lastrowid
    db.execute(
        "INSERT INTO student_group_members(group_id, student_id, role) VALUES(?,?,'leader')",
        (group_id, session["user_id"]),
    )
    db.execute(
        "INSERT OR REPLACE INTO student_availability(user_id, status, looking_for, last_updated) VALUES(?,'busy','project',CURRENT_TIMESTAMP)",
        (session["user_id"],),
    )
    db.commit()
    db.close()
    return jsonify({"success": True, "group_id": group_id})


@app.route("/student/my-groups", methods=["GET"])
def get_my_groups():
    if "user_id" not in session or session.get("role") != "student":
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        groups = db.execute("""
            SELECT g.*, p.title as linked_project_title, p.faculty_id,
                   u.name as leader_name,
                   (SELECT COUNT(*) FROM student_group_members WHERE group_id=g.id) as member_count
            FROM student_groups g
            JOIN student_group_members m ON m.group_id=g.id
            LEFT JOIN projects p ON p.id=g.project_id
            JOIN users u ON u.id=g.leader_id
            WHERE m.student_id=? ORDER BY g.created_at DESC
        """, (session["user_id"],)).fetchall()
        result = []
        for g in groups:
            members = db.execute("""
                SELECT u.id, u.name, u.bio, m.role,
                       (SELECT GROUP_CONCAT(s.name) FROM user_skills us JOIN skills s ON s.id=us.skill_id WHERE us.user_id=u.id) as skills
                FROM student_group_members m JOIN users u ON u.id=m.student_id
                WHERE m.group_id=?
            """, (g["id"],)).fetchall()
            g_dict = dict(g)
            g_dict["members"] = [dict(m) for m in members]
            g_dict["is_leader"] = g["leader_id"] == session["user_id"]
            result.append(g_dict)
        return jsonify({"groups": result})
    finally:
        db.close()


@app.route("/discovery/network", methods=["GET"])
def get_discovery_network():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        students = db.execute("""
            SELECT u.id, u.name, u.bio, u.avatar, IFNULL(sa.status,'available') as availability,
                   sa.looking_for,
                   (SELECT GROUP_CONCAT(s.name) FROM user_skills us JOIN skills s ON s.id=us.skill_id WHERE us.user_id=u.id) as skills
            FROM users u LEFT JOIN student_availability sa ON sa.user_id=u.id
            WHERE u.role='student' AND u.id!=? ORDER BY u.name
        """, (session["user_id"],)).fetchall()
        groups = db.execute("""
            SELECT g.*, p.title as linked_project_title, u.name as leader_name,
                   (SELECT COUNT(*) FROM student_group_members WHERE group_id=g.id) as member_count
            FROM student_groups g LEFT JOIN projects p ON p.id=g.project_id
            JOIN users u ON u.id=g.leader_id WHERE g.status='active' ORDER BY g.created_at DESC
        """).fetchall()
        group_list = []
        for g in groups:
            members = db.execute("""
                SELECT u.id, u.name FROM student_group_members m JOIN users u ON u.id=m.student_id WHERE m.group_id=?
            """, (g["id"],)).fetchall()
            g_dict = dict(g)
            g_dict["members"] = [dict(m) for m in members]
            g_dict["is_member"] = any(m["id"] == session["user_id"] for m in members)
            group_list.append(g_dict)
        projects = db.execute("""
            SELECT p.*, u.name as faculty_name FROM projects p
            JOIN users u ON u.id=p.faculty_id WHERE p.status='open' ORDER BY p.posted_at DESC
        """).fetchall()
        project_list = []
        for p in projects:
            p_dict = dict(p)
            p_dict["required_skills"] = json.loads(p["required_skills"]) if p["required_skills"] else []
            project_list.append(p_dict)
        return jsonify({"students": [dict(s) for s in students], "groups": group_list, "projects": project_list})
    finally:
        db.close()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Teacher flow
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/get_incoming_requests")
def get_incoming_requests():
    if "user_id" not in session or session.get("role") not in {"teacher", "faculty"}:
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        reqs = db.execute("""
            SELECT r.id, u.name as student_name, r.goal_role, r.project_idea, r.status
            FROM mentorship_requests r JOIN users u ON u.id=r.student_id
            WHERE r.status='pending'
        """).fetchall()
        return jsonify({"requests": [dict(r) for r in reqs]})
    finally:
        db.close()


@app.route("/accept_request", methods=["POST"])
def accept_request():
    if "user_id" not in session or session.get("role") not in {"teacher", "faculty"}:
        return jsonify({"error": "Unauthorized"}), 401
    req_id = request.form.get("request_id")
    if not req_id:
        return jsonify({"error": "request_id is required"}), 400
    db = get_db()
    db.execute("UPDATE mentorship_requests SET status='matched' WHERE id=?", (req_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Messaging
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/send_message", methods=["POST"])
def send_message():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    receiver_id = request.form.get("receiver_id")
    message = (request.form.get("message") or "").strip()
    if not receiver_id or not message:
        return jsonify({"error": "receiver_id and message are required"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages(sender_id, receiver_id, message) VALUES(?,?,?)",
            (session["user_id"], receiver_id, message),
        )
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


@app.route("/get_messages")
def get_messages():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    other_id = request.args.get("other_id")
    if not other_id:
        return jsonify({"error": "other_id is required"}), 400
    db = get_db()
    try:
        msgs = db.execute("""
            SELECT * FROM messages
            WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)
            ORDER BY timestamp ASC
        """, (session["user_id"], other_id, other_id, session["user_id"])).fetchall()
        return jsonify({"messages": [dict(m) for m in msgs]})
    finally:
        db.close()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Phase 5: Smart Analytics
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/user/analytics", methods=["GET"])
def get_user_analytics():
    """Return radar, progress, and match-trend data for the dashboard charts."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    user_id = session["user_id"]
    analytics_limit = 50

    try:
        history_count_row = db.execute("""
            SELECT COUNT(*) AS total
            FROM user_analysis_history
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        history = db.execute("""
            SELECT total_score, skill_breakdown, created_at
            FROM user_analysis_history WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (user_id, analytics_limit)).fetchall()

        latest_row = db.execute("""
            SELECT total_score, skill_breakdown, matched_skills, missing_skills
            FROM user_analysis_history WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()

        first_row = db.execute("""
            SELECT total_score FROM user_analysis_history
            WHERE user_id = ? ORDER BY created_at ASC LIMIT 1
        """, (user_id,)).fetchone()

    finally:
        db.close()

    history = list(reversed(history))
    history_count = int((history_count_row["total"] if history_count_row else 0) or 0)
    session_analysis = session.get("analysis")

    response = {
        "radar": {},
        "progress": [],
        "match_trend": {},
        "stats": {},
    }

    if latest_row and latest_row["skill_breakdown"]:
        response["radar"] = safe_json_loads(latest_row["skill_breakdown"], {})
    elif session_analysis:
        response["radar"] = {
            d: calculate_domain_score(session_analysis.get("matched_skills", []), d)
            for d in ("frontend", "backend", "dsa", "ml", "devops")
        }

    for row in history:
        response["progress"].append({"date": row["created_at"], "score": row["total_score"]})

    if first_row and latest_row:
        response["match_trend"] = {
            "before": first_row["total_score"],
            "after": latest_row["total_score"],
            "improvement": latest_row["total_score"] - first_row["total_score"],
        }
    elif session_analysis:
        matched = len(session_analysis.get("matched_skills", []))
        response["match_trend"] = {"before": 0, "after": matched, "improvement": matched}
    else:
        response["match_trend"] = {"before": 0, "after": 0, "improvement": 0}

    if latest_row:
        response["stats"] = {
            "total_score": latest_row["total_score"],
            "matched_skills_count": len(safe_json_loads(latest_row["matched_skills"], [])),
            "missing_skills_count": len(safe_json_loads(latest_row["missing_skills"], [])),
            "analysis_count": history_count,
        }
    elif session_analysis:
        response["stats"] = {
            "total_score": len(session_analysis.get("matched_skills", [])),
            "matched_skills_count": len(session_analysis.get("matched_skills", [])),
            "missing_skills_count": len(session_analysis.get("missing_skills", [])),
            "analysis_count": 0,
        }

    return jsonify(response)


def calculate_domain_score(skills, domain):
    domain_keywords = {
        "frontend": ["html", "css", "javascript", "react", "vue", "angular", "typescript", "next.js", "tailwind"],
        "backend": ["python", "flask", "django", "node.js", "express", "sql", "api", "rest"],
        "dsa": ["algorithm", "data structure", "sorting", "searching", "graph", "tree", "dynamic programming"],
        "ml": ["machine learning", "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy", "data science"],
        "devops": ["docker", "kubernetes", "aws", "azure", "ci/cd", "jenkins", "terraform", "git"]
    }
    keywords = domain_keywords.get(domain, [])
    matches = sum(1 for skill in skills if any(kw.lower() in skill.lower() for kw in keywords))
    return min(100, int((matches / 10) * 100))


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Phase 6: Career Path Intelligence
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CAREER_ROLES = {
    "Web Developer": {
        "skills": ["html", "css", "javascript", "react", "vue", "angular", "typescript", "next.js", "tailwind", "bootstrap", "jquery"],
        "domains": ["frontend", "backend"],
        "description": "Builds full-stack web applications with modern frameworks"
    },
    "Frontend Engineer": {
        "skills": ["html", "css", "javascript", "react", "vue", "angular", "typescript", "next.js", "tailwind", "sass", "webpack"],
        "domains": ["frontend"],
        "description": "Specializes in user interfaces and client-side experiences"
    },
    "Backend Developer": {
        "skills": ["python", "flask", "django", "node.js", "express", "sql", "postgresql", "mongodb", "redis", "api", "rest", "graphql"],
        "domains": ["backend"],
        "description": "Builds server-side logic, APIs, and database systems"
    },
    "Full Stack Developer": {
        "skills": ["html", "css", "javascript", "react", "node.js", "python", "sql", "mongodb", "express", "django", "api"],
        "domains": ["frontend", "backend"],
        "description": "Handles both frontend and backend development"
    },
    "Data Scientist": {
        "skills": ["python", "pandas", "numpy", "matplotlib", "seaborn", "sql", "statistics", "machine learning", "scikit-learn", "jupyter"],
        "domains": ["ml"],
        "description": "Analyzes data and builds predictive models"
    },
    "ML Engineer": {
        "skills": ["python", "tensorflow", "pytorch", "scikit-learn", "machine learning", "deep learning", "nlp", "computer vision", "mlops"],
        "domains": ["ml"],
        "description": "Deploys and scales machine learning models"
    },
    "DevOps Engineer": {
        "skills": ["docker", "kubernetes", "aws", "azure", "ci/cd", "jenkins", "terraform", "ansible", "linux", "bash", "python"],
        "domains": ["devops"],
        "description": "Manages infrastructure and deployment pipelines"
    },
    "Software Engineer": {
        "skills": ["python", "java", "c++", "javascript", "git", "data structures", "algorithms", "oop", "system design"],
        "domains": ["dsa"],
        "description": "General software development with strong CS fundamentals"
    },
    "Mobile Developer": {
        "skills": ["flutter", "react native", "swift", "kotlin", "android", "ios", "mobile", "firebase"],
        "domains": ["frontend"],
        "description": "Builds native and cross-platform mobile applications"
    },
    "Data Engineer": {
        "skills": ["python", "sql", "spark", "hadoop", "kafka", "airflow", "etl", "data pipelines", "aws", "gcp"],
        "domains": ["backend", "ml"],
        "description": "Designs and maintains data infrastructure and pipelines"
    }
}


def calculate_career_match(user_skills, role_profile):
    if not user_skills:
        return 0, []
    role_skills = role_profile["skills"]
    user_set = set(s.lower().strip() for s in user_skills)
    role_set = set(s.lower().strip() for s in role_skills)
    exact_matches = user_set & role_set
    partial_matches = set()
    for user_skill in user_set:
        for role_skill in role_set:
            if user_skill in role_skill or role_skill in user_skill:
                partial_matches.add(role_skill)
    all_matches = exact_matches | partial_matches
    if role_skills:
        base_score = (len(all_matches) / len(role_skills)) * 100
        exact_bonus = (len(exact_matches) / len(role_skills)) * 20
        score = min(100, int(base_score + exact_bonus))
    else:
        score = 0
    return score, list(all_matches)


def get_top_career_suggestions(user_skills, top_n=3):
    if not user_skills:
        return []
    matches = []
    for role_name, profile in CAREER_ROLES.items():
        score, matched_skills = calculate_career_match(user_skills, profile)
        matches.append({
            "role": role_name,
            "score": score,
            "matched_skills": matched_skills,
            "description": profile["description"],
            "domains": profile["domains"]
        })
    matches.sort(key=lambda x: x["score"], reverse=True)
    return matches[:top_n]


@app.route("/career/compare", methods=["GET"])
def compare_careers():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    try:
        user_id = session["user_id"]
        latest = db.execute("""
            SELECT matched_skills FROM user_analysis_history
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()
    finally:
        db.close()

    user_skills = []
    if latest and latest["matched_skills"]:
        user_skills = safe_json_loads(latest["matched_skills"], [])
    elif session.get("analysis"):
        user_skills = session["analysis"].get("matched_skills", [])

    results = []
    for role_name, profile in CAREER_ROLES.items():
        score, matched = calculate_career_match(user_skills, profile)
        results.append({
            "role": role_name,
            "score": score,
            "matched_skills": matched[:5],
            "description": profile["description"],
            "domains": profile["domains"]
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    top_suggestions = results[:3] if len(results) >= 3 else results

    return jsonify({
        "all_roles": results,
        "suggested": top_suggestions,
        "best_fit": results[0] if results else None,
        "user_skill_count": len(user_skills)
    })


@app.route("/career/suggest", methods=["POST"])
def get_career_suggestions():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    analysis = session.get("analysis")
    if not analysis:
        return jsonify({"error": "Complete skill analysis first"}), 400
    user_skills = analysis.get("matched_skills", [])
    suggestions = get_top_career_suggestions(user_skills, top_n=3)
    return jsonify({
        "suggestions": suggestions,
        "current_role": analysis.get("role", "Unknown"),
        "current_match": analysis.get("match_score", 0)
    })


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# HTML page routes
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/faculty/dashboard")
@limiter.exempt
def faculty_dashboard():
    if "user_id" not in session or session.get("role") not in {"teacher", "faculty"}:
        return redirect("/login")
    return render_template("faculty_projects.html")


@app.route("/student/projects-page")
@limiter.exempt
def student_projects_page():
    if "user_id" not in session or session.get("role") != "student":
        return redirect("/login")
    return render_template("student_projects.html")


@app.route("/faculty/applications/<int:project_id>/view")
@limiter.exempt
def faculty_applications_page(project_id):
    if "user_id" not in session or session.get("role") not in {"teacher", "faculty"}:
        return redirect("/login")
    return render_template("faculty_applications.html", project_id=project_id)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Error Handlers
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    log.warning("Rate limit exceeded: %s %s | IP=%s", request.method, request.path, request.remote_addr)
    return jsonify({
        "success": False,
        "error": "Too many requests. Please try again later."
    }), 429


@app.errorhandler(Exception)
def handle_exception(e):
    log.error("Unhandled exception on %s %s: %s", request.method, request.path, e, exc_info=True)
    # Don't expose internal details in production
    if IS_DEV:
        return error_response(f"Internal server error: {str(e)}", 500)
    return error_response("Internal server error", 500)


if __name__ == "__main__":
    app.run(debug=IS_DEV, port=5000)

