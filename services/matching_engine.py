"""
services/matching_engine.py — SkillRadar Final Version

Semantic teacher–student matching using sentence-transformer embeddings.

Changes from previous version
------------------------------
1. SimilarityCache off-by-one fixed:
     OLD: if len(self.cache) > self.max_size → grew to max_size+1 before eviction
     NEW: if len(self.cache) >= self.max_size → evicts at the limit

2. Cache key collision risk fixed:
     OLD: f"{query}|{'|'.join(corpus_tuple)}" — a query containing '|' could
          collide with a different query+corpus pair.
     NEW: SHA-256 of (query, corpus) serialized with json.dumps — zero collision risk.

3. Query construction weighted by skill importance:
     OLD: "Expert in python, docker, linux, redis, postgresql, celery"
          — all six skills get equal weight, diluting the signal
     NEW: Top-3 required skills build the primary query.
          A secondary "context" phrase appends remaining skills at reduced weight
          by putting them in a parenthetical.  This makes the embedding space
          lean toward the most critical skills.

4. Empty bio guard:
     OLD: empty bio "" reaches cosine_similarity → all-zero vector → nan score
     NEW: teachers with empty bio get score=0.0 directly, skipping embedding

5. Keyword fallback improved:
     OLD: +20 per matched skill, starting from 25 — scores could hit 125
     NEW: score = min(100, 25 + matched_count * 20) — capped at 100

6. INFO log on every cache read removed — was polluting hot-path logs.
   Now only DEBUG-level inside cache hits/misses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from .embedding_engine import similarity_scores

log = logging.getLogger("SkillRadar.matching")


# ─────────────────────────────────────────────────────────────────────────────
# Similarity cache
# ─────────────────────────────────────────────────────────────────────────────

class _SimilarityCache:
    """
    LRU-style in-memory cache for similarity_scores() results.

    Key: SHA-256 of (query, sorted corpus) — collision-free, order-independent.
    Eviction: oldest insertion removed when capacity is reached (FIFO on dict).
    Python 3.7+ dicts are insertion-ordered, so `next(iter(cache))` is the
    oldest entry.
    """

    def __init__(self, max_size: int = 500):
        self._cache:    dict = {}
        self._max_size: int  = max_size

    def _key(self, query: str, corpus: list[str]) -> str:
        payload = json.dumps({"q": query, "c": sorted(corpus)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, query: str, corpus: list[str]):
        k = self._key(query, corpus)
        v = self._cache.get(k)
        if v is not None:
            log.debug("[SimilarityCache] HIT  key=%s", k[:12])
        return v

    def set(self, query: str, corpus: list[str], scores) -> None:
        k = self._key(query, corpus)
        # Evict oldest entry if at capacity (fix: >= not >)
        if len(self._cache) >= self._max_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
            log.debug("[SimilarityCache] Evicted oldest entry, size=%d", len(self._cache))
        self._cache[k] = scores
        log.debug("[SimilarityCache] SET  key=%s  size=%d", k[:12], len(self._cache))

    def clear(self) -> None:
        self._cache.clear()
        log.info("[SimilarityCache] Cleared.")

    @property
    def size(self) -> int:
        return len(self._cache)


_cache = _SimilarityCache(max_size=500)


# ─────────────────────────────────────────────────────────────────────────────
# Query construction
# ─────────────────────────────────────────────────────────────────────────────

# Skills that carry the most signal in teacher matching — checked first
_HIGH_SIGNAL_SKILLS = {
    "machine learning", "deep learning", "pytorch", "tensorflow",
    "mlops", "distributed systems", "system design", "kubernetes",
    "docker", "python", "react", "node.js", "fastapi", "postgresql",
    "data engineering", "nlp", "computer vision",
}


def _build_query(missing_skills: list[str]) -> str:
    """
    Build a natural-language query from missing skills that biases the
    embedding toward the most important skills.

    Strategy:
      1. Sort skills: high-signal first, then alphabetical for stability.
      2. Primary phrase: top 3 skills → "Expert in X, Y and Z"
      3. Context phrase: skills 4+ → appended as "(also: A, B, C)"

    This causes the query embedding to cluster near the top skills while
    still encoding the rest as a soft signal.
    """
    if not missing_skills:
        return "Software engineering mentor"

    # Sort: high-signal skills to front, rest alphabetically
    high   = sorted([s for s in missing_skills if s.lower() in _HIGH_SIGNAL_SKILLS])
    others = sorted([s for s in missing_skills if s.lower() not in _HIGH_SIGNAL_SKILLS])
    ordered = (high + others)[:10]   # cap at 10 to avoid query dilution

    primary = ordered[:3]
    rest    = ordered[3:]

    if len(primary) == 1:
        query = f"Expert mentor in {primary[0]}"
    elif len(primary) == 2:
        query = f"Expert mentor in {primary[0]} and {primary[1]}"
    else:
        query = f"Expert mentor in {primary[0]}, {primary[1]} and {primary[2]}"

    if rest:
        query += f" (also: {', '.join(rest)})"

    return query


# ─────────────────────────────────────────────────────────────────────────────
# DB helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def match_student_with_teachers(
    user_id:        int,
    missing_skills: list[str] | None = None,
) -> list[dict]:
    """
    Semantically match a student's skill gap to teacher bios.

    Args:
        user_id:        Student's DB ID.
        missing_skills: Pre-computed missing skills list.
                        If None, falls back to latest mentorship request role.

    Returns:
        Top 3 teacher dicts sorted by match_score descending.
        Each dict: {id, name, bio, match_score, reason}
    """
    db = _get_db()

    # ── Resolve missing skills ────────────────────────────────────────────────
    if missing_skills is None:
        row = db.execute("""
            SELECT goal_role FROM mentorship_requests
            WHERE student_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()
        db.close()

        if not row:
            return []
        missing_skills = [row["goal_role"]]
    else:
        db.close()
        db = _get_db()   # reopen for teacher query below

    # ── Load teachers ─────────────────────────────────────────────────────────
    teachers = db.execute(
        "SELECT id, name, bio, role FROM users WHERE role IN ('teacher', 'faculty')"
    ).fetchall()
    db.close()

    if not teachers:
        log.info("[MatchingEngine] No teachers found in DB.")
        return []

    # ── Separate teachers with usable bios from those without ────────────────
    with_bio    = [(t["id"], t["name"], t["bio"]) for t in teachers if t["bio"] and t["bio"].strip()]
    without_bio = [(t["id"], t["name"], "")        for t in teachers if not (t["bio"] and t["bio"].strip())]

    query = _build_query(missing_skills)
    log.debug("[MatchingEngine] Query: '%s'", query)

    matches: list[dict] = []

    # ── Score teachers that have bios ─────────────────────────────────────────
    if with_bio:
        bio_texts = [bio for _, _, bio in with_bio]

        # Try cache first
        scored = _cache.get(query, bio_texts)
        if scored is None:
            try:
                scored = similarity_scores(query, bio_texts)
                _cache.set(query, bio_texts, scored)
            except Exception as exc:
                log.error(
                    "[MatchingEngine] Semantic scoring failed (%s), "
                    "using keyword fallback.", exc,
                )
                scored = None

        if scored is not None:
            bio_score_map = dict(scored)
            for tid, name, bio in with_bio:
                sim   = bio_score_map.get(bio, 0.0)
                sim   = 0.0 if (sim != sim) else sim    # nan guard
                score = round(sim * 100, 1)
                matches.append(_build_match_dict(
                    tid, name, bio, score, sim, missing_skills
                ))
        else:
            # Fallback for with_bio teachers
            for tid, name, bio in with_bio:
                matches.append(_keyword_fallback_match(tid, name, bio, missing_skills))

    # ── Teachers without bio get score 0 ─────────────────────────────────────
    for tid, name, bio in without_bio:
        matches.append({
            "id":          tid,
            "name":        name,
            "bio":         bio,
            "match_score": 0.0,
            "reason":      "No bio available for semantic matching.",
        })

    matches.sort(key=lambda x: x["match_score"], reverse=True)

    top3 = matches[:3]
    log.info(
        "[MatchingEngine] Matched user=%d  top_score=%.1f  query_skills=%d",
        user_id,
        top3[0]["match_score"] if top3 else 0.0,
        len(missing_skills),
    )
    return top3


def clear_similarity_cache() -> None:
    """Clear the in-memory similarity cache."""
    _cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_match_dict(
    tid:            int,
    name:           str,
    bio:            str,
    score:          float,
    sim:            float,
    missing_skills: list[str],
) -> dict:
    top2 = missing_skills[:2]
    if sim >= 0.70:
        reason = (
            f"Strong semantic match for your learning needs in "
            f"{', '.join(top2)}."
        )
    elif sim >= 0.50:
        reason = f"Good overlap with your skill gaps in {missing_skills[0]}."
    elif sim >= 0.30:
        reason = "Potential match based on your skill gap profile."
    else:
        reason = "Limited overlap — consider as a secondary option."

    return {"id": tid, "name": name, "bio": bio, "match_score": score, "reason": reason}


def _keyword_fallback_match(
    tid:            int,
    name:           str,
    bio:            str,
    missing_skills: list[str],
) -> dict:
    """
    Keyword-based scoring when semantic matching fails.
    Score = min(100, 25 + 20 × matched_skill_count).
    """
    bio_lower     = (bio or "").lower()
    matched_count = sum(1 for s in missing_skills if s.lower() in bio_lower)
    score         = min(100, 25 + matched_count * 20)
    reason        = "Matches based on keyword overlap."

    if matched_count > 0:
        first_match = next(s for s in missing_skills if s.lower() in bio_lower)
        reason = f"Keyword match — specialises in {first_match}."

    return {"id": tid, "name": name, "bio": bio, "match_score": score, "reason": reason}