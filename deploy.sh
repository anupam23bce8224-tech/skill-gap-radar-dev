#!/bin/bash
# Production Backend Setup & Deployment Guide

echo "=========================================="
echo "SkillRadar Production Backend Setup"
echo "=========================================="

# Step 1: Verify Python environment
echo -e "\n[1] Checking Python environment..."
python --version

# Step 2: Check all imports
echo -e "\n[2] Validating all module imports..."
export GROQ_API_KEY=dummy
python -c "
from app import app
from utils import *
from routes.student_routes import student_bp
from routes.faculty_routes import faculty_bp
from routes.sdp_routes import sdp_bp
from services.matching_engine import cached_similarity_scores
print('✓ All modules imported successfully')
"

# Step 3: Run production feature tests
echo -e "\n[3] Running production feature tests..."
python test_production_features.py

# Step 4: Initialize database
echo -e "\n[4] Initializing production database..."
python -c "
from database import init_db
init_db()
print('✓ Database initialized')
"

# Step 5: Start Flask server
echo -e "\n[5] Starting production server..."
echo "Server starting on http://localhost:5000"
echo "Credentials required for all endpoints"
echo ""
export FLASK_ENV=production
export FLASK_DEBUG=0
python app.py

# Optional: Start with Gunicorn for production
# gunicorn -w 4 -b 0.0.0.0:8000 app:app
