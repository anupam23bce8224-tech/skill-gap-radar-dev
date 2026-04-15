# SkillRadar Production API Documentation

## Base URL

```
http://localhost:5000
```

## Authentication

All endpoints require a valid session with `user_id` and `role` set.

### Session Setup

```python
session['user_id'] = 123
session['role'] = 'student'  # or 'faculty'
```

---

## Response Format

### Success (200, 201)

```json
{
  "success": true,
  "data": {},
  "error": null,
  "message": "Success message"
}
```

### Error (400, 401, 403, 404, 500)

```json
{
  "success": false,
  "data": null,
  "error": "Error description"
}
```

---

## STUDENT ENDPOINTS

### GET /student/projects

List all open projects with AI-matched skills.

**Authentication**: Required (student)
**Authorization**: N/A
**Caching**: ✅ Uses similarity score cache
**Validation**: ✅ Input sanitization

**Response**:

```json
{
  "success": true,
  "data": {
    "projects": [
      {
        "id": 1,
        "title": "Backend API Development",
        "description": "Build RESTful API...",
        "faculty_name": "Dr. Smith",
        "required_skills": ["Python", "Flask", "PostgreSQL"],
        "posted_at": "2024-04-12T10:00:00",
        "matched_skills": [
          { "name": "Python", "confidence": 90, "source": "resume" }
        ],
        "missing_skills": ["Flask"],
        "match_score": 67
      }
    ]
  }
}
```

---

### POST /student/apply/<project_id>

Apply to a project with automatic skill matching.

**Authentication**: Required (student)
**Authorization**: Ownership of application (implicit)
**Duplicate Prevention**: ✅ UNIQUE(project_id, student_id)
**Validation**: ✅ Required fields, length bounds
**Caching**: ✅ Uses similarity score cache

**Request**:

```json
{
  "project_idea": "I want to build scalable auto infrastructure...",
  "interest_statement": "I'm passionate about distributed systems..."
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "application_id": 42,
    "matched_skills": [
      { "name": "Python", "confidence": 85, "source": "resume" }
    ],
    "missing_skills": ["Docker"],
    "match_score": 75,
    "status": "pending"
  },
  "message": "Application submitted successfully"
}
```

**Error Cases**:

- 400: Missing fields, empty strings, invalid types
- 400: Already applied to this project
- 404: Project not found or closed
- 401: Not authenticated
- 403: Not a student

---

## FACULTY ENDPOINTS

### GET /faculty/projects

List all projects created by faculty.

**Authentication**: Required (faculty)
**Authorization**: Only own projects
**Validation**: ✅ Result sanitization

**Response**:

```json
{
  "success": true,
  "data": {
    "projects": [
      {
        "id": 1,
        "title": "Backend API Development",
        "description": "...",
        "required_skills": ["Python", "Flask"],
        "posted_at": "2024-04-12T10:00:00",
        "status": "open",
        "application_count": 5,
        "accepted_count": 2
      }
    ]
  }
}
```

---

### POST /faculty/projects

Create a new project.

**Authentication**: Required (faculty)
**Authorization**: N/A (owns by creating)
**Validation**: ✅ Title, description, skills required

**Request**:

```json
{
  "title": "Backend API Development",
  "description": "Build scalable RESTful API with 20+ endpoints, database design, rate limiting...",
  "required_skills": ["Python", "Flask", "PostgreSQL", "Redis"]
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "project_id": 42,
    "title": "Backend API Development",
    "description": "...",
    "required_skills": ["Python", "Flask", "PostgreSQL", "Redis"],
    "status": "open"
  },
  "message": "Project created successfully"
}
```

**Validation Rules**:

- `title`: 5-200 characters, required
- `description`: 20-2000 characters, required
- `required_skills`: List of 1+ non-empty strings

---

### GET /faculty/matches/<project_id>

Get detailed skill matches for all applicants.

**Authentication**: Required (faculty)
**Authorization**: ✅ Own project only
**Caching**: ✅ Uses similarity score cache

**Response**:

```json
{
  "success": true,
  "data": {
    "project_id": 1,
    "required_skills": ["Python", "Flask", "PostgreSQL"],
    "applications": [
      {
        "application_id": 42,
        "student_id": 10,
        "student_name": "John Doe",
        "project_idea": "I want to...",
        "interest_statement": "I'm passionate about...",
        "matched_skills": [
          { "name": "Python", "confidence": 90, "source": "resume" },
          {
            "name": "Flask",
            "confidence": 75,
            "source": "semantic_match",
            "matched_via": "Django"
          }
        ],
        "missing_skills": ["PostgreSQL"],
        "match_score": 67,
        "status": "pending"
      }
    ]
  }
}
```

**Error Cases**:

- 404: Project not found
- 403: Not project owner

---

### GET /faculty/applications/<project_id>

List all applications for a project.

**Authentication**: Required (faculty)
**Authorization**: ✅ Own project only

**Response**:

```json
{
  "success": true,
  "data": {
    "project_id": 1,
    "applications": [
      {
        "id": 42,
        "student_id": 10,
        "student_name": "John Doe",
        "student_skills": [...],
        "project_idea": "...",
        "interest_statement": "...",
        "match_score": 75,
        "status": "pending",
        "applied_at": "2024-04-12T15:30:00"
      }
    ]
  }
}
```

---

### PATCH /faculty/applications/<application_id>/status

Update application status.

**Authentication**: Required (faculty)
**Authorization**: ✅ Own project/application only
**Validation**: ✅ Status must be: pending, accepted, rejected

**Request**:

```json
{
  "status": "accepted"
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "application_id": 42,
    "status": "accepted"
  },
  "message": "Application status updated successfully"
}
```

**Status Values**:

- `pending` - Initial state, waiting for review
- `accepted` - Student accepted to project
- `rejected` - Student rejected for project

---

## SDP ENDPOINTS

### GET /get_proposals

List SDP proposals.

**Authentication**: Required
**Authorization**:

- Faculty: sees all proposals
- Students: see own + approved

**Response**:

```json
{
  "success": true,
  "data": {
    "proposals": [
      {
        "id": 1,
        "title": "AI-Powered Skill Gap Analyzer",
        "description": "Platform that uses AI to...",
        "objectives": ["Implement resume parsing", "Build matching engine"],
        "technologies": ["Python", "PyTorch", "PostgreSQL"],
        "team_size": 4,
        "duration": "6 months",
        "submitted_by": 15,
        "submitter_name": "Jane Smith",
        "status": "approved",
        "submitted_at": "2024-04-12T12:00:00"
      }
    ]
  }
}
```

---

### POST /submit_proposal

Submit a new SDP proposal.

**Authentication**: Required (student)
**Authorization**: N/A (student owns by creating)
**Duplicate Prevention**: ✅ Only 1 pending per student
**Validation**: ✅ All fields required, length bounds

**Request**:

```json
{
  "title": "AI-Powered Skill Gap Analyzer",
  "description": "A comprehensive platform that uses machine learning to analyze resumes...",
  "objectives": [
    "Implement semantic skill extraction",
    "Build AI matching engine",
    "Create analytics dashboard"
  ],
  "technologies": ["Python", "PyTorch", "PostgreSQL", "React"],
  "team_size": 4,
  "duration": "6 months"
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "proposal_id": 100,
    "title": "AI-Powered Skill Gap Analyzer",
    "description": "...",
    "objectives": [...],
    "technologies": [...],
    "team_size": 4,
    "duration": "6 months",
    "status": "pending"
  },
  "message": "Proposal submitted successfully"
}
```

**Validation Rules**:

- `title`: 5-200 characters, required
- `description`: 20-3000 characters, required
- `objectives`: List of 1+ non-empty strings
- `technologies`: List of 1+ non-empty strings
- `team_size`: 1-10, required
- `duration`: Non-empty string, required

**Error Cases**:

- 400: Already have pending proposal
- 400: Validation failure
- 401: Not authenticated
- 403: Not a student

---

### POST /proposal/approve

Approve a proposal.

**Authentication**: Required (faculty)
**Authorization**: N/A (any faculty can approve)
**Validation**: ✅ Proposal must exist and be pending

**Request**:

```json
{
  "proposal_id": 100
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "proposal_id": 100,
    "status": "approved"
  },
  "message": "Proposal approved successfully"
}
```

**Error Cases**:

- 404: Proposal not found
- 400: Proposal already approved or rejected
- 401: Not authenticated
- 403: Not faculty

---

### POST /proposal/reject

Reject a proposal.

**Authentication**: Required (faculty)
**Authorization**: N/A (any faculty can reject)
**Validation**: ✅ Proposal must exist and be pending

**Request**:

```json
{
  "proposal_id": 100
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "proposal_id": 100,
    "status": "rejected"
  },
  "message": "Proposal rejected successfully"
}
```

---

## ERROR CODES

| Code | Meaning      | Example                            |
| ---- | ------------ | ---------------------------------- |
| 400  | Bad Request  | Invalid input, validation failed   |
| 401  | Unauthorized | Not authenticated                  |
| 403  | Forbidden    | Authenticated but access denied    |
| 404  | Not Found    | Resource doesn't exist             |
| 500  | Server Error | Database error, unexpected failure |

---

## SECURITY FEATURES

### 1. Authentication

- All endpoints require valid `session['user_id']`
- Returns 401 if missing

### 2. Authorization

- Faculty can only access own projects/applications
- Students can only access own applications/proposals
- Returns 403 for unauthorized access

### 3. Input Validation

- All strings bounded by length
- Required fields checked
- Data types validated
- Returns 400 for validation failures

### 4. Duplicate Prevention

- Applications: UNIQUE(project_id, student_id)
- Proposals: Only 1 pending per student

### 5. Error Handling

- Graceful failure with descriptive errors
- Proper HTTP status codes
- DB transaction rollback on failure

### 6. Logging

- Auth events logged: `[AUTH_EVENT]`
- API actions logged: `[API_ACTION]`
- Authorization failures logged
- Validation errors logged

### 7. Performance

- Similarity scores cached (LRU, max 500 entries)
- Prevents redundant computations
- Ready for Redis upgrade

---

## TESTING

Run production feature tests:

```bash
python test_production_features.py
```

Expected output:

```
✓ All production features validated successfully!
```

---

## DEPLOYMENT

See `PRODUCTION_BACKEND.md` for complete deployment checklist.

1. Backup database
2. Set GROQ_API_KEY environment variable
3. Run Flask app: `python app.py`
4. Test endpoints with proper authentication
5. Use Gunicorn for production: `gunicorn -w 4 app:app`
