"""
student_routes.py — Production-ready student project system routes.

Routes:
- GET /student/projects: List all open projects with AI-matched skills
- POST /student/apply/<project_id>: Apply to a project with skill matching
"""

import json
import logging
from flask import Blueprint, request, session, jsonify
from database import get_db
from services.matching_engine import similarity_scores
from utils import (
    require_student, error_response, success_response,
    validate_field, check_ownership, sanitize_string, sanitize_list,
    get_cached_similarity, set_cached_similarity, log_api_action
)

log = logging.getLogger("SkillRadar")
student_bp = Blueprint('student', __name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe_json(value, default=None):
    if default is None:
        default = []
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _semantic_match(req_skill: str, student_skills: dict) -> dict | None:
    """
    Try to find a semantic match for req_skill among the student's skill names.
    Returns a matched_skill dict or None.
    """
    student_skill_names = list(student_skills.keys())
    if not student_skill_names:
        return None

    cache_key = f"{req_skill}|{'|'.join(sorted(student_skill_names))}"
    scores = get_cached_similarity(req_skill, cache_key)

    if scores is None:
        try:
            scores = similarity_scores(req_skill, student_skill_names, timeout=5)
        except Exception:
            log.warning("Similarity timeout for '%s', falling back to exact match", req_skill)
            scores = [
                (s, 1.0 if s.lower() == req_skill.lower() else 0.0)
                for s in student_skill_names
            ]
        set_cached_similarity(req_skill, cache_key, scores)

    best = max(scores, key=lambda x: x[1]) if scores else None
    if best and best[1] >= 0.7:
        matched_name, similarity = best
        return {
            'name': req_skill,
            'confidence': round(similarity * 100),
            'source': 'semantic_match',
            'matched_via': matched_name
        }
    return None


def _build_skill_match(required_skills: list, student_skills: dict) -> tuple[list, list]:
    """
    Return (matched_skills, missing_skills) for a list of required skills
    against the student's skill dict.
    """
    matched, missing = [], []

    for req_skill in required_skills:
        if req_skill in student_skills:
            skill_info = student_skills[req_skill]
            matched.append({
                'name': req_skill,
                'confidence': round(skill_info.get('confidence', 0.5) * 100),
                'source': skill_info.get('source', 'resume')
            })
        else:
            try:
                sem = _semantic_match(req_skill, student_skills)
                if sem:
                    matched.append(sem)
                else:
                    missing.append(req_skill)
            except Exception as e:
                log.error("Error computing similarity for '%s': %s", req_skill, e)
                missing.append(req_skill)

    return matched, missing


def _load_student_analysis(db, user_id: int) -> dict | None:
    """
    Load the latest analysis for a student, trying the primary cache table
    first then falling back to the history table.
    Returns a dict with at least a 'matched' key, or None.
    """
    # Primary: user_analysis (full blob)
    row = db.execute(
        "SELECT analysis_data FROM user_analysis WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    if row and row['analysis_data']:
        try:
            data = json.loads(row['analysis_data'])
            if isinstance(data, dict) and 'matched' in data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: history table
    hist = db.execute(
        "SELECT matched_skills, missing_skills, total_score FROM user_analysis_history "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    if hist:
        try:
            matched_list = _safe_json(hist['matched_skills'], [])
            return {
                'matched': {k: {'confidence': 0.7, 'source': 'history'} for k in matched_list},
                'match_score': hist['total_score']
            }
        except Exception:
            pass

    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@student_bp.route('/student/projects', methods=['GET'])
@require_student
def get_student_projects():
    """Get all open projects with AI-matched skills for the current student."""
    user_id = session['user_id']
    db = get_db()

    try:
        analysis = _load_student_analysis(db, user_id)

        # Fetch all open projects once (shared for both branches)
        projects = db.execute("""
            SELECT p.id, p.title, p.description, p.required_skills, p.posted_at,
                   u.name as faculty_name
            FROM projects p
            JOIN users u ON u.id = p.faculty_id
            WHERE p.status = 'open'
            ORDER BY p.posted_at DESC
        """).fetchall()

        result = []
        student_skills = (analysis or {}).get('matched', {})

        for p in projects:
            required_skills = sanitize_list(_safe_json(p['required_skills'], []))

            if not analysis or not student_skills:
                matched_skills, missing_skills, match_score = [], [], 0
            else:
                matched_skills, missing_skills = _build_skill_match(required_skills, student_skills)
                match_score = (
                    round((len(matched_skills) / len(required_skills)) * 100)
                    if required_skills else 0
                )

            result.append({
                'id': p['id'],
                'title': sanitize_string(p['title']),
                'description': sanitize_string(p['description']),
                'faculty_name': sanitize_string(p['faculty_name']),
                'required_skills': required_skills,
                'posted_at': p['posted_at'],
                'matched_skills': matched_skills,
                'missing_skills': missing_skills,
                'match_score': match_score
            })

        label = 'with matching' if analysis else 'without matching'
        log_api_action(user_id, '/student/projects', 'GET',
                       details=f'returned {len(result)} projects {label}')
        log.debug("[/student/projects] %d projects for user_id=%s", len(result), user_id)
        return success_response({"projects": result})

    except Exception as e:
        log.error("[/student/projects] Error for user_id=%s: %s", user_id, e, exc_info=True)
        return error_response("Internal server error", 500)
    finally:
        db.close()


@student_bp.route('/student/apply/<int:project_id>', methods=['POST'])
@require_student
def apply_to_project(project_id):
    """Apply to a project with skill matching calculation and duplicate prevention."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json(silent=True)
    if data is None:
        return error_response("Empty or malformed request body", 400)

    # Validate required fields
    project_idea, err = validate_field('project_idea', str, min_length=10, max_length=2000)
    if err:
        return error_response(err, 400)

    interest_statement, err = validate_field('interest_statement', str, min_length=10, max_length=2000)
    if err:
        return error_response(err, 400)

    user_id = session['user_id']
    db = get_db()

    try:
        # Validate project exists and is open
        project = db.execute("""
            SELECT p.*, u.name as faculty_name
            FROM projects p
            JOIN users u ON u.id = p.faculty_id
            WHERE p.id = ? AND p.status = 'open'
        """, (project_id,)).fetchone()

        if not project:
            log_api_action(user_id, f'/student/apply/{project_id}', 'POST',
                           status='failed', details='project not found')
            return error_response("Project not found or not open", 404)

        # Check for duplicate application before doing any expensive work
        existing_app = db.execute(
            "SELECT id, status FROM applications WHERE project_id = ? AND student_id = ?",
            (project_id, user_id)
        ).fetchone()

        if existing_app:
            log_api_action(user_id, f'/student/apply/{project_id}', 'POST',
                           status='failed', details='duplicate application')
            return error_response(
                f"You have already applied to this project (status: {existing_app['status']})",
                400
            )

        # Calculate skill matching
        matched_skills: list = []
        missing_skills: list = []
        match_score: int = 0

        analysis = _load_student_analysis(db, user_id)
        if analysis:
            try:
                student_skills = analysis.get('matched', {})
                required_skills = _safe_json(project['required_skills'], [])
                matched_skills, missing_skills = _build_skill_match(required_skills, student_skills)
                if required_skills:
                    match_score = round((len(matched_skills) / len(required_skills)) * 100)
            except Exception as e:
                log.error("[apply] Skill match calculation failed for user_id=%s: %s", user_id, e)

        # Get student name
        student = db.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        student_name = student['name'] if student else 'Unknown'

        # Insert application — the UNIQUE constraint guards against race conditions
        try:
            cursor = db.execute("""
                INSERT INTO applications (
                    project_id, student_id, student_name, student_skills,
                    project_idea, interest_statement, match_score, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (
                project_id, user_id, student_name,
                json.dumps(matched_skills),
                sanitize_string(project_idea),
                sanitize_string(interest_statement),
                match_score
            ))

            application_id = cursor.lastrowid
            db.commit()

            log_api_action(
                user_id, f'/student/apply/{project_id}', 'POST',
                details=f'application_id={application_id} match_score={match_score}'
            )

            return success_response({
                'application_id': application_id,
                'matched_skills': matched_skills,
                'missing_skills': missing_skills,
                'match_score': match_score,
                'status': 'pending'
            }, message="Application submitted successfully")

        except Exception as integrity_err:
            # Catches both IntegrityError (race condition duplicate) and other DB errors
            err_str = str(integrity_err).lower()
            if 'unique' in err_str or 'integrity' in err_str:
                log_api_action(user_id, f'/student/apply/{project_id}', 'POST',
                               status='failed', details='race condition duplicate')
                return error_response("You have already applied to this project", 400)
            db.rollback()
            log.error("[apply] DB insert failed for user_id=%s project_id=%s: %s",
                      user_id, project_id, integrity_err)
            return error_response("Failed to submit application", 500)

    except Exception as e:
        log.error("[apply] Unexpected error for user_id=%s project_id=%s: %s",
                  user_id, project_id, e, exc_info=True)
        return error_response("Internal server error", 500)
    finally:
        db.close()