"""
services/embedding_engine.py — SkillRadar Final Version

Sentence-Transformer embedding layer.

Cache priority: Redis (if available) → shelve disk → encode fresh.

Redis connection is tested ONCE at startup and the result is remembered for
the entire process lifetime.  No re-ping on every request.

Public API
----------
    encode(texts, use_cache)                        → np.ndarray
    encode_pipeline_inputs(resume_text, skill_list) → dict
    similarity_scores(query, candidates)            → list[tuple[str, float]]
    top_matches(query, candidates, threshold, top_k)→ list[tuple[str, float]]
    extract_skills_semantic(text, skills, threshold)→ dict[str, float]
    clear_cache()                                   → None
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import shelve
import threading
from typing import Union

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity as _cosine_similarity

log = logging.getLogger("SkillRadar.embeddings")

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", ".cache", "embeddings")
_shelve_lock = threading.RLock()

# ── Model singleton ───────────────────────────────────────────────────────────
_model = None


def _get_model():
    """Load SentenceTransformer exactly once per process."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            log.info("[EmbeddingEngine] Model loaded: all-MiniLM-L6-v2")
        except ImportError:
            raise RuntimeError(
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )
    return _model


def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ── Redis client ──────────────────────────────────────────────────────────────
# Tested once at first use.  Result cached for process lifetime.
# _redis_available = None  → not yet tested
# _redis_available = True  → connected, _redis_client is valid
# _redis_available = False → unavailable, skip forever (no re-ping on reload)
_redis_client    = None
_redis_available = None


def _get_redis():
    """
    Return a connected Redis client or None.

    The connection is attempted exactly once per process.  After that the
    cached True/False flag is used directly — no network call on every request.
    """
    global _redis_client, _redis_available

    if _redis_available is True:
        return _redis_client
    if _redis_available is False:
        return None

    # First call — attempt connection
    try:
        import redis

        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=False,
        )
        client.ping()
        _redis_client    = client
        _redis_available = True
        log.info("[EmbeddingEngine] Redis cache connected.")
        return _redis_client

    except Exception as exc:
        _redis_available = False
        log.warning(
            "[EmbeddingEngine] Redis unavailable (%s), "
            "falling back to shelve disk cache.", exc,
        )
        return None


# ── Core encode ───────────────────────────────────────────────────────────────

def encode(
    texts: Union[str, list[str]],
    use_cache: bool = True,
) -> np.ndarray:
    """
    Encode one or more strings into 384-dim embedding vectors.

    Args:
        texts:     Single string or list of strings.
        use_cache: Set False to bypass all caching (e.g. tests).

    Returns:
        np.ndarray shape (384,) for a single string, (n, 384) for a list.
    """
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    if not texts:
        return np.zeros((0, 384), dtype=np.float32)

    if not use_cache:
        embs = _get_model().encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return embs[0] if single else embs

    redis_client = _get_redis()
    keys         = [f"skillradar:emb:{_cache_key(t)}" for t in texts]
    results      = [None] * len(texts)

    # ── Read from Redis ───────────────────────────────────────────────────────
    if redis_client is not None:
        try:
            raw = redis_client.mget(keys)      # single round-trip
            for i, v in enumerate(raw):
                results[i] = v                 # bytes or None
        except Exception as exc:
            log.warning("[EmbeddingEngine] Redis read error: %s", exc)

    # ── Read from shelve for any remaining misses ─────────────────────────────
    miss_idx = [i for i, v in enumerate(results) if v is None]

    if miss_idx and redis_client is None:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        try:
            with _shelve_lock:
                with shelve.open(CACHE_PATH) as shelf:
                    still_missing = []
                    for i in miss_idx:
                        k = _cache_key(texts[i])
                        if k in shelf:
                            results[i] = pickle.dumps(shelf[k])
                        else:
                            still_missing.append(i)
            miss_idx = still_missing
        except Exception as exc:
            log.warning("[EmbeddingEngine] Shelve read error: %s", exc)

    # ── Encode true misses ────────────────────────────────────────────────────
    if miss_idx:
        miss_texts     = [texts[i] for i in miss_idx]
        new_embeddings = _get_model().encode(
            miss_texts, convert_to_numpy=True, show_progress_bar=False
        )

        # Write back to Redis or shelve
        if redis_client is not None:
            try:
                pipe = redis_client.pipeline()
                for i, emb in zip(miss_idx, new_embeddings):
                    pipe.setex(keys[i], 86400, pickle.dumps(emb))   # 24 h TTL
                pipe.execute()
            except Exception as exc:
                log.warning("[EmbeddingEngine] Redis write error: %s", exc)
        else:
            try:
                os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
                with _shelve_lock:
                    with shelve.open(CACHE_PATH) as shelf:
                        for i, emb in zip(miss_idx, new_embeddings):
                            shelf[_cache_key(texts[i])] = emb
            except Exception as exc:
                log.warning("[EmbeddingEngine] Shelve write error: %s", exc)

        for i, emb in zip(miss_idx, new_embeddings):
            results[i] = pickle.dumps(emb)

    arr = np.array([pickle.loads(v) for v in results], dtype=np.float32)
    return arr[0] if single else arr


# ── Pipeline batch encoder (Step 0) ──────────────────────────────────────────

def encode_pipeline_inputs(
    resume_text: str,
    skill_list:  list[str],
) -> dict:
    """
    Precompute all embeddings needed by run_analysis_pipeline() in one call.

    Returns:
        {
          "resume_chunks":    list[str],
          "chunk_embeddings": np.ndarray  (n_chunks, 384),
          "skill_embeddings": np.ndarray  (n_skills, 384),
          "skill_list":       list[str],      ← authoritative order for sim_matrix
          "n_chunks":         int,
          "cache_hits":       int,            ← hits / total_texts (correct ratio)
        }
    """
    chunks: list[str] = []
    if resume_text and resume_text.strip():
        raw    = re.split(r"[\n.;]", resume_text)
        chunks = [c.strip() for c in raw if len(c.strip()) > 8]
    if not chunks:
        chunks = ["(no resume text provided)"]

    all_texts   = chunks + skill_list
    total_texts = len(all_texts)

    # Count cache hits BEFORE encoding (best-effort, no encoding triggered)
    cache_hits   = 0
    redis_client = _get_redis()
    try:
        if redis_client is not None:
            keys       = [f"skillradar:emb:{_cache_key(t)}" for t in all_texts]
            flags      = redis_client.mget(keys)
            cache_hits = sum(1 for v in flags if v is not None)
        else:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with _shelve_lock:
                with shelve.open(CACHE_PATH) as shelf:
                    cache_hits = sum(1 for t in all_texts if _cache_key(t) in shelf)
    except Exception:
        pass   # cache count is informational only

    all_embeddings   = encode(all_texts, use_cache=True)
    chunk_embeddings = all_embeddings[: len(chunks)]
    skill_embeddings = all_embeddings[len(chunks):]

    log.debug(
        "[EmbeddingEngine] encode_pipeline_inputs  chunks=%d  skills=%d  "
        "cache_hits=%d/%d",
        len(chunks), len(skill_list), cache_hits, total_texts,
    )

    return {
        "resume_chunks":    chunks,
        "chunk_embeddings": chunk_embeddings,
        "skill_embeddings": skill_embeddings,
        "skill_list":       skill_list,
        "n_chunks":         len(chunks),
        "cache_hits":       cache_hits,
        "total_texts":      total_texts,
    }


# ── Similarity helpers ────────────────────────────────────────────────────────

def similarity_scores(
    query:      str,
    candidates: list[str],
) -> list[tuple[str, float]]:
    """
    Cosine similarity between a query string and each candidate.

    Guards:
      - Empty candidates list → returns []
      - Empty-string candidates produce all-zero vectors → score clamped to 0.0
        (cosine_similarity returns nan for zero vectors; we replace with 0.0)

    Returns:
        List of (candidate_text, score) sorted descending by score.
        Scores are in [0.0, 1.0].
    """
    if not candidates:
        return []

    all_texts  = [query] + candidates
    embeddings = encode(all_texts)

    query_emb     = embeddings[0:1]          # (1, 384)
    candidate_emb = embeddings[1:]           # (n, 384)

    # Guard: zero-norm rows produce nan — replace with 0.0
    raw_scores = _cosine_similarity(query_emb, candidate_emb)[0]
    scores     = [0.0 if (s != s) else float(s) for s in raw_scores]  # nan check

    return sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)


def top_matches(
    query:      str,
    candidates: list[str],
    threshold:  float = 0.45,
    top_k:      int   = 5,
) -> list[tuple[str, float]]:
    """Return top-k candidates above similarity threshold."""
    if not candidates:
        return []
    scored = similarity_scores(query, candidates)
    return [(c, s) for c, s in scored[:top_k] if s >= threshold]


def extract_skills_semantic(
    resume_text: str,
    skill_list:  list[str],
    threshold:   float = 0.40,
) -> dict[str, float]:
    """
    Extract skills from resume text using semantic similarity.
    Returns {skill_name: best_similarity_score} for matched skills.
    """
    chunks = [c.strip() for c in re.split(r"[\n.;]", resume_text) if len(c.strip()) > 8]
    if not chunks or not skill_list:
        return {}

    chunk_embeddings = encode(chunks)
    skill_embeddings = encode(skill_list)
    sim_matrix       = _cosine_similarity(skill_embeddings, chunk_embeddings)

    matched: dict[str, float] = {}
    for i, skill in enumerate(skill_list):
        row  = sim_matrix[i]
        best = float(row.max()) if len(row) > 0 else 0.0
        if best != best:   # nan guard
            best = 0.0
        if best >= threshold:
            matched[skill] = round(best, 4)
    return matched


def clear_cache() -> None:
    """Wipe Redis and shelve caches."""
    import glob

    redis_client = _get_redis()
    if redis_client is not None:
        try:
            cursor = 0
            while True:
                cursor, keys = redis_client.scan(
                    cursor, match="skillradar:emb:*", count=500
                )
                if keys:
                    redis_client.delete(*keys)
                if cursor == 0:
                    break
            log.info("[EmbeddingEngine] Redis cache cleared.")
        except Exception as exc:
            log.warning("[EmbeddingEngine] Redis clear error: %s", exc)

    with _shelve_lock:
        for f in glob.glob(CACHE_PATH + ".*"):
            try:
                os.remove(f)
            except OSError:
                pass
    log.info("[EmbeddingEngine] Shelve cache cleared.")
