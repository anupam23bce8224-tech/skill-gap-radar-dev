"""
faculty_routes.py — Production-ready faculty project management routes.

Routes:
- GET /faculty/projects: List faculty's projects
- POST /faculty/projects: Create new project
- GET /faculty/matches/<project_id>: Get skill matches for project applicants
- GET /faculty/applications/<project_id>: Get applications for a project
- PATCH /faculty/applications/<id>/status: Update application status
"""

import json
import logging
from flask import Blueprint, request, session, jsonify
from database import get_db
from services.matching_engine import similarity_scores
from utils import (
    require_faculty, error_response,
    validate_field, check_project_ownership, sanitize_string, sanitize_list,
    get_cached_similarity, set_cached_similarity, log_api_action
)

log = logging.getLogger("SkillRadar")
faculty_bp = Blueprint('faculty', __name__)


@faculty_bp.route('/faculty/projects', methods=['GET'])
@require_faculty
def get_faculty_projects():
    """Get all projects created by the current faculty member."""
    faculty_id = session['user_id']
    db = get_db()

    try:
        projects = db.execute("""
            SELECT p.id, p.title, p.description, p.required_skills, p.posted_at, p.status,
                   COUNT(a.id) as application_count,
                   COUNT(CASE WHEN a.status = 'accepted' THEN 1 END) as accepted_count
            FROM projects p
            LEFT JOIN applications a ON a.project_id = p.id
            WHERE p.faculty_id = ?
            GROUP BY p.id
            ORDER BY p.posted_at DESC
        """, (faculty_id,)).fetchall()

        result = []
        for p in projects:
            result.append({
                'id': p['id'],
                'title': sanitize_string(p['title']),
                'description': sanitize_string(p['description']),
                'required_skills': sanitize_list(json.loads(p['required_skills'] or '[]')),
                'posted_at': p['posted_at'],
                'status': p['status'],
                'application_count': p['application_count'],
                'accepted_count': p['accepted_count']
            })

        log_api_action(faculty_id, '/faculty/projects', 'GET', details=f'returned {len(result)} projects')
        return jsonify(result)

    except Exception as e:
        log.error(f"Error in get_faculty_projects: {e}")
        return error_response("Internal server error", 500)
    finally:
        db.close()


@faculty_bp.route('/faculty/projects', methods=['POST'])
@faculty_bp.route('/faculty/post-project', methods=['POST'])
@require_faculty
def create_project():
    """Create a new project."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json()
    if data is None:
        return error_response("Empty request body", 400)

    # Validate required fields
    title, err = validate_field('title', str, min_length=5, max_length=200)
    if err:
        return error_response(err, 400)

    description, err = validate_field('description', str, min_length=20, max_length=2000)
    if err:
        return error_response(err, 400)

    required_skills, err = validate_field('required_skills', list, min_length=1)
    if err:
        return error_response(err, 400)

    faculty_id = session['user_id']
    db = get_db()

    try:
        # Validate skills array items are strings
        if not all(isinstance(s, str) and s.strip() for s in required_skills):
            return error_response("All skills must be non-empty strings", 400)

        cursor = db.execute("""
            INSERT INTO projects (faculty_id, title, description, required_skills, status)
            VALUES (?, ?, ?, ?, 'open')
        """, (faculty_id, sanitize_string(title), sanitize_string(description), json.dumps(required_skills)))

        project_id = cursor.lastrowid
        db.commit()

        log_api_action(
            faculty_id, '/faculty/projects', 'POST',
            details=f'project_id={project_id} title={title}'
        )

        return jsonify({
            'success': True,
            'project_id': project_id,
            'title': title,
            'description': description,
            'required_skills': required_skills,
            'status': 'open'
        })

    except Exception as e:
        db.rollback()
        log.error(f"Error creating project: {e}")
        return error_response("Failed to create project", 500)
    finally:
        db.close()


@faculty_bp.route('/faculty/matches/<int:project_id>', methods=['GET'])
@require_faculty
def get_project_matches(project_id):
    """Get skill matches for all applicants to a project."""
    faculty_id = session['user_id']
    db = get_db()

    try:
        # Verify faculty owns this project (authorization)
        project, err = check_project_ownership(db, project_id, faculty_id)
        if err:
            log_api_action(faculty_id, f'/faculty/matches/{project_id}', 'GET', status='failed', details=err)
            return error_response(err, 404 if err == "Project not found" else 403)

        required_skills = sanitize_list(json.loads(project['required_skills'] or '[]'))

        # Get all applications with student analysis
        applications = db.execute("""
            SELECT a.id, a.student_id, a.student_name, a.student_skills,
                   a.project_idea, a.interest_statement, a.match_score, a.match_reason, a.status,
                   ua.analysis_data
            FROM applications a
            LEFT JOIN user_analysis ua ON ua.user_id = a.student_id
            WHERE a.project_id = ?
            ORDER BY a.match_score DESC, a.applied_at DESC
        """, (project_id,)).fetchall()

        result = []
        for app in applications:
            matched_skills = []
            missing_skills = []

            try:
                if app['analysis_data']:
                    analysis = json.loads(app['analysis_data'])
                    student_skills = analysis.get('matched', {})
                    if not isinstance(student_skills, dict):
                        student_skills = {}

                    # Calculate detailed matches
                    for req_skill in required_skills:
                        if req_skill in student_skills:
                            skill_info = student_skills[req_skill]
                            confidence = skill_info.get('confidence', 0.5)
                            matched_skills.append({
                                'name': req_skill,
                                'confidence': round(confidence * 100),
                                'source': skill_info.get('source', 'resume')
                            })
                        else:
                            # Semantic matching with cache
                            student_skill_names = list(student_skills.keys())
                            if student_skill_names:
                                try:
                                    cache_key = f"{req_skill}|{'|'.join(sorted(student_skill_names))}"
                                    scores = get_cached_similarity(req_skill, cache_key)
                                    if scores is None:
                                        scores = similarity_scores(req_skill, student_skill_names)
                                        set_cached_similarity(req_skill, cache_key, scores)

                                    best_match = max(scores, key=lambda x: x[1]) if scores else None
                                    if best_match and best_match[1] >= 0.7:
                                        matched_skill_name, similarity = best_match
                                        matched_skills.append({
                                            'name': req_skill,
                                            'confidence': round(similarity * 100),
                                            'source': 'semantic_match',
                                            'matched_via': matched_skill_name
                                        })
                                    else:
                                        missing_skills.append(req_skill)
                                except Exception as e:
                                    log.error(f"Error computing similarity: {e}")
                                    missing_skills.append(req_skill)
                            else:
                                missing_skills.append(req_skill)
                else:
                    # No analysis data, use stored skills from application
                    stored_skills = json.loads(app['student_skills'] or '[]')
                    matched_skills = [
                        sk.get('name') for sk in stored_skills
                        if isinstance(sk, dict) and sk.get('name')
                    ]
                    missing_skills = [s for s in required_skills if s not in matched_skills]

            except Exception as e:
                log.error(f"Error processing application {app['id']}: {e}")
                # Fallback to stored data
                fallback_skills = json.loads(app['student_skills'] or '[]')
                matched_skills = [
                    sk.get('name') for sk in fallback_skills
                    if isinstance(sk, dict) and sk.get('name')
                ]
                missing_skills = [s for s in required_skills if s not in matched_skills]

            normalized_matched = []
            for item in matched_skills:
                if isinstance(item, dict):
                    name = item.get('name')
                    if name:
                        normalized_matched.append(name)
                elif isinstance(item, str) and item.strip():
                    normalized_matched.append(item.strip())

            result.append({
                'application_id': app['id'],
                'student_id': app['student_id'],
                'student_name': sanitize_string(app['student_name']),
                'project_idea': sanitize_string(app['project_idea']),
                'interest_statement': sanitize_string(app['interest_statement']),
                'matched_skills': normalized_matched,
                'missing_skills': missing_skills,
                'match_score': app['match_score'],
                'match_reason': sanitize_string(app['match_reason'] or "Skill-fit score based on required skills"),
                'status': app['status']
            })

        log_api_action(faculty_id, f'/faculty/matches/{project_id}', 'GET', details=f'returned {len(result)} matches')
        return jsonify(result)

    except Exception as e:
        log.error(f"Error in get_project_matches: {e}")
        return error_response("Internal server error", 500)
    finally:
        db.close()


@faculty_bp.route('/faculty/applications/<int:project_id>', methods=['GET'])
@require_faculty
def get_project_applications(project_id):
    """Get all applications for a specific project."""
    faculty_id = session['user_id']
    db = get_db()

    try:
        # Verify faculty owns this project  (authorization)
        project, err = check_project_ownership(db, project_id, faculty_id)
        if err:
            log_api_action(faculty_id, f'/faculty/applications/{project_id}', 'GET', status='failed', details=err)
            return error_response(err, 404 if err == "Project not found" else 403)

        applications = db.execute("""
            SELECT a.id, a.student_id, a.student_name, a.student_skills,
                   a.project_idea, a.interest_statement, a.match_score, a.match_reason,
                   a.status, a.applied_at
            FROM applications a
            WHERE a.project_id = ?
            ORDER BY a.match_score DESC, a.applied_at DESC
        """, (project_id,)).fetchall()

        result = []
        for app in applications:
            stored_skills = sanitize_list(json.loads(app['student_skills'] or '[]'))
            matched_skill_names = []
            for skill in stored_skills:
                if isinstance(skill, dict):
                    name = skill.get('name')
                    if name:
                        matched_skill_names.append(name)
                elif isinstance(skill, str) and skill.strip():
                    matched_skill_names.append(skill.strip())

            result.append({
                'application_id': app['id'],
                'student_id': app['student_id'],
                'student_name': sanitize_string(app['student_name']),
                'student_skills': stored_skills,
                'matched_skills': matched_skill_names,
                'project_idea': sanitize_string(app['project_idea']),
                'interest_statement': sanitize_string(app['interest_statement']),
                'match_score': app['match_score'],
                'match_reason': sanitize_string(app['match_reason'] or "Skill-fit score based on required skills"),
                'status': app['status'],
                'applied_at': app['applied_at']
            })

        log_api_action(faculty_id, f'/faculty/applications/{project_id}', 'GET', details=f'returned {len(result)} applications')
        return jsonify(result)

    except Exception as e:
        log.error(f"Error in get_project_applications: {e}")
        return error_response("Internal server error", 500)
    finally:
        db.close()


@faculty_bp.route('/faculty/applications/<int:application_id>/status', methods=['PATCH'])
@require_faculty
def update_application_status(application_id):
    """Update the status of an application."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json()
    if data is None:
        return error_response("Empty request body", 400)

    new_status, err = validate_field('status', str, min_length=1, max_length=20)
    if err:
        return error_response(err, 400)

    if new_status not in ['pending', 'accepted', 'rejected']:
        return error_response("Invalid status. Must be: pending, accepted, or rejected", 400)

    faculty_id = session['user_id']
    db = get_db()

    try:
        # Verify faculty owns the project this application belongs to (authorization)
        application = db.execute("""
            SELECT a.id, a.status, p.faculty_id
            FROM applications a
            JOIN projects p ON p.id = a.project_id
            WHERE a.id = ?
        """, (application_id,)).fetchone()

        if not application:
            log_api_action(
                faculty_id, f'/faculty/applications/{application_id}/status', 'PATCH',
                status='failed', details='application not found'
            )
            return error_response("Application not found", 404)

        if application['faculty_id'] != faculty_id:
            log_api_action(
                faculty_id, f'/faculty/applications/{application_id}/status', 'PATCH',
                status='failed', details='unauthorized access'
            )
            return error_response("Access denied", 403)

        # Update the status
        db.execute("""
            UPDATE applications SET status = ? WHERE id = ?
        """, (new_status, application_id))

        db.commit()

        log_api_action(
            faculty_id, f'/faculty/applications/{application_id}/status', 'PATCH',
            details=f'status changed to {new_status}'
        )

        return jsonify({
            'success': True,
            'application_id': application_id,
            'status': new_status
        })

    except Exception as e:
        db.rollback()
        log.error(f"Error updating application status: {e}")
        return error_response("Failed to update status", 500)
    finally:
        db.close()
