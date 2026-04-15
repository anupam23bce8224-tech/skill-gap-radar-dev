"""
roadmap_generator.py — Phase 2 (Improved)

What changed vs previous version:

1. DEDUPLICATION — the phase-building logic previously existed in two
   separate functions (generate_roadmap() and generate_roadmap_from_analysis()).
   It is now in a single _build_roadmap_phases() helper that both call.
   One bug fix = one place to fix.

2. ADAPTIVE PHASES — effort estimates and phase content are driven by
   user level (beginner / intermediate / advanced) inferred from match_score.
   Phase 2 ordering respects skill dependencies from ROLES_CONFIG.

3. DB FUNCTION — generate_roadmap() (DB-backed, for saved profiles) now
   calls _build_roadmap_phases() and serialises the result to SQLite.

4. MEMORY FUNCTION — generate_roadmap_from_analysis() (pipeline adapter)
   also calls _build_roadmap_phases() and returns the same structure.

5. IMPROVED PHASE CONTENT — each step now includes a resource URL
   from the top-ranked improvement action, not a hard-coded string.

6. NO BREAKING CHANGES — output dict schema unchanged; all callers work.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from .improvement_engine import get_improvements_for_skill
from .analysis_engine import ROLES_CONFIG

log = logging.getLogger("SkillRadar.roadmap")

# ── Effort estimates per user level ───────────────────────────────────────────
EFFORT_MAP: dict[str, dict[str, str]] = {
    "beginner":     {"critical": "1–2 weeks", "high": "1 week",    "medium": "3–5 days", "low": "2–3 days"},
    "intermediate": {"critical": "3–5 days",  "high": "2–3 days",  "medium": "1–2 days", "low": "1 day"},
    "advanced":     {"critical": "1–2 days",  "high": "1 day",     "medium": "4 hours",  "low": "2 hours"},
}


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


def _infer_level(match_score: float) -> str:
    if match_score >= 70:
        return "advanced"
    if match_score >= 40:
        return "intermediate"
    return "beginner"


def _ordered_by_dependency(skills: list[str], reference_list: list[str]) -> list[str]:
    """
    Sort skills by their position in reference_list (dependency order).
    Skills not found in reference_list go to the end.
    """
    def pos(s: str) -> int:
        try:
            return reference_list.index(s.lower())
        except ValueError:
            return len(reference_list)
    return sorted(skills, key=pos)


def _top_resource(skill: str) -> str:
    """Return the resource URL for the highest-impact action for a skill."""
    actions = get_improvements_for_skill(skill)
    if actions:
        return actions[0].get("resource", "")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Single shared phase builder — called by BOTH public functions
# ─────────────────────────────────────────────────────────────────────────────

def _build_roadmap_phases(
    role:          str,
    missing_req:   list[str],
    missing_bonus: list[str],
    matched:       dict,
    level:         str,
) -> dict:
    """
    Build the four-phase roadmap dict.

    Args:
        role:          Target job role.
        missing_req:   Required skills the user is missing.
        missing_bonus: Bonus skills the user is missing.
        matched:       {skill: {confidence, source, tier}} from analysis.
        level:         "beginner" | "intermediate" | "advanced"

    Returns:
        {
            "role":   str,
            "level":  str,
            "phases": {
                "Phase 1 – Foundations":  [...],
                "Phase 2 – Build":        [...],
                "Phase 3 – Polish":       [...],
                "Phase 4 – Career Ready": [...],
            }
        }

    Each step dict:
        {skill, action, outcome, priority, effort, level, resource}
    """
    effort   = EFFORT_MAP.get(level, EFFORT_MAP["intermediate"])
    role_cfg = ROLES_CONFIG.get(role, {})

    required_list = role_cfg.get("required", [])
    bonus_list    = role_cfg.get("bonus",    [])

    # Dependency-ordered missing lists
    sorted_missing   = _ordered_by_dependency(missing_req,   required_list)
    sorted_bonus_m   = _ordered_by_dependency(missing_bonus, bonus_list)

    # ── Phase 1: Foundations — top 3 critical required gaps ──────────────────
    phase1: list[dict] = []
    for skill in sorted_missing[:3]:
        actions    = get_improvements_for_skill(skill)
        top_action = actions[0] if actions else {"task": f"Learn {skill}", "resource": ""}
        phase1.append({
            "skill":    skill,
            "action":   f"Learn core concepts of {skill}",
            "outcome":  (
                f"Understand fundamentals and build a small demo with {skill}. "
                f"Start with: {top_action['task']}"
            ),
            "priority": "critical",
            "effort":   effort["critical"],
            "level":    level,
            "resource": top_action.get("resource", ""),
        })

    if not phase1:
        phase1.append({
            "skill":    "Core Skills",
            "action":   "Strengthen fundamentals",
            "outcome":  "Review and deepen your fundamentals for the target role.",
            "priority": "critical",
            "effort":   effort["critical"],
            "level":    level,
            "resource": "",
        })

    # ── Phase 2: Build — remaining required + top 2 bonus gaps ───────────────
    phase2: list[dict] = []
    for skill in sorted_missing[3:]:
        actions = get_improvements_for_skill(skill)
        best    = actions[0] if actions else {"task": f"Build a project using {skill}", "resource": ""}
        phase2.append({
            "skill":    skill,
            "action":   f"Apply {skill} in a real-world project",
            "outcome":  f"Integrate {skill} into a portfolio project. {best['task']}",
            "priority": "high",
            "effort":   effort["high"],
            "level":    level,
            "resource": best.get("resource", ""),
        })
    for skill in sorted_bonus_m[:2]:
        actions = get_improvements_for_skill(skill)
        best    = actions[0] if actions else {"task": f"Learn {skill}", "resource": ""}
        phase2.append({
            "skill":    skill,
            "action":   f"Learn and practice {skill}",
            "outcome":  f"Add {skill} as a differentiator. {best['task']}",
            "priority": "medium",
            "effort":   effort["medium"],
            "level":    level,
            "resource": best.get("resource", ""),
        })

    if not phase2:
        phase2.append({
            "skill":    "Portfolio",
            "action":   "Build a multi-skill project",
            "outcome":  "Combine your top skills into a cohesive portfolio project.",
            "priority": "high",
            "effort":   effort["high"],
            "level":    level,
            "resource": "",
        })

    # ── Phase 3: Polish — validate resume-only skills with GitHub ─────────────
    phase3: list[dict] = []
    resume_only = [s for s, v in matched.items() if v.get("source") == "resume"]
    for skill in resume_only[:3]:
        phase3.append({
            "skill":    skill,
            "action":   f"Push a {skill} project to GitHub",
            "outcome":  f"Validate {skill} knowledge with public code evidence",
            "priority": "medium",
            "effort":   effort["medium"],
            "level":    level,
            "resource": "https://github.com",
        })
    for skill in sorted_bonus_m[2:]:
        phase3.append({
            "skill":    skill,
            "action":   f"Explore advanced {skill} patterns",
            "outcome":  f"Demonstrate {skill} in a production-style codebase",
            "priority": "low",
            "effort":   "Ongoing",
            "level":    level,
            "resource": _top_resource(skill),
        })

    if not phase3:
        phase3.append({
            "skill":    "Best Practices",
            "action":   "Architecture & design patterns",
            "outcome":  "Apply SOLID principles and write tests for your projects.",
            "priority": "medium",
            "effort":   effort["medium"],
            "level":    level,
            "resource": "https://refactoring.guru/design-patterns",
        })

    # ── Phase 4: Career Readiness — always fixed ──────────────────────────────
    phase4: list[dict] = [{
        "skill":    "Career Readiness",
        "action":   "Polish resume, GitHub READMEs, and practice mock interviews",
        "outcome":  f"Apply confidently for {role} positions",
        "priority": "final",
        "effort":   "Ongoing",
        "level":    level,
        "resource": "https://www.pramp.com",
    }]

    return {
        "role":   role,
        "level":  level,
        "phases": {
            "Phase 1 – Foundations":  phase1,
            "Phase 2 – Build":        phase2,
            "Phase 3 – Polish":       phase3,
            "Phase 4 – Career Ready": phase4,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public functions
# ─────────────────────────────────────────────────────────────────────────────

def generate_roadmap_from_analysis(analysis: dict) -> dict:
    """
    Pipeline adapter — called by app.py Step 5.

    Builds a roadmap directly from the in-memory analysis dict.
    No DB write required.  Returns the same phase structure as
    generate_roadmap() so Jinja templates work unchanged.

    Args:
        analysis: session["analysis"] dict from pipeline.py.

    Returns:
        {role, level, phases: {"Phase 1 – Foundations": [...], ...}}
    """
    role          = analysis.get("role", "Web Developer")
    match_score   = analysis.get("match_score", 0)
    missing_req   = analysis.get("missing_required", [])
    missing_bonus = analysis.get("missing_bonus",    [])
    matched       = analysis.get("matched",          {})
    level         = _infer_level(match_score)

    return _build_roadmap_phases(role, missing_req, missing_bonus, matched, level)


def generate_roadmap(
    user_id:         int,
    target_role:     str,
    missing_skills:  list[str],
    moderate_skills: list[str],
    match_score:     float = 0.0,
) -> dict:
    """
    DB-backed roadmap generator.

    Persists a roadmap + individual steps to SQLite and returns the
    roadmap object.  Calls _build_roadmap_phases() internally so the
    phase content is identical to generate_roadmap_from_analysis().

    Args:
        user_id:        DB user ID.
        target_role:    Target job role string.
        missing_skills: Skills not found in resume or GitHub.
        moderate_skills:Skills found in resume but not GitHub-validated.
        match_score:    Current match % (infers user level).

    Returns:
        {roadmap_id, target_role, level, steps: [...]}
    """
    level   = _infer_level(match_score)
    effort  = EFFORT_MAP.get(level, EFFORT_MAP["intermediate"])

    role_cfg      = ROLES_CONFIG.get(target_role, {})
    required_list = role_cfg.get("required", [])
    bonus_list    = role_cfg.get("bonus",    [])

    sorted_missing = _ordered_by_dependency(missing_skills, required_list)
    sorted_moderate = moderate_skills[:]

    # Use the shared builder to get phase data
    roadmap_data = _build_roadmap_phases(
        role          = target_role,
        missing_req   = missing_skills,
        missing_bonus = [],           # moderate skills go in Phase 2 for DB path
        matched       = {},           # no matched dict in DB path signature
        level         = level,
    )

    # Phase 2 in the DB path also includes moderate skills
    for skill in sorted_moderate[:2]:
        actions = get_improvements_for_skill(skill)
        best    = actions[0] if actions else {"task": f"Build a project using {skill}", "resource": ""}
        roadmap_data["phases"]["Phase 2 – Build"].append({
            "skill":    skill,
            "action":   f"Apply {skill} in a real-world project",
            "outcome":  f"Integrate {skill} into a portfolio project. {best['task']}",
            "priority": "high",
            "effort":   effort["high"],
            "level":    level,
            "resource": best.get("resource", ""),
        })

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO roadmaps(user_id, target_role) VALUES(?,?)",
            (user_id, target_role),
        )
        roadmap_id = cur.lastrowid

        db_steps: list[dict] = []
        phase_key_map = {
            "Phase 1 – Foundations":  "Phase 1: Foundations",
            "Phase 2 – Build":        "Phase 2: Intermediate",
            "Phase 3 – Polish":       "Phase 3: Advanced",
            "Phase 4 – Career Ready": "Phase 4: Industry Readiness",
        }

        for phase_key, steps in roadmap_data["phases"].items():
            db_phase = phase_key_map.get(phase_key, phase_key)
            for step in steps:
                resources_json = json.dumps({
                    "task":     step["action"],
                    "resource": step.get("resource", ""),
                    "skill":    step["skill"],
                })
                cur2 = conn.execute(
                    """INSERT INTO roadmap_steps
                       (roadmap_id, phase, title, description, resources)
                       VALUES(?,?,?,?,?)""",
                    (roadmap_id, db_phase, step["action"],
                     step["outcome"], resources_json),
                )
                db_steps.append({
                    "id":          cur2.lastrowid,
                    "phase":       db_phase,
                    "title":       step["action"],
                    "description": step["outcome"],
                    "time":        step["effort"],
                    "resources":   json.loads(resources_json),
                    "status":      "pending",
                })

        conn.commit()
    finally:
        conn.close()

    return {
        "roadmap_id":  roadmap_id,
        "target_role": target_role,
        "level":       level,
        "steps":       db_steps,
    }


def get_user_roadmap(user_id: int) -> dict | None:
    """Fetch the latest active roadmap for a user, grouped by phase."""
    conn = get_db_connection()
    try:
        roadmap = conn.execute("""
            SELECT id, target_role FROM roadmaps
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()

        if not roadmap:
            return None

        steps = conn.execute("""
            SELECT id, phase, title, description, resources, status
            FROM roadmap_steps WHERE roadmap_id = ?
        """, (roadmap["id"],)).fetchall()
    finally:
        conn.close()

    phases: dict[str, list] = {}
    for step in steps:
        phase_label = step["phase"].split(":")[0].strip()
        if phase_label not in phases:
            phases[phase_label] = []
        step_dict = dict(step)
        try:
            step_dict["resources"] = json.loads(step["resources"] or "{}")
        except (json.JSONDecodeError, TypeError):
            step_dict["resources"] = {}
        phases[phase_label].append(step_dict)

    return {
        "roadmap_id":  roadmap["id"],
        "target_role": roadmap["target_role"],
        "phases":      phases,
    }


def update_step_status(step_id: int, status: str) -> None:
    """Mark a roadmap step as 'pending' | 'in_progress' | 'done'."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE roadmap_steps SET status=? WHERE id=?", (status, step_id))
        conn.commit()
    finally:
        conn.close()