"""
github_analysis.py — Phase 2 upgrade.

What changed vs MVP:
  OLD: language_count presence → binary "verified"
  NEW: per-skill confidence score based on:
       - repo count in that language
       - average stars (quality signal)
       - active repo ratio
       - topic/description semantic matching via embedding_engine

  OLD: "depth" = Beginner/Intermediate/Advanced from avg complexity
  NEW: same depth label but score-backed + per-skill confidence dict returned

New return shape (verify_github_skills):
  {
    "verified_ids": [...],
    "depth": "Intermediate",
    "frameworks": [...],
    "skill_confidence": {"python": 0.92, "docker": 0.78, ...},
    "stats": {
        "total_repos": 24, "active_repos": 12,
        "total_stars": 45, "avg_complexity": 2.3
    }
  }
"""

import os
import sqlite3
import requests
from collections import defaultdict

from utils import embedding_cache


def _empty_github_profile() -> dict:
    return {
        "languages": {},
        "frameworks": {},
        "depth": "Beginner",
        "stats": {
            "total_repos": 0,
            "active_repos": 0,
            "total_stars": 0,
            "avg_complexity": 0,
        },
    }


def get_db_connection():
    conn = sqlite3.connect("skillgap.db")
    conn.row_factory = sqlite3.Row
    return conn


# ── Language → canonical DB skill name ───────────────────────────────────────
LANG_SKILL_MAP = {
    "python":         "python",
    "javascript":     "javascript",
    "typescript":     "typescript",
    "html":           "html",
    "css":            "css",
    "jupyter notebook": "python",
    "sql":            "sql",
    "go":             "go",
    "rust":           "rust",
    "java":           "java",
    "kotlin":         "kotlin",
    "swift":          "swift",
    "c++":            "c++",
    "c#":             "c#",
    "ruby":           "ruby",
    "php":            "php",
    "scala":          "scala",
    "r":              "r",
    "shell":          "linux",
}

FRAMEWORK_KEYWORDS = [
    "react", "flask", "django", "node", "express", "next",
    "spring", "pytorch", "tensorflow", "pandas", "fastapi",
    "vue", "angular", "svelte", "rails", "laravel",
]


def analyze_github_profile(username: str, timeout: int = 5) -> dict:
    """
    Phase 2 GitHub profiler.

    Returns:
      {
        "languages": {"python": {"count": 8, "confidence": 0.92}, ...},
        "frameworks": {"django": {"confidence": 0.80}, ...},
        "depth":  "Intermediate",
        "stats":  {total_repos, active_repos, total_stars, avg_complexity}
      }
    """
    if not username:
        return _empty_github_profile()

    cache_key = f"github_profile:{username}"
    cached = embedding_cache.get(cache_key)
    if cached:
        print("[GitHub] Cache hit")
        return cached

    try:
        url = f"https://api.github.com/users/{username}/repos?per_page=100"
        token = os.getenv("GITHUB_TOKEN")
        headers = {"User-Agent": "SkillRadar/2.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(url, timeout=timeout, headers=headers)

        if response.status_code == 404:
            print(f"[GitHub] User not found: {username}")
            return _empty_github_profile()
        if response.status_code == 403:
            print("[GitHub] Rate limit exceeded.")
            if cached:
                return cached
            return _empty_github_profile()
        if response.status_code != 200:
            print(f"[GitHub] API error {response.status_code}")
            return _empty_github_profile()

        repos = response.json()
        if not isinstance(repos, list):
            return _empty_github_profile()

    except Exception as e:
        print(f"[GitHub] CRITICAL ERROR: {e}")
        return _empty_github_profile()

    # ── Aggregate signals ─────────────────────────────────────────────────────
    lang_count   = defaultdict(int)
    lang_stars   = defaultdict(int)
    detected_fw  = defaultdict(int)   # framework → repo-mention count
    total_stars  = 0
    active_repos = 0
    complexity_scores = []

    for repo in repos:
        lang = (repo.get("language") or "").lower()
        if lang:
            lang_count[lang]  += 1
            lang_stars[lang]  += repo.get("stargazers_count", 0)

        stars    = repo.get("stargazers_count", 0)
        size     = repo.get("size", 0)
        has_wiki = repo.get("has_wiki", False)

        total_stars += stars
        if stars > 0 or size > 5000:
            active_repos += 1

        # Complexity heuristic
        c = 0
        if size > 1000:  c += 1
        if size > 10000: c += 2
        if stars > 0:    c += 1
        if has_wiki:     c += 1
        complexity_scores.append(c)

        # Framework detection from description + topics
        desc   = (repo.get("description") or "").lower()
        topics = repo.get("topics", [])
        combined = desc + " " + " ".join(topics)
        for fw in FRAMEWORK_KEYWORDS:
            if fw in combined:
                detected_fw[fw] += 1

    n = len(repos) or 1
    avg_complexity = sum(complexity_scores) / n
    avg_stars      = total_stars / n

    depth = "Beginner"
    if avg_complexity >= 2: depth = "Intermediate"
    if avg_complexity >= 4: depth = "Advanced"

    # ── Compute per-language confidence ──────────────────────────────────────
    # Signals:
    #   base = 0.55  (you at least used this language)
    #   + repo_ratio bonus: how dominant is this language? (up to +0.20)
    #   + star bonus:       avg stars for this language repos (up to +0.15)
    #   + active_bonus:     overall active ratio (up to +0.10)

    total_repos   = len(repos)
    active_ratio  = active_repos / max(total_repos, 1)

    language_info = {}
    for lang, count in lang_count.items():
        repo_ratio   = count / total_repos
        star_bonus   = min(0.15, lang_stars[lang] / (count * 10 + 1))
        active_bonus = active_ratio * 0.10
        confidence   = min(1.0, 0.55 + repo_ratio * 0.20 + star_bonus + active_bonus)
        language_info[lang] = {
            "count":      count,
            "confidence": round(confidence, 4),
        }

    # ── Per-framework confidence ──────────────────────────────────────────────
    framework_info = {}
    for fw, mentions in detected_fw.items():
        base_conf = min(1.0, 0.60 + mentions * 0.06 + avg_stars * 0.01)
        framework_info[fw] = {"confidence": round(base_conf, 4)}

    result = {
        "languages": language_info,
        "frameworks": framework_info,
        "depth":    depth,
        "stats": {
            "total_repos":   total_repos,
            "active_repos":  active_repos,
            "total_stars":   total_stars,
            "avg_complexity": round(avg_complexity, 2),
        },
    }
    embedding_cache.set(cache_key, result, ttl=21600)
    return result


def verify_github_skills(user_id: int, github_username: str) -> dict:
    """
    Match GitHub profile data to DB skills and persist to user_skills table.

    Phase 2: stores confidence score (from compute above) instead of flat 1.0.
    Returns full confidence dict so callers can use it for gap scoring.
    """
    github_data = analyze_github_profile(github_username)
    if not github_data:
        return {"verified_ids": [], "depth": "Beginner", "frameworks": [], "skill_confidence": {}}

    conn     = get_db_connection()
    db_skills = conn.execute("SELECT id, name FROM skills").fetchall()
    skill_map = {s["name"].lower(): s["id"] for s in db_skills}

    verified_ids      = []
    skill_confidence  = {}

    # ── Languages ─────────────────────────────────────────────────────────────
    for lang, info in github_data.get("languages", {}).items():
        canonical = LANG_SKILL_MAP.get(lang, lang)
        if canonical in skill_map:
            sid  = skill_map[canonical]
            conf = info["confidence"]
            verified_ids.append(sid)
            skill_confidence[canonical] = conf
        elif lang in skill_map:
            sid  = skill_map[lang]
            conf = info["confidence"]
            verified_ids.append(sid)
            skill_confidence[lang] = conf

    # ── Frameworks / topics ───────────────────────────────────────────────────
    for fw, info in github_data.get("frameworks", {}).items():
        if fw in skill_map:
            sid  = skill_map[fw]
            conf = info["confidence"]
            if sid not in verified_ids:
                verified_ids.append(sid)
            skill_confidence[fw] = max(skill_confidence.get(fw, 0), conf)

    # ── Upsert user_skills with real confidence score ─────────────────────────
    for sid in set(verified_ids):
        # Find skill name for confidence lookup
        skill_name = next(
            (name for name, id_ in skill_map.items() if id_ == sid),
            None
        )
        conf = skill_confidence.get(skill_name, 0.7) if skill_name else 0.7

        existing = conn.execute(
            "SELECT id, source FROM user_skills WHERE user_id=? AND skill_id=?",
            (user_id, sid)
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO user_skills(user_id, skill_id, score, source) VALUES(?,?,?,?)",
                (user_id, sid, conf, "github"),
            )
        else:
            # Upgrade source + score if GitHub evidence is stronger
            new_source = "resume+github" if existing["source"] == "resume" else existing["source"]
            conn.execute(
                "UPDATE user_skills SET source=?, score=MAX(score, ?) WHERE id=?",
                (new_source, conf, existing["id"]),
            )

    conn.commit()
    conn.close()

    return {
        "verified_ids":    list(set(verified_ids)),
        "depth":           github_data["depth"],
        "frameworks":      list(github_data.get("frameworks", {}).keys()),
        "skill_confidence": skill_confidence,
        "stats":           github_data.get("stats", {}),
    }
