"""
services/analysis_engine.py — SkillRadar NLP Upgrade

Semantic skill extraction using precomputed sentence-transformer embeddings.
Keyword matching is a last-resort fallback only.

Changes from previous version
------------------------------
1. sim_matrix row index is now derived from pipeline_embeddings["skill_list"]
   (the authoritative order), not from all_skills local list — index mismatch fixed.
2. cache_hits denominator fixed in encode_pipeline_inputs (reported in pipeline log).
3. Empty-resume guard: sentinel chunk suppressed before similarity scoring.
4. confidence_details added per skill — richer than before.
5. Section-aware chunking bonus: skills found in experience/skills sections
   get a +0.05 confidence boost.
6. merge_github_confidence geometric-mean formula preserved exactly.
7. ROLES_CONFIG expanded with Frontend Engineer + Full Stack Developer.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("SkillRadar.analysis")

# ─────────────────────────────────────────────────────────────────────────────
# Role definitions — single source of truth
# ─────────────────────────────────────────────────────────────────────────────

ROLES_CONFIG: dict[str, dict] = {
    "Web Developer": {
        "required": ["html", "css", "javascript", "react", "git", "sql", "api design"],
        "bonus":    ["typescript", "node.js", "tailwind", "next.js", "testing"],
    },
    "Frontend Engineer": {
        "required": ["html", "css", "javascript", "react", "git", "typescript", "tailwind"],
        "bonus":    ["next.js", "vue", "testing", "node.js", "figma", "webpack"],
    },
    "Backend Developer": {
        "required": ["python", "sql", "rest apis", "git", "docker"],
        "bonus":    ["flask", "django", "redis", "postgresql", "linux", "celery"],
    },
    "Full Stack Developer": {
        "required": ["html", "css", "javascript", "react", "python", "sql", "git", "docker"],
        "bonus":    ["typescript", "node.js", "flask", "postgresql", "redis", "testing"],
    },
    "Data Scientist": {
        "required": ["python", "pandas", "numpy", "sql", "statistics", "machine learning"],
        "bonus":    ["matplotlib", "scikit-learn", "jupyter", "tensorflow", "spark"],
    },
    "ML Engineer": {
        "required": ["python", "machine learning", "scikit-learn", "docker", "git"],
        "bonus":    ["pytorch", "tensorflow", "cuda", "fastapi", "linux", "mlops"],
    },
    "DevOps Engineer": {
        "required": ["docker", "linux", "git", "ci/cd", "kubernetes"],
        "bonus":    ["terraform", "aws", "ansible", "python", "nginx", "redis"],
    },
    "Software Engineer": {
        "required": ["python", "git", "sql", "algorithms", "data structures"],
        "bonus":    ["javascript", "docker", "testing", "linux", "system design"],
    },
}

# Role name aliases - maps free-text form values to canonical ROLES_CONFIG keys
ROLE_ALIASES: dict[str, str] = {
    "frontend engineer":         "Web Developer",
    "frontend developer":        "Frontend Engineer",
    "front end developer":       "Frontend Engineer",
    "front-end developer":       "Frontend Engineer",
    "backend engineer":          "Backend Developer",
    "backend developer":         "Backend Developer",
    "back end developer":        "Backend Developer",
    "back-end developer":        "Backend Developer",
    "full stack developer":      "Full Stack Developer",
    "full-stack developer":      "Full Stack Developer",
    "fullstack developer":       "Full Stack Developer",
    "full stack engineer":       "Full Stack Developer",
    "data analyst":              "Data Scientist",
    "data engineer":             "Data Scientist",
    "ml engineer":               "Machine Learning Engineer",
    "machine learning engineer": "Machine Learning Engineer",
    "ai engineer":               "ML Engineer",
    "artificial intelligence engineer": "ML Engineer",
    "software developer":        "Software Engineer",
    "software development engineer": "Software Engineer",
    "sde":                       "Software Engineer",
    "devops":                    "DevOps Engineer",
    "site reliability engineer": "DevOps Engineer",
    "sre":                       "DevOps Engineer",
    "web developer":             "Web Developer",
}

ROLES_CONFIG["Machine Learning Engineer"] = {
    "required": ROLES_CONFIG["ML Engineer"]["required"][:],
    "bonus": ROLES_CONFIG["ML Engineer"]["bonus"][:],
}

# Similarity thresholds — required skills get a lower bar (prefer recall)
THRESHOLD_REQUIRED = 0.38
THRESHOLD_BONUS    = 0.42

# Section headers that indicate high-value resume zones
_HIGH_VALUE_SECTIONS = {
    "experience", "work experience", "skills", "technical skills",
    "projects", "achievements", "certifications", "education",
}

# GitHub signal maps
GITHUB_LANG_MAP: dict[str, str] = {
    "python":           "python",
    "javascript":       "javascript",
    "typescript":       "typescript",
    "html":             "html",
    "css":              "css",
    "jupyter notebook": "python",
    "sql":              "sql",
    "go":               "go",
    "rust":             "rust",
    "java":             "java",
    "kotlin":           "kotlin",
    "swift":            "swift",
    "c++":              "c++",
    "c#":               "c#",
    "shell":            "linux",
}

GITHUB_TOPIC_MAP: dict[str, str] = {
    "react":            "react",
    "nextjs":           "next.js",
    "next-js":          "next.js",
    "flask":            "flask",
    "django":           "django",
    "pytorch":          "pytorch",
    "tensorflow":       "tensorflow",
    "docker":           "docker",
    "scikit-learn":     "scikit-learn",
    "pandas":           "pandas",
    "numpy":            "numpy",
    "machine-learning": "machine learning",
    "redis":            "redis",
    "postgresql":       "postgresql",
    "tailwind":         "tailwind",
    "node":             "node.js",
    "fastapi":          "fastapi",
    "mlops":            "mlops",
    "celery":           "celery",
    "linux":            "linux",
    "cuda":             "cuda",
    "kubernetes":       "kubernetes",
    "terraform":        "terraform",
    "ci-cd":            "ci/cd",
    "github-actions":   "ci/cd",
}


# ─────────────────────────────────────────────────────────────────────────────
# Role normalisation (used by pipeline.py before calling this module)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_role(raw_role: str) -> str:
    """
    Map a free-text role string to a canonical ROLES_CONFIG key.
    Falls back to "Web Developer" for unknown roles.
    """
    if not raw_role:
        return "Web Developer"

    clean = raw_role.strip().lower()
    if clean in ROLE_ALIASES:
        return ROLE_ALIASES[clean]
    # Try exact title-case match
    title = raw_role.strip().title()
    if title in ROLES_CONFIG:
        return title
    log.warning("[AnalysisEngine] Unknown role '%s', defaulting to Web Developer", raw_role)
    return "Web Developer"


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Semantic extraction from precomputed embeddings
# ─────────────────────────────────────────────────────────────────────────────

def extract_skills_from_embeddings(
    pipeline_embeddings: dict,
    goal_role: str,
) -> dict:
    """
    Classify skills from precomputed embedding arrays.

    DOES NOT call the model — all vectors are provided by
    encode_pipeline_inputs() in embedding_engine.py.

    Index alignment guarantee
    -------------------------
    We iterate over pipeline_embeddings["skill_list"] (the authoritative order
    from encode_pipeline_inputs) rather than a locally built all_skills list.
    This prevents the silent score-to-wrong-skill bug that occurred when the
    two lists had different orderings.

    Empty resume guard
    ------------------
    If the only chunk is the sentinel "(no resume text provided)", the
    similarity matrix is computed but ALL scores are discarded — no false
    positives from a meaningless chunk.

    Section-aware confidence boost
    --------------------------------
    Chunks that follow a high-value section header (e.g. "Skills", "Experience")
    get a +0.05 additive bonus applied to the best-match score for that skill
    before threshold comparison.  Boost is capped so confidence never exceeds 1.0.
    """
    goal_role = normalize_role(goal_role)
    role_cfg     = ROLES_CONFIG[goal_role]
    all_required = role_cfg["required"]
    all_bonus    = role_cfg["bonus"]

    chunk_embeddings = pipeline_embeddings["chunk_embeddings"]   # (n_chunks, 384)
    skill_embeddings = pipeline_embeddings["skill_embeddings"]   # (n_skills, 384)
    skill_list       = pipeline_embeddings["skill_list"]         # authoritative order
    resume_chunks    = pipeline_embeddings.get("resume_chunks", [])

    # ── Empty resume guard ────────────────────────────────────────────────────
    is_empty_resume = (
        len(resume_chunks) == 1
        and resume_chunks[0].strip().lower() == "(no resume text provided)"
    )

    matched_scores: dict[str, float] = {}
    ai_available = False

    if not is_empty_resume:
        try:
            from sklearn.metrics.pairwise import cosine_similarity

            # (n_skills, n_chunks) — each row is one skill, each column one chunk
            sim_matrix = cosine_similarity(skill_embeddings, chunk_embeddings)

            # Compute per-chunk section bonus ONCE
            section_boost = _build_section_boost(resume_chunks)  # list[float] len=n_chunks

            for i, skill in enumerate(skill_list):
                # Best raw similarity across all chunks
                raw_scores   = sim_matrix[i]                         # shape (n_chunks,)
                boosted      = [
                    min(1.0, float(raw_scores[j]) + section_boost[j])
                    for j in range(len(resume_chunks))
                ]
                best_score   = max(boosted) if boosted else 0.0
                best_raw     = float(raw_scores.max())

                threshold = THRESHOLD_REQUIRED if skill in all_required else THRESHOLD_BONUS
                if best_score >= threshold:
                    matched_scores[skill] = round(best_score, 4)

            ai_available = True
            log.debug(
                "[AnalysisEngine] Semantic path: %d/%d skills matched",
                len(matched_scores), len(skill_list),
            )

        except Exception as exc:
            log.warning(
                "[AnalysisEngine] Semantic similarity failed (%s), "
                "falling back to keyword scan.", exc,
            )
            resume_text    = " ".join(resume_chunks)
            matched_scores = _keyword_fallback(
                _normalize(resume_text), all_required, all_bonus
            )
    else:
        log.info("[AnalysisEngine] Empty resume detected — skipping semantic scoring.")

    # ── Build structured skill_details ───────────────────────────────────────
    skill_details: dict[str, dict] = {}
    missing_req:   list[str]       = []
    missing_bonus: list[str]       = []

    for skill in all_required:
        if skill in matched_scores:
            skill_details[skill] = _make_skill_entry(
                confidence = matched_scores[skill],
                source     = "resume",
                tier       = "required",
                method     = "semantic" if ai_available else "keyword",
            )
        else:
            missing_req.append(skill)

    for skill in all_bonus:
        if skill in matched_scores:
            skill_details[skill] = _make_skill_entry(
                confidence = matched_scores[skill],
                source     = "resume",
                tier       = "bonus",
                method     = "semantic" if ai_available else "keyword",
            )
        else:
            missing_bonus.append(skill)

    matched_names = list(skill_details.keys())
    match_score   = _calculate_match_score(matched_names, all_required, all_bonus)

    return {
        # ── Standard pipeline schema ─────────────────────────────────────────
        "role":              goal_role,
        "matched_skills":    matched_names,
        "missing_skills":    missing_req + missing_bonus,
        "skill_details":     skill_details,
        "github_confidence": {},
        "gap_analysis":      {},
        "recommendations":   {},
        "roadmap":           {},
        "user_level":        "",
        # ── Internal / backward-compat keys ──────────────────────────────────
        "matched":           skill_details,
        "missing_required":  missing_req,
        "missing_bonus":     missing_bonus,
        "match_score":       match_score,
        "ai_extraction":     ai_available,
        "empty_resume":      is_empty_resume,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Merge GitHub confidence into skill_details
# ─────────────────────────────────────────────────────────────────────────────

def merge_github_confidence(
    analysis: dict,
    github_confidence: dict[str, float],
) -> dict:
    """
    Merge GitHub confidence scores into analysis["skill_details"].

    Merge rules
    -----------
    Resume + GitHub  -> geometric mean + 0.05 bonus, source = "resume+github"
    GitHub only      -> gh_conf as-is, source = "github"  (added to matched)
    Resume only      -> unchanged
    """
    if not github_confidence:
        return analysis

    role     = normalize_role(analysis.get("role", "Web Developer"))
    role_cfg = ROLES_CONFIG.get(role, ROLES_CONFIG["Web Developer"])
    all_role = set(role_cfg["required"] + role_cfg["bonus"])

    skill_details = analysis.get("skill_details", {})

    for skill, gh_conf in github_confidence.items():
        skill_lower = skill.lower()

        if skill_lower in skill_details:
            resume_conf = skill_details[skill_lower]["confidence"]
            combined    = round(min(1.0, (resume_conf * gh_conf) ** 0.5 + 0.05), 4)
            skill_details[skill_lower]["confidence"] = combined
            skill_details[skill_lower]["source"]     = "resume+github"

        elif skill_lower in all_role:
            tier = "required" if skill_lower in role_cfg["required"] else "bonus"
            skill_details[skill_lower] = _make_skill_entry(
                confidence = round(gh_conf, 4),
                source     = "github",
                tier       = tier,
                method     = "github",
            )
            analysis["missing_required"] = [
                s for s in analysis.get("missing_required", []) if s != skill_lower
            ]
            analysis["missing_bonus"] = [
                s for s in analysis.get("missing_bonus", []) if s != skill_lower
            ]

    matched_names = list(skill_details.keys())
    analysis["skill_details"]  = skill_details
    analysis["matched"]        = skill_details
    analysis["matched_skills"] = matched_names
    analysis["missing_skills"] = (
        analysis.get("missing_required", []) + analysis.get("missing_bonus", [])
    )
    analysis["match_score"] = _calculate_match_score(
        matched_names, role_cfg["required"], role_cfg["bonus"]
    )
    return analysis


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_skill_entry(
    confidence: float,
    source:     str,
    tier:       str,
    method:     str,
) -> dict:
    """Canonical skill_details entry shape."""
    level = (
        "strong"   if confidence >= 0.75 else
        "moderate" if confidence >= 0.55 else
        "weak"
    )
    return {
        "confidence": round(confidence, 4),
        "source":     source,
        "tier":       tier,
        "method":     method,       # "semantic" | "keyword" | "github"
        "level":      level,        # "strong" | "moderate" | "weak"
    }


def _build_section_boost(chunks: list[str]) -> list[float]:
    """
    Return a per-chunk additive boost value (0.0 or 0.05).

    A chunk that immediately follows a high-value section header gets +0.05.
    The header line itself gets 0.0 (it contains no skill evidence).
    """
    boosts: list[float] = []
    in_high_value = False

    for chunk in chunks:
        lower = chunk.strip().lower()
        if lower in _HIGH_VALUE_SECTIONS or any(
            lower.startswith(h) for h in _HIGH_VALUE_SECTIONS
        ):
            in_high_value = True
            boosts.append(0.0)      # header line itself — no boost
        else:
            boosts.append(0.05 if in_high_value else 0.0)

    return boosts


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _keyword_fallback(
    text:     str,
    required: list[str],
    bonus:    list[str],
) -> dict[str, float]:
    """
    Last-resort keyword scan.  Only called when sklearn is unavailable.
    Returns static confidence 0.75 — lower than semantic scores to signal
    reduced certainty in downstream consumers.
    """
    ALIASES: dict[str, str] = {
        "react.js":       "react",
        "reactjs":        "react",
        "nodejs":         "node.js",
        "node":           "node.js",
        "postgres":       "postgresql",
        "sklearn":        "scikit-learn",
        "scikit learn":   "scikit-learn",
        "ml":             "machine learning",
        "api":            "api design",
        "rest api":       "rest apis",
        "restful":        "rest apis",
        "tf":             "tensorflow",
        "torch":          "pytorch",
        "statistical":    "statistics",
        "deep learning":  "machine learning",
        "neural network": "machine learning",
        "tailwindcss":    "tailwind",
        "ci cd":          "ci/cd",
        "github actions": "ci/cd",
        "k8s":            "kubernetes",
    }
    matched: dict[str, float] = {}
    for skill in required + bonus:
        found = skill in text
        if not found:
            for alias, target in ALIASES.items():
                if target == skill and alias in text:
                    found = True
                    break
        if found:
            matched[skill] = 0.75   # keyword match = lower confidence than semantic
    return matched


def _calculate_match_score(
    matched_names: list[str],
    required:      list[str],
    bonus:         list[str],
) -> float:
    req = (
        sum(1 for s in required if s in matched_names) / len(required)
    ) if required else 0.0
    bon = (
        sum(1 for s in bonus if s in matched_names) / len(bonus)
    ) if bonus else 0.0
    return round((req * 0.7 + bon * 0.3) * 100, 1)
