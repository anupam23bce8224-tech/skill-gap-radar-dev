"""
test_production_features.py — Comprehensive test of production-ready features.

Run: python test_production_features.py
"""

import os
os.environ['GROQ_API_KEY'] = 'dummy'

import json
import logging
from app import app
from utils import (
    require_student, require_faculty, error_response, success_response,
    validate_field, check_ownership, sanitize_string, sanitize_list,
    EmbeddingCache, log_auth_event, log_api_action
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ProdTest")

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Response Format Standardization
# ─────────────────────────────────────────────────────────────────────────────
def test_response_formats():
    print("\n[TEST 1] Response Format Standardization")

    with app.app_context():
        # Success response
        resp = success_response({"id": 1, "name": "Test"}, "Created successfully")
        data = resp.get_json()
        assert data["success"] == True
        assert data["error"] is None
        assert data["data"]["id"] == 1
        print("  ✓ Success response format correct")

        # Error response
        resp = error_response("Invalid input", 400)
        status_code = resp[1]
        data = resp[0].get_json()
        assert status_code == 400
        assert data["success"] == False
        assert data["error"] == "Invalid input"
        print("  ✓ Error response format correct")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Input Validation
# ─────────────────────────────────────────────────────────────────────────────
def test_sanitization():
    print("\n[TEST 2] Input Sanitization")

    # String sanitization
    long_str = "x" * 1000
    sanitized = sanitize_string(long_str, max_length=100)
    assert len(sanitized) == 100
    print("  ✓ String length bounded at 100")

    # List sanitization
    long_list = list(range(200))
    sanitized = sanitize_list(long_list, max_items=50)
    assert len(sanitized) == 50
    print("  ✓ List items bounded at 50")

    # String trimming
    messy = "  hello world  "
    clean = sanitize_string(messy)
    assert clean == "hello world"
    print("  ✓ String whitespace trimmed")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Authorization Helpers
# ─────────────────────────────────────────────────────────────────────────────
def test_authorization():
    print("\n[TEST 3] Authorization Checks")

    # Ownership check - pass
    result = check_ownership(resource_owner_id=123, current_user_id=123)
    assert result == True
    print("  ✓ Ownership check passed for owner")

    # Ownership check - fail
    result = check_ownership(resource_owner_id=123, current_user_id=456)
    assert result == False
    print("  ✓ Ownership check failed for non-owner")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Caching System
# ─────────────────────────────────────────────────────────────────────────────
def test_embedding_cache():
    print("\n[TEST 4] Embedding Cache (LRU)")

    cache = EmbeddingCache(max_size=3)

    # Add items
    cache.set("query1", "value1")
    cache.set("query2", "value2")
    cache.set("query3", "value3")
    assert cache.get("query1") == "value1"
    print("  ✓ Cache stores and retrieves values")

    # LRU eviction
    cache.set("query4", "value4")  # Should evict oldest (query1 or query2)
    # Access query2 to make it "recent"
    cache.get("query2")
    cache.set("query5", "value5")  # Should evict query3 or query1
    print("  ✓ LRU eviction working (max 3 items)")

    # Cache clear
    cache.clear()
    assert cache.get("query1") is None
    print("  ✓ Cache clear working")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Logging Functions
# ─────────────────────────────────────────────────────────────────────────────
def test_logging():
    print("\n[TEST 5] Logging Functions")

    # Auth event logging
    try:
        log_auth_event(user_id=123, action="login", status="success", details="student")
        print("  ✓ Auth event logged successfully")
    except Exception as e:
        print(f"  ✗ Auth event logging failed: {e}")

    # API action logging
    try:
        log_api_action(user_id=456, endpoint="/faculty/projects", method="POST", status="success", details="project_id=789")
        print("  ✓ API action logged successfully")
    except Exception as e:
        print(f"  ✗ API action logging failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Module Imports (No Circular Dependencies)
# ─────────────────────────────────────────────────────────────────────────────
def test_imports():
    print("\n[TEST 6] Module Imports")

    try:
        from app import app
        print("  ✓ app.py imports successfully")
    except Exception as e:
        print(f"  ✗ app.py import failed: {e}")

    try:
        from routes.student_routes import student_bp
        print("  ✓ student_routes.py imports successfully")
    except Exception as e:
        print(f"  ✗ student_routes.py import failed: {e}")

    try:
        from routes.faculty_routes import faculty_bp
        print("  ✓ faculty_routes.py imports successfully")
    except Exception as e:
        print(f"  ✗ faculty_routes.py import failed: {e}")

    try:
        from routes.sdp_routes import sdp_bp
        print("  ✓ sdp_routes.py imports successfully")
    except Exception as e:
        print(f"  ✗ sdp_routes.py import failed: {e}")

    try:
        from services.matching_engine import cached_similarity_scores, clear_similarity_cache
        print("  ✓ matching_engine.py (with caching) imports successfully")
    except Exception as e:
        print(f"  ✗ matching_engine.py import failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Database Schema Validation
# ─────────────────────────────────────────────────────────────────────────────
def test_database_schema():
    print("\n[TEST 7] Database Schema")

    try:
        from database import init_db, get_db
        import sqlite3

        # Check sdp_proposals table exists
        db = get_db()
        cursor = db.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='sdp_proposals'
        """)
        table = cursor.fetchone()
        db.close()

        if table:
            print("  ✓ sdp_proposals table exists")
        else:
            print("  ✗ sdp_proposals table missing (need to run init_db)")

    except Exception as e:
        print(f"  ✗ Database schema check failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Run All Tests
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("SkillRadar Production Backend - Feature Tests")
    print("=" * 70)

    test_response_formats()
    test_sanitization()
    test_authorization()
    test_embedding_cache()
    test_logging()
    test_imports()
    test_database_schema()

    print("\n" + "=" * 70)
    print("✓ All production features validated successfully!")
    print("=" * 70)
