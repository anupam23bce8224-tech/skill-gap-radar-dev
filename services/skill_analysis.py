"""
skill_analysis.py — Phase 2 (Improved)

Key improvements over previous version:

1. GAP CALCULATION — required vs bonus fully separated:
   - match_percentage computed from required skills ONLY
   - bonus skills scored independently as bonus_score (0-100)
   - bonus NEVER inflates or deflates the core gap score
   - safe division: _compute_match_percentage handles n_required == 0

2. SCORING — normalised, graduated confidence levels:
   - STRONG    : eff_conf >= 0.80 AND has GitHub source
   - MODERATE  : 0.50 <= eff_conf < 0.80  (or GitHub-present but low sim)
   - MISSING   : not found at all
   - github_boost reduced to +0.25 (was +0.30) to avoid over-inflating
     lightly-evidenced repos

3. SINGLE SOURCE OF TRUTH — _classify_required_skill() and
   _classify_bonus_skill() are shared between both gap calculators.
   Previously the same logic was duplicated across the two functions.

4. NO BREAKING CHANGES — output dict keys are identical to the
   previous version; all callers and templates work unchanged.
   New key: bonus_score (int 0-100) — additive, ignored by old callers.
"""

from __future__ import annotations

import logging
import os
import sqlite3

log = logging.getLogger("SkillRadar.skill_analysis")

# ── Confidence thresholds ─────────────────────────────────────────────────────
STRONG_THRESHOLD   = 0.80   # GitHub-verified, high semantic similarity
MODERATE_THRESHOLD = 0.50   # resume-present or partial GitHub signal

# Required skill weight (all required skills are equal)
REQUIRED_WEIGHT = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers — single source of truth for all scoring logic
# ─────────────────────────────────────────────────────────────────────────────

def _confidence_to_label(score: float) -> str:
    if score >= STRONG_THRESHOLD:
        return "High"
    if score >= MODERATE_THRESHOLD:
        return "Medium"
    return "Low"


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


def _effective_confidence(raw_conf: float, source: str) -> float:
    """
    Apply GitHub boost and cap at 1.0.
    github_boost = 0.25 (conservative — avoids over-inflating weak GitHub repos).
    """
    github_boost = 0.25 if "github" in source else 0.0
    return min(1.0, raw_conf + github_boost)


def _build_evidence(source: str, eff_conf: float) -> list[str]:
    ev: list[str] = []
    if "github" in source:
        ev.append("Verified in GitHub Projects")
    if "resume" in source:
        ev.append("Found in Resume")
    if not ev:
        ev.append("Low signal detected")
    return ev


def _classify_required_skill(
    skill: str,
    raw_conf: float,
    source: str,
    get_improvements_fn,
) -> tuple[str, dict]:
    """
    Classify a *found* required skill as 'strong' or 'moderate'.

    Returns:
        ("strong" | "moderate", skill_dict)
    """
    eff_conf  = _effective_confidence(raw_conf, source)
    evidence  = _build_evidence(source, eff_conf)
    score_pct = round(eff_conf * 100)

    if eff_conf >= STRONG_THRESHOLD and "github" in source:
        return "strong", {
            "name":         skill,
            "score":        score_pct,
            "confidence":   _confidence_to_label(eff_conf),
            "evidence":     evidence,
            "missing":      [],
            "improvements": [],
            "tier":         "required",
            "eff_conf":     round(eff_conf, 4),
        }

    # moderate — identify what's still missing
    gaps: list[str] = []
    if "github" not in source:
        gaps.append("GitHub project validation")
    if eff_conf < MODERATE_THRESHOLD:
        gaps.append("Stronger evidence (higher similarity match)")
    if not gaps:
        gaps.append("Practical implementation")

    return "moderate", {
        "name":         skill,
        "score":        score_pct,
        "confidence":   _confidence_to_label(eff_conf),
        "evidence":     evidence,
        "missing":      gaps,
        "improvements": get_improvements_fn(skill),
        "tier":         "required",
        "eff_conf":     round(eff_conf, 4),
    }


def _classify_bonus_skill(skill: str, raw_conf: float, source: str) -> dict:
    """Classify a found bonus skill. Always shown as a positive differentiator."""
    eff_conf = _effective_confidence(raw_conf, source)
    return {
        "name":         skill,
        "score":        round(eff_conf * 100),
        "confidence":   _confidence_to_label(eff_conf),
        "evidence":     _build_evidence(source, eff_conf),
        "missing":      [],
        "improvements": [],
        "tier":         "bonus",
        "eff_conf":     round(eff_conf, 4),
    }


def _compute_match_percentage(weighted_score: float, n_required: int) -> int:
    """
    Normalise weighted_score against the maximum possible required score.

    max_score = n_required × REQUIRED_WEIGHT
    Clamped to [0, 100] to guard against floating-point edge cases.
    """
    if n_required == 0:
        return 0
    max_score = float(n_required) * REQUIRED_WEIGHT
    return max(0, min(100, round((weighted_score / max_score) * 100.0)))


def _compute_bonus_score(found_bonus: list[dict], n_bonus: int) -> int:
    """
    Separate 0-100 score for bonus skills.

    Formula: avg_eff_conf × coverage_ratio × 100
    A user with no bonus skills scores 0 (not undefined).
    """
    if n_bonus == 0 or not found_bonus:
        return 0
    avg_conf = sum(s["eff_conf"] for s in found_bonus) / len(found_bonus)
    coverage = len(found_bonus) / n_bonus
    return max(0, min(100, round(avg_conf * coverage * 100)))


# ─────────────────────────────────────────────────────────────────────────────
# Public gap calculators
# ─────────────────────────────────────────────────────────────────────────────

def calculate_skill_gap(user_id: int, goal_role: str) -> dict | None:
    """
    DB-backed gap calculator for saved user profiles.

    Used by teacher dashboard, historical reports, or any flow where
    skills are already persisted in user_skills table.

    Returns:
        {strong, moderate, missing, match_percentage, bonus_score,
         gap_score, next_action}
        or None if role is unknown.
    """
    from .analysis_engine import ROLES_CONFIG
    from .improvement_engine import get_next_best_action, get_improvements_for_skill

    role_cfg = ROLES_CONFIG.get(goal_role)
    if not role_cfg:
        log.warning("[SkillAnalysis] Unknown role: %s", goal_role)
        return None

    required_skills = role_cfg["required"]
    bonus_skills    = role_cfg["bonus"]

    conn = get_db_connection()
    try:
        user_skill_rows = conn.execute("""
            SELECT s.name, us.score, us.source
            FROM user_skills us
            JOIN skills s ON s.id = us.skill_id
            WHERE us.user_id = ?
        """, (user_id,)).fetchall()
    finally:
        conn.close()

    user_map: dict[str, dict] = {
        r["name"].lower(): {"score": float(r["score"]), "source": r["source"]}
        for r in user_skill_rows
    }

    strong        = []
    moderate      = []
    missing_list  = []
    weighted_score = 0.0

    # Required skills — drive match_percentage
    for skill in required_skills:
        sk_lower = skill.lower()
        if sk_lower in user_map:
            row      = user_map[sk_lower]
            eff_conf = _effective_confidence(row["score"], row["source"])
            weighted_score += REQUIRED_WEIGHT * eff_conf
            category, skill_dict = _classify_required_skill(
                skill, row["score"], row["source"], get_improvements_for_skill
            )
            if category == "strong":
                strong.append(skill_dict)
            else:
                moderate.append(skill_dict)
        else:
            missing_list.append({
                "name":         skill,
                "score":        0,
                "confidence":   "Low",
                "evidence":     ["Not found in resume or GitHub"],
                "missing":      ["Core concepts", "Practical experience"],
                "improvements": get_improvements_for_skill(skill),
                "tier":         "required",
                "gap_weight":   round(REQUIRED_WEIGHT, 2),
                "eff_conf":     0.0,
            })

    # Bonus skills — separate scoring, never touches match_percentage
    found_bonus: list[dict] = []
    for skill in bonus_skills:
        sk_lower = skill.lower()
        if sk_lower in user_map:
            row = user_map[sk_lower]
            skill_dict = _classify_bonus_skill(skill, row["score"], row["source"])
            found_bonus.append(skill_dict)
            strong.append(skill_dict)

    match_pct   = _compute_match_percentage(weighted_score, len(required_skills))
    bonus_score = _compute_bonus_score(found_bonus, len(bonus_skills))

    missing_names = [m["name"] for m in missing_list]
    next_action   = get_next_best_action(missing_names, goal_role)

    return {
        "strong":           strong,
        "moderate":         moderate,
        "missing":          missing_list,
        "match_percentage": match_pct,
        "bonus_score":      bonus_score,
        "gap_score":        100 - match_pct,
        "next_action":      next_action,
    }


def calculate_skill_gap_from_analysis(analysis: dict) -> dict:
    """
    Pipeline adapter — called by app.py Step 3.

    Works entirely from the in-memory analysis dict (no DB hit).
    Uses the same _classify_*() helpers as calculate_skill_gap()
    so scoring is bit-for-bit identical across both paths.

    Args:
        analysis: session["analysis"] dict built by pipeline.py.

    Returns:
        {strong, moderate, missing, match_percentage, bonus_score, gap_score}
    """
    from .analysis_engine import ROLES_CONFIG
    from .improvement_engine import get_improvements_for_skill

    role     = analysis.get("role", "Web Developer")
    role_cfg = ROLES_CONFIG.get(role, {})

    required_skills = role_cfg.get("required", [])
    bonus_skills    = role_cfg.get("bonus",    [])
    matched         = analysis.get("matched",  {})  # {skill: {confidence, source, tier}}

    strong        = []
    moderate      = []
    missing_list  = []
    weighted_score = 0.0

    # Required skills
    for skill in required_skills:
        if skill in matched:
            info     = matched[skill]
            raw_conf = float(info.get("confidence", 0.5))
            source   = info.get("source", "resume")
            eff_conf = _effective_confidence(raw_conf, source)
            weighted_score += REQUIRED_WEIGHT * eff_conf
            category, skill_dict = _classify_required_skill(
                skill, raw_conf, source, get_improvements_for_skill
            )
            if category == "strong":
                strong.append(skill_dict)
            else:
                moderate.append(skill_dict)
        else:
            missing_list.append({
                "name":         skill,
                "score":        0,
                "confidence":   "Low",
                "evidence":     ["Not found in resume or GitHub"],
                "missing":      ["Core concepts", "Practical experience"],
                "improvements": get_improvements_for_skill(skill),
                "tier":         "required",
                "gap_weight":   round(REQUIRED_WEIGHT, 2),
                "eff_conf":     0.0,
            })

    # Bonus skills — separate, never touches match_percentage
    found_bonus: list[dict] = []
    for skill in bonus_skills:
        if skill in matched:
            info     = matched[skill]
            raw_conf = float(info.get("confidence", 0.5))
            source   = info.get("source", "resume")
            skill_dict = _classify_bonus_skill(skill, raw_conf, source)
            found_bonus.append(skill_dict)
            strong.append(skill_dict)

    match_pct   = _compute_match_percentage(weighted_score, len(required_skills))
    bonus_score = _compute_bonus_score(found_bonus, len(bonus_skills))

    return {
        "strong":           strong,
        "moderate":         moderate,
        "missing":          missing_list,
        "match_percentage": match_pct,
        "bonus_score":      bonus_score,
        "gap_score":        100 - match_pct,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PDF skill extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_skills_from_pdf(pdf_path: str) -> list[int]:
    """
    Extract DB skill IDs from a resume PDF.

    Priority: sentence-transformers → spaCy PhraseMatcher.
    """
    if not os.path.exists(pdf_path):
        log.warning("[SkillAnalysis] File not found: %s", pdf_path)
        return []

    try:
        from pdfminer.high_level import extract_text
        raw_text = extract_text(pdf_path)
        if not raw_text:
            log.warning("[SkillAnalysis] PDF returned empty text.")
            return []
        raw_text = " ".join(raw_text.split())
    except Exception as exc:
        log.error("[SkillAnalysis] PDF read error: %s", exc)
        return []

    conn = get_db_connection()
    try:
        db_skills = conn.execute("SELECT id, name FROM skills").fetchall()
    finally:
        conn.close()

    if not db_skills:
        return []

    skill_names  = [s["name"].lower() for s in db_skills]
    skill_id_map = {s["name"].lower(): s["id"] for s in db_skills}

    # AI path — semantic extraction
    try:
        from .embedding_engine import extract_skills_semantic
        matched   = extract_skills_semantic(raw_text.lower(), skill_names, threshold=0.40)
        found_ids = [skill_id_map[n] for n in matched if n in skill_id_map]
        log.info("[SkillAnalysis] Semantic extraction: %d skills from PDF.", len(found_ids))
        return found_ids
    except Exception as exc:
        log.warning("[SkillAnalysis] Semantic extraction failed (%s), using spaCy fallback.", exc)

    # spaCy fallback
    try:
        import spacy
        from spacy.matcher import PhraseMatcher
        nlp     = spacy.load("en_core_web_sm")
        matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
        matcher.add("SKILLS", [nlp.make_doc(n) for n in skill_names])
        doc       = nlp(raw_text)
        found_ids = set()
        for _, start, end in matcher(doc):
            name = doc[start:end].text.lower().strip()
            if name in skill_id_map:
                found_ids.add(skill_id_map[name])
        return list(found_ids)
    except Exception as exc:
        log.error("[SkillAnalysis] spaCy fallback also failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def save_user_skills(
    user_id: int,
    skill_ids: list[int],
    source: str = "resume",
) -> None:
    """Upsert extracted skill IDs into user_skills table."""
    conn = get_db_connection()
    try:
        for sid in skill_ids:
            row = conn.execute(
                "SELECT id FROM user_skills WHERE user_id=? AND skill_id=?",
                (user_id, sid),
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO user_skills(user_id, skill_id, score, source) VALUES(?,?,?,?)",
                    (user_id, sid, 1.0, source),
                )
        conn.commit()
    finally:
        conn.close()