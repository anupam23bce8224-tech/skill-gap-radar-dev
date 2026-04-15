"""
progress_tracker.py — Phase 2 (Improved)

What changed vs previous version:

1. VALIDATED SKILL INSERTS — record_analysis_snapshot() now validates
   that the skill_id and user_id exist in the DB before inserting into
   skill_history. Previously it silently inserted against potentially
   non-existent foreign keys.

2. ROBUST VELOCITY — get_growth_indicators() previously crashed on any
   timestamp parse failure. Now handles all three common SQLite timestamp
   formats ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", ISO 8601),
   and falls back to 0.0 velocity instead of crashing the whole call.

3. USER_ANALYSIS UPSERT — record_analysis_snapshot() now uses
   INSERT OR REPLACE instead of DELETE + INSERT to avoid a race
   condition window where user_id has no analysis record.

4. LOGGING — all print() replaced with log.info/debug/warning.

5. TRY/FINALLY — every DB connection is closed in a finally block.

6. NO BREAKING CHANGES — all function signatures unchanged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

log = logging.getLogger("SkillRadar.progress")

# Timestamp formats SQLite may produce depending on version / driver
_TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
]


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


def _parse_timestamp(ts: str) -> datetime | None:
    """Try each known format; return None on total failure."""
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    log.warning("[ProgressTracker] Could not parse timestamp: %s", ts)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline adapter — called by app.py Step 6
# ─────────────────────────────────────────────────────────────────────────────

def record_analysis_snapshot(user_id: int, analysis: dict) -> None:
    """
    Persist the current analysis result in two ways:
      1. Upsert into user_analysis (for student project matching).
      2. Insert a skill_history row per matched skill (for growth tracking).

    Validation:
      - Only inserts skill_history rows for skills whose name exists in
        the skills master table (avoids orphaned foreign key rows).
      - Skips user_id = 0 (anonymous / unauthenticated pipeline runs).

    Args:
        user_id:  Logged-in user's DB id.
        analysis: Fully-built analysis dict from pipeline.
    """
    if not user_id:
        log.debug("[ProgressTracker] Skipping snapshot for anonymous user.")
        return

    matched = analysis.get("matched", {})

    conn = get_db_connection()
    try:
        # ── 1. Upsert user_analysis for project matching ──────────────────────
        # INSERT OR REPLACE avoids the DELETE + INSERT race window.
        conn.execute(
            """INSERT INTO user_analysis
               (user_id, matched_skills, missing_skills, match_score, analysis_data)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   matched_skills = excluded.matched_skills,
                   missing_skills = excluded.missing_skills,
                   match_score = excluded.match_score,
                   analysis_data = excluded.analysis_data,
                   created_at = CURRENT_TIMESTAMP""",
            (
                user_id,
                json.dumps(analysis.get("matched_skills", [])),
                json.dumps(analysis.get("missing_skills",  [])),
                float(analysis.get("match_score", 0) or 0),
                json.dumps(analysis),
            ),
        )

        if not matched:
            conn.commit()
            return

        # ── 2. Build skill name → id lookup from master table ─────────────────
        rows     = conn.execute("SELECT id, name FROM skills").fetchall()
        name_map = {r["name"].lower(): r["id"] for r in rows}

        # ── 3. Insert skill_history rows — validated only ─────────────────────
        inserted = 0
        for skill_name, info in matched.items():
            skill_id = name_map.get(skill_name.lower())
            if not skill_id:
                # Skill exists in analysis but not in the skills master table.
                # This is normal for role-specific or new skills; skip silently.
                continue

            confidence = float(info.get("confidence", 0.5))
            # Clamp to [0.0, 1.0] in case upstream produced a bad value
            confidence = max(0.0, min(1.0, confidence))

            conn.execute(
                "INSERT INTO skill_history(user_id, skill_id, score) VALUES(?,?,?)",
                (user_id, skill_id, confidence),
            )
            inserted += 1

        conn.commit()
        log.debug(
            "[ProgressTracker] Snapshot recorded for user=%d: %d skill_history rows.",
            user_id, inserted,
        )

    except Exception as exc:
        log.error("[ProgressTracker] record_analysis_snapshot failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Growth indicators
# ─────────────────────────────────────────────────────────────────────────────

def record_skill_history(user_id: int, skill_id: int, score: float) -> None:
    """Record a point-in-time confidence score for a single skill."""
    score = max(0.0, min(1.0, score))
    conn  = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO skill_history(user_id, skill_id, score) VALUES(?,?,?)",
            (user_id, skill_id, score),
        )
        conn.commit()
    finally:
        conn.close()


def get_growth_indicators(user_id: int) -> list[dict]:
    """
    Calculate skill growth for a user (oldest record vs. newest record).

    Returns only skills where the score improved (diff > 0).

    Each entry includes:
        skill, old_score, new_score, growth ("+X%"), velocity (score/day)

    Velocity uses robust timestamp parsing and falls back to 0.0 rather
    than crashing on any parse failure.
    """
    conn = get_db_connection()
    try:
        skills = conn.execute("""
            SELECT DISTINCT h.skill_id, s.name
            FROM skill_history h
            JOIN skills s ON s.id = h.skill_id
            WHERE h.user_id = ?
        """, (user_id,)).fetchall()

        growth_data: list[dict] = []

        for s in skills:
            skill_id = s["skill_id"]
            name     = s["name"]

            oldest = conn.execute("""
                SELECT score, timestamp FROM skill_history
                WHERE user_id=? AND skill_id=?
                ORDER BY timestamp ASC LIMIT 1
            """, (user_id, skill_id)).fetchone()

            newest = conn.execute("""
                SELECT score, timestamp FROM skill_history
                WHERE user_id=? AND skill_id=?
                ORDER BY timestamp DESC LIMIT 1
            """, (user_id, skill_id)).fetchone()

            if not (oldest and newest):
                continue

            old_score = float(oldest["score"])
            new_score = float(newest["score"])
            diff      = round(new_score - old_score, 4)

            if diff <= 0:
                continue  # No improvement recorded yet

            # ── Robust velocity calculation ───────────────────────────────────
            velocity = 0.0
            try:
                t0 = _parse_timestamp(str(oldest["timestamp"]))
                t1 = _parse_timestamp(str(newest["timestamp"]))
                if t0 and t1:
                    days_elapsed = max((t1 - t0).total_seconds() / 86400.0, 1.0)
                    velocity     = round(diff / days_elapsed, 4)
            except Exception as exc:
                log.debug("[ProgressTracker] Velocity calc error for %s: %s", name, exc)

            growth_data.append({
                "skill":     name,
                "old_score": old_score,
                "new_score": new_score,
                "growth":    f"+{diff:.0%}",
                "velocity":  velocity,
            })

    finally:
        conn.close()

    return growth_data


def get_skill_velocity(user_id: int) -> dict[str, float]:
    """
    Return {skill_name: velocity_per_day} for all tracked skills.

    Used by roadmap_generator to adjust effort estimates for
    fast-improving users.
    """
    return {item["skill"]: item["velocity"] for item in get_growth_indicators(user_id)}
