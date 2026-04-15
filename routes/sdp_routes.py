"""
sdp_routes.py — Production-ready SDP (Senior Design Project) proposal system routes.

Routes:
- GET /get_proposals: List all SDP proposals
- POST /submit_proposal: Submit a new SDP proposal
- POST /proposal/approve: Approve a proposal
- POST /proposal/reject: Reject a proposal
"""

import json
import logging
from flask import Blueprint, request, session, jsonify
from database import get_db
from utils import (
    require_authenticated, require_faculty, require_student,
    error_response, success_response,
    validate_field, sanitize_string, sanitize_list,
    log_api_action
)

log = logging.getLogger("SkillRadar")
sdp_bp = Blueprint('sdp', __name__)


@sdp_bp.route('/get_proposals', methods=['GET'])
@require_authenticated
def get_proposals():
    """Get all SDP proposals based on user role."""
    user_id = session['user_id']
    user_role = session.get('role')
    db = get_db()

    try:
        if user_role == 'faculty':
            # Faculty can see all proposals
            proposals = db.execute("""
                SELECT p.id, p.title, p.description, p.objectives, p.technologies,
                       p.team_size, p.duration, p.submitted_by, p.status, p.submitted_at,
                       u.name as submitter_name
                FROM sdp_proposals p
                JOIN users u ON u.id = p.submitted_by
                ORDER BY p.submitted_at DESC
            """).fetchall()

        elif user_role == 'student':
            # Students can see their own proposals and approved ones
            proposals = db.execute("""
                SELECT p.id, p.title, p.description, p.objectives, p.technologies,
                       p.team_size, p.duration, p.submitted_by, p.status, p.submitted_at,
                       u.name as submitter_name
                FROM sdp_proposals p
                JOIN users u ON u.id = p.submitted_by
                WHERE p.submitted_by = ? OR p.status = 'approved'
                ORDER BY p.submitted_at DESC
            """, (user_id,)).fetchall()

        else:
            log_api_action(user_id, '/get_proposals', 'GET', status='failed', details='invalid role')
            return error_response("Invalid user role", 403)

        result = []
        for p in proposals:
            result.append({
                'id': p['id'],
                'title': sanitize_string(p['title']),
                'description': sanitize_string(p['description']),
                'objectives': sanitize_list(json.loads(p['objectives'] or '[]')),
                'technologies': sanitize_list(json.loads(p['technologies'] or '[]')),
                'team_size': p['team_size'],
                'duration': sanitize_string(p['duration']),
                'submitted_by': p['submitted_by'],
                'submitter_name': sanitize_string(p['submitter_name']),
                'status': p['status'],
                'submitted_at': p['submitted_at']
            })

        log_api_action(user_id, '/get_proposals', 'GET', details=f'returned {len(result)} proposals')
        return success_response({"proposals": result})

    except Exception as e:
        log.error(f"Error in get_proposals: {e}")
        return error_response("Internal server error", 500)
    finally:
        db.close()


@sdp_bp.route('/submit_proposal', methods=['POST'])
@require_student
def submit_proposal():
    """Submit a new SDP proposal with duplicate prevention."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json()
    if data is None:
        return error_response("Empty request body", 400)

    # Validate required fields
    title, err = validate_field('title', str, min_length=5, max_length=200)
    if err:
        return error_response(err, 400)

    description, err = validate_field('description', str, min_length=20, max_length=3000)
    if err:
        return error_response(err, 400)

    objectives, err = validate_field('objectives', list, min_length=1)
    if err:
        return error_response(err, 400)

    technologies, err = validate_field('technologies', list, min_length=1)
    if err:
        return error_response(err, 400)

    team_size, err = validate_field('team_size', int)
    if err:
        return error_response(err, 400)

    duration, err = validate_field('duration', str, min_length=1, max_length=100)
    if err:
        return error_response(err, 400)

    # Validate ranges
    if not (1 <= team_size <= 10):
        return error_response("Team size must be between 1 and 10", 400)

    # Validate array items
    if not all(isinstance(obj, str) and obj.strip() for obj in objectives):
        return error_response("All objectives must be non-empty strings", 400)

    if not all(isinstance(tech, str) and tech.strip() for tech in technologies):
        return error_response("All technologies must be non-empty strings", 400)

    user_id = session['user_id']
    db = get_db()

    try:
        # Check for existing pending proposal (duplicate prevention)
        existing = db.execute("""
            SELECT id, status FROM sdp_proposals
            WHERE submitted_by = ? AND status = 'pending'
        """, (user_id,)).fetchone()

        if existing:
            log_api_action(user_id, '/submit_proposal', 'POST', status='failed', details='duplicate pending proposal')
            return error_response(
                "You already have a pending proposal. Please wait for review or withdraw it first.",
                400
            )

        cursor = db.execute("""
            INSERT INTO sdp_proposals (
                title, description, objectives, technologies,
                team_size, duration, submitted_by, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            sanitize_string(title),
            sanitize_string(description),
            json.dumps(objectives),
            json.dumps(technologies),
            team_size,
            sanitize_string(duration),
            user_id
        ))

        proposal_id = cursor.lastrowid
        db.commit()

        log_api_action(
            user_id, '/submit_proposal', 'POST',
            details=f'proposal_id={proposal_id} title={title}'
        )

        return success_response({
            'proposal_id': proposal_id,
            'title': title,
            'description': description,
            'objectives': objectives,
            'technologies': technologies,
            'team_size': team_size,
            'duration': duration,
            'status': 'pending'
        }, message="Proposal submitted successfully")

    except Exception as e:
        db.rollback()
        log.error(f"Error submitting proposal: {e}")
        return error_response("Failed to submit proposal", 500)
    finally:
        db.close()


@sdp_bp.route('/proposal/approve', methods=['POST'])
@require_faculty
def approve_proposal():
    """Approve an SDP proposal."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json()
    if data is None:
        return error_response("Empty request body", 400)

    proposal_id, err = validate_field('proposal_id', int)
    if err:
        return error_response(err, 400)

    faculty_id = session['user_id']
    db = get_db()

    try:
        # Check if proposal exists and is pending
        proposal = db.execute("""
            SELECT id, status FROM sdp_proposals WHERE id = ?
        """, (proposal_id,)).fetchone()

        if not proposal:
            log_api_action(faculty_id, '/proposal/approve', 'POST', status='failed', details='proposal not found')
            return error_response("Proposal not found", 404)

        if proposal['status'] != 'pending':
            log_api_action(
                faculty_id, '/proposal/approve', 'POST',
                status='failed', details=f'proposal already {proposal["status"]}'
            )
            return error_response(f"Proposal is already {proposal['status']}", 400)

        # Update status to approved
        db.execute("""
            UPDATE sdp_proposals SET status = 'approved' WHERE id = ?
        """, (proposal_id,))

        db.commit()

        log_api_action(faculty_id, '/proposal/approve', 'POST', details=f'proposal_id={proposal_id}')

        return success_response({
            'proposal_id': proposal_id,
            'status': 'approved'
        }, message="Proposal approved successfully")

    except Exception as e:
        db.rollback()
        log.error(f"Error approving proposal: {e}")
        return error_response("Failed to approve proposal", 500)
    finally:
        db.close()


@sdp_bp.route('/proposal/reject', methods=['POST'])
@require_faculty
def reject_proposal():
    """Reject an SDP proposal."""
    if not request.is_json:
        return error_response("Request must be JSON", 400)

    data = request.get_json()
    if data is None:
        return error_response("Empty request body", 400)

    proposal_id, err = validate_field('proposal_id', int)
    if err:
        return error_response(err, 400)

    faculty_id = session['user_id']
    db = get_db()

    try:
        # Check if proposal exists and is pending
        proposal = db.execute("""
            SELECT id, status FROM sdp_proposals WHERE id = ?
        """, (proposal_id,)).fetchone()

        if not proposal:
            log_api_action(faculty_id, '/proposal/reject', 'POST', status='failed', details='proposal not found')
            return error_response("Proposal not found", 404)

        if proposal['status'] != 'pending':
            log_api_action(
                faculty_id, '/proposal/reject', 'POST',
                status='failed', details=f'proposal already {proposal["status"]}'
            )
            return error_response(f"Proposal is already {proposal['status']}", 400)

        # Update status to rejected
        db.execute("""
            UPDATE sdp_proposals SET status = 'rejected' WHERE id = ?
        """, (proposal_id,))

        db.commit()

        log_api_action(faculty_id, '/proposal/reject', 'POST', details=f'proposal_id={proposal_id}')

        return success_response({
            'proposal_id': proposal_id,
            'status': 'rejected'
        }, message="Proposal rejected successfully")

    except Exception as e:
        db.rollback()
        log.error(f"Error rejecting proposal: {e}")
        return error_response("Failed to reject proposal", 500)
    finally:
        db.close()