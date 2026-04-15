"""
pipeline.py â€” SkillRadar NLP Upgrade (changed sections only)
=============================================================

CHANGES FROM PREVIOUS VERSION
------------------------------
1. Role normalisation moved into analysis_engine.normalize_role() â€”
   pipeline no longer maintains its own ROLE_ALIASES dict.

2. cache_hits log line fixed:
   OLD: cache_hits=%d/%d  (denominator was len(all_skills) but numerator
        counted all_texts including chunks â†’ always showed nonsense like 103/12)
   NEW: cache_hits correctly computed as hits-in-cache / total-texts-submitted

3. Empty resume warning added to pipeline log so operators can see it.

4. _slim_analysis_for_session() added â€” strips heavy nested dicts before
   writing to the Flask session cookie (fixes the 6052-byte cookie warning).

5. Step 0a log now uses pipeline_embeddings["skill_list"] length, not
   len(all_skills), to match the actual arrays passed downstream.

HOW TO APPLY
------------
Replace the corresponding sections in your existing pipeline.py.
Everything not shown here stays exactly as-is.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import threading
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional

from services.embedding_engine import encode_pipeline_inputs
from services.analysis_engine  import (
    extract_skills_from_embeddings,
    merge_github_confidence,
    normalize_role,           # NEW import â€” role alias resolution lives here now
    ROLES_CONFIG,
)
from services.github_analysis    import analyze_github_profile, LANG_SKILL_MAP
from services.skill_analysis     import calculate_skill_gap_from_analysis
from services.improvement_engine import get_next_best_action, rank_all_actions
from services.roadmap_generator  import generate_roadmap_from_analysis
from services.progress_tracker   import record_analysis_snapshot

log = logging.getLogger("SkillRadar.pipeline")

_DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "frontend": {"html", "css", "javascript", "react", "typescript", "tailwind",
                 "next.js", "vue", "angular"},
    "backend":  {"python", "flask", "django", "node.js", "rest apis", "api design",
                 "postgresql", "redis", "celery", "fastapi"},
    "dsa":      {"algorithms", "data structures", "sorting", "searching",
                 "graphs", "trees", "dynamic programming"},
    "ml":       {"machine learning", "deep learning", "pytorch", "tensorflow",
                 "scikit-learn", "pandas", "numpy", "statistics", "mlops", "cuda"},
    "devops":   {"docker", "git", "linux", "kubernetes", "ci/cd", "terraform",
                 "aws", "cloud", "nginx"},
}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â”€â”€ CHANGE 1: run_analysis_pipeline() â€” replace the entire function â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def run_analysis_pipeline(
    raw_text:    str,
    goal_role:   str,
    github_user: str,
    user_id:     int,
    previous_analysis: Optional[dict] = None,
) -> dict:
    """
    Full AI analysis pipeline with parallel Step 0 and semantic NLP extraction.

    â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
    â”‚  PARALLEL BLOCK  (ThreadPoolExecutor, max_workers=2)     â”‚
    â”‚  Thread A â€” encode_pipeline_inputs()   CPU / model       â”‚
    â”‚  Thread B â€” analyze_github_profile()   network I/O       â”‚
    â”‚  GitHub timeout: 5 s.  GitHub failure is non-fatal.      â”‚
    â”‚  Embedding failure raises RuntimeError immediately.      â”‚
    â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
    Step 1 â€” extract_skills_from_embeddings()   semantic NLP
    Step 2 â€” merge_github_confidence()
    Step 3 â€” calculate_skill_gap_from_analysis()
    Step 4 â€” get_next_best_action() + rank_all_actions()
    Step 5 â€” generate_roadmap_from_analysis()
    Step 6 â€” record_analysis_snapshot()  (non-blocking)
    Step 7 â€” _persist_analytics_history()  (non-blocking)
    """
    pipeline_start = time.perf_counter()
    log.info("=" * 60)
    log.info(
        "[Pipeline] START  user=%s  role=%s  github=%s",
        user_id, goal_role, github_user or "none",
    )

    # â”€â”€ CHANGE 1a: role normalisation delegated to analysis_engine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    goal_role  = normalize_role(goal_role)          # handles all aliases + logging
    role_cfg   = ROLES_CONFIG[goal_role]
    all_skills = list(dict.fromkeys(role_cfg["required"] + role_cfg["bonus"]))

    log.info("[Pipeline] Resolved role â†’ '%s'  (%d skills)", goal_role, len(all_skills))

    # =========================================================================
    # PARALLEL BLOCK
    # =========================================================================
    parallel_start = time.perf_counter()

    pipeline_embeddings: dict = {}
    github_profile:      dict = {}

    with ThreadPoolExecutor(max_workers=2) as executor:

        embed_future: Future = executor.submit(
            encode_pipeline_inputs, raw_text, all_skills
        )
        github_future: Optional[Future] = (
            executor.submit(analyze_github_profile, github_user, timeout=5)
            if github_user else None
        )

        # â”€â”€ Resolve embeddings (required) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            pipeline_embeddings = embed_future.result()

            # â”€â”€ CHANGE 1b: cache_hits ratio now uses total texts, not skills â”€â”€
            total_texts = pipeline_embeddings["n_chunks"] + len(all_skills)
            cache_hits  = pipeline_embeddings.get("cache_hits", 0)

            log.info(
                "[Step 0a / embeddings]  chunks=%d  skills=%d  "
                "cache_hits=%d/%d  empty_resume=%s  elapsed=%s",
                pipeline_embeddings["n_chunks"],
                len(pipeline_embeddings["skill_list"]),
                cache_hits,
                total_texts,                        # correct denominator
                pipeline_embeddings["n_chunks"] == 1
                and "(no resume" in pipeline_embeddings["resume_chunks"][0].lower(),
                _ms(parallel_start),
            )
        except Exception as exc:
            log.error("[Step 0a / embeddings] FATAL: %s", exc)
            raise RuntimeError(f"Embedding step failed: {exc}") from exc

        # â”€â”€ Resolve GitHub (optional) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if github_future is not None:
            try:
                github_profile = github_future.result(timeout=5)
                log.info(
                    "[Step 0b / github]  depth=%s  langs=%d  elapsed=%s",
                    github_profile.get("depth", "n/a"),
                    len(github_profile.get("languages", {})),
                    _ms(parallel_start),
                )
            except FutureTimeoutError:
                github_future.cancel()
                log.warning("[Step 0b / github] Timed out â€” continuing without GitHub data.")
            except Exception as exc:
                log.warning("[Step 0b / github] Failed (%s) â€” continuing without GitHub data.", exc)
        else:
            log.info("[Step 0b / github] Skipped â€” no username provided.")

    log.info("[Parallel block] resolved in %s", _ms(parallel_start))
    # =========================================================================
    # END PARALLEL BLOCK
    # =========================================================================

    github_confidence: dict[str, float] = _extract_github_confidence(github_profile)

    # â”€â”€ Step 1: Semantic skill extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    t0       = time.perf_counter()
    analysis = extract_skills_from_embeddings(
        pipeline_embeddings = pipeline_embeddings,
        goal_role           = goal_role,
    )
    analysis["github_confidence"] = github_confidence
    analysis["github_profile"]    = github_profile
    analysis["resume_text"]       = raw_text or ""
    if github_user:
        analysis["github_username"] = github_user

    # â”€â”€ CHANGE 1c: warn operators when resume was empty â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if analysis.get("empty_resume"):
        log.warning(
            "[Step 1 / analysis] Empty resume â€” skill scores are GitHub-only. "
            "Matched=%d from GitHub confidence only.",
            len(analysis["matched_skills"]),
        )

    log.info(
        "[Step 1 / analysis]  matched=%d  missing=%d  method=%s  elapsed=%s",
        len(analysis["matched_skills"]),
        len(analysis["missing_skills"]),
        "semantic" if analysis.get("ai_extraction") else "keyword",
        _ms(t0),
    )

    # â”€â”€ Step 2: Merge GitHub signals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    t0       = time.perf_counter()
    analysis = merge_github_confidence(analysis, github_confidence)
    log.info(
        "[Step 2 / merge_github]  matched=%d  score=%.1f%%  elapsed=%s",
        len(analysis["matched_skills"]), analysis["match_score"], _ms(t0),
    )

    # â”€â”€ Step 3: Gap scoring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    t0         = time.perf_counter()
    gap_result = calculate_skill_gap_from_analysis(analysis)
    analysis["gap_analysis"] = gap_result
    analysis["gap_detail"]   = gap_result
    log.info(
        "[Step 3 / skill_gap]  strong=%d  moderate=%d  missing=%d  match=%.1f%%  elapsed=%s",
        len(gap_result["strong"]),
        len(gap_result["moderate"]),
        len(gap_result["missing"]),
        gap_result["match_percentage"],
        _ms(t0),
    )

    # â”€â”€ Step 4: Ranked actions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    t0            = time.perf_counter()
    user_level    = _infer_level(analysis["match_score"])
    missing_names = analysis["missing_skills"]

    next_action = get_next_best_action(
        missing_skills    = missing_names,
        focus_role        = goal_role,
        user_level        = user_level,
        matched_skills    = analysis["matched_skills"],
        github_confidence = github_confidence,
    )
    ranked_actions = rank_all_actions(
        missing_skills    = missing_names,
        focus_role        = goal_role,
        user_level        = user_level,
        top_k             = 5,
        github_confidence = github_confidence,
    )
    analysis["next_action"]     = next_action
    analysis["ranked_actions"]  = ranked_actions
    analysis["recommendations"] = {
        "next_action":    next_action,
        "ranked_actions": ranked_actions,
    }
    analysis["user_level"] = user_level
    log.info(
        "[Step 4 / improvement]  top_skill=%s  priority=%.2f  elapsed=%s",
        next_action.get("skill", "n/a"),
        next_action.get("priority_score", 0),
        _ms(t0),
    )

    # â”€â”€ Step 5: Roadmap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    t0      = time.perf_counter()
    roadmap = generate_roadmap_from_analysis(analysis)
    analysis["roadmap"] = roadmap
    total_steps = sum(len(v) for v in roadmap.get("phases", {}).values())
    log.info(
        "[Step 5 / roadmap]  level=%s  phases=%d  steps=%d  elapsed=%s",
        roadmap.get("level"), len(roadmap.get("phases", {})), total_steps, _ms(t0),
    )

    # â”€â”€ Step 6 + 7: background persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _queue_persistence(user_id, analysis, github_user)

    elapsed = time.perf_counter() - pipeline_start
    log.info(
        "[Pipeline] DONE  total=%.2fs  score=%.1f%%  level=%s",
        elapsed, analysis["match_score"], user_level,
    )
    log.info("=" * 60)

    return analysis


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â”€â”€ CHANGE 2: _slim_analysis_for_session() â€” NEW function â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Use this in app.py instead of writing the full analysis to session:
#
#   session["analysis"] = _slim_analysis_for_session(analysis)
#
# Fixes: "session cookie too large: 6052 bytes, limit 4093 bytes"
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def slim_analysis_for_session(analysis: dict) -> dict:
    """
    Return a lightweight version of the analysis dict safe to store in a
    Flask session cookie (target < 3 500 bytes serialized).

    Heavy fields stripped: skill_details, github_profile, gap_analysis (full),
    recommendations (full), roadmap (full phases).
    Slim summaries kept for template rendering.
    """
    gap   = analysis.get("gap_detail", {})
    rdmap = analysis.get("roadmap", {})

    return {
        # â”€â”€ Core identity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        "role":            analysis.get("role"),
        "match_score":     analysis.get("match_score"),
        "user_level":      analysis.get("user_level"),
        "github_username": analysis.get("github_username", ""),
        "ai_extraction":   analysis.get("ai_extraction", False),
        "empty_resume":    analysis.get("empty_resume", False),

        # â”€â”€ Skill lists (flat, needed by templates + chat endpoint) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        "matched_skills":   analysis.get("matched_skills", []),
        "missing_skills":   analysis.get("missing_skills", []),
        "missing_required": analysis.get("missing_required", []),
        "missing_bonus":    analysis.get("missing_bonus", []),

        # â”€â”€ Slim skill details â€” confidence + source only, no tier/method â”€â”€â”€â”€â”€
        "skill_details": {
            skill: {
                "confidence": info.get("confidence", 0),
                "source":     info.get("source", "resume"),
            }
            for skill, info in
            list(analysis.get("skill_details", {}).items())[:20]   # cap at 20
        },

        # â”€â”€ Gap summary (counts + top-5 per bucket) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        "gap_detail": {
            "match_percentage": gap.get("match_percentage", 0),
            "gap_score":        gap.get("gap_score", 0),
            "strong":           gap.get("strong",   [])[:5],
            "moderate":         gap.get("moderate", [])[:5],
            "missing":          gap.get("missing",  [])[:5],
        },

        # â”€â”€ Single best action + top-3 ranked â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        "next_action":    analysis.get("next_action", {}),
        "ranked_actions": analysis.get("ranked_actions", [])[:3],

        # â”€â”€ Roadmap â€” level + first 2 items per phase â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        "roadmap": {
            "role":  rdmap.get("role"),
            "level": rdmap.get("level"),
            "phases": {
                phase_name: steps[:2]
                for phase_name, steps in rdmap.get("phases", {}).items()
            },
        },
    }


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Private helpers â€” unchanged from previous version
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _ms(t0: float) -> str:
    return f"{(time.perf_counter() - t0) * 1000:.0f}ms"


def _queue_persistence(user_id: int, analysis: dict, github_user: str) -> None:
    snapshot = deepcopy(analysis)
    worker = threading.Thread(
        target=_persist_in_background,
        args=(user_id, snapshot, github_user),
        daemon=True,
    )
    worker.start()


def _persist_in_background(user_id: int, analysis: dict, github_user: str) -> None:
    try:
        t0 = time.perf_counter()
        record_analysis_snapshot(user_id, analysis)
        log.info("[Step 6 / tracker]  snapshot recorded  elapsed=%s", _ms(t0))
    except Exception as exc:
        log.warning("[Step 6 / tracker]  non-fatal: %s", exc)

    try:
        _persist_analytics_history(user_id, analysis, github_user)
    except Exception as exc:
        log.warning("[Step 7 / analytics_history]  non-fatal: %s", exc)


def _infer_level(match_score: float) -> str:
    if match_score >= 70: return "advanced"
    if match_score >= 40: return "intermediate"
    return "beginner"


def _extract_github_confidence(github_profile: dict) -> dict[str, float]:
    confidence: dict[str, float] = {}
    for lang, info in github_profile.get("languages", {}).items():
        canonical = LANG_SKILL_MAP.get(lang.lower(), lang.lower())
        conf      = info.get("confidence", 0.0) if isinstance(info, dict) else 0.0
        confidence[canonical] = max(confidence.get(canonical, 0.0), conf)
    for fw, info in github_profile.get("frameworks", {}).items():
        fw_lower = fw.lower()
        conf     = info.get("confidence", 0.0) if isinstance(info, dict) else 0.0
        confidence[fw_lower] = max(confidence.get(fw_lower, 0.0), conf)
    return confidence


def _domain_score(matched_skills: list[str], domain: str) -> int:
    keywords = _DOMAIN_KEYWORDS.get(domain, set())
    if not keywords:
        return 0
    hits = sum(1 for s in matched_skills if s.lower() in keywords)
    return round((hits / len(keywords)) * 100)


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


def _persist_analytics_history(user_id: int, analysis: dict, github_user: str) -> None:
    try:
        skill_breakdown = {
            d: _domain_score(analysis.get("matched_skills", []), d)
            for d in ("frontend", "backend", "dsa", "ml", "devops")
        }
        db = _get_db()
        db.execute(
            """
            INSERT INTO user_analysis_history
                (user_id, total_score, skill_breakdown,
                 matched_skills, missing_skills, analysis_source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                len(analysis.get("matched_skills", [])),
                json.dumps(skill_breakdown),
                json.dumps(analysis.get("matched_skills", [])),
                json.dumps(analysis.get("missing_skills", [])),
                "combined" if github_user else "resume",
            ),
        )
        db.commit()
        db.close()
    except Exception as exc:
        log.warning("[Pipeline] analytics history write failed: %s", exc)
