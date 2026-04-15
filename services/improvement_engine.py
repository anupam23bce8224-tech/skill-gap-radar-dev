"""
improvement_engine.py — SkillRadar (Improved)

Priority-scored recommendation engine.

Priority formula:
    priority = (market_demand × effective_gap × impact) / log2(1 + effort_days)

    effective_gap = gap_ratio × (1 - github_confidence × 0.4)

    → Skills the user already has GitHub evidence for get downweighted.
      Truly unknown skills with high market demand rise to the top.

What changed vs previous version:

1. GITHUB DISCOUNT — properly integrated into both get_next_best_action()
   and rank_all_actions(). Previously the param was accepted but ignored
   in the priority score calculation; now eff_gap uses it directly.

2. NORMALIZATION — _priority_score() input sanitised (clamped, not raw)
   so floating-point edge cases don't produce negative scores.

3. LOGGING — replaced all print() with log.debug() / log.info().

4. EFFORT CAP — max_effort is looked up via dict.get() with a sane default
   instead of raising KeyError for unknown user_level strings.

5. EMPTY SKILLS — all public functions handle empty missing_skills without
   returning malformed dicts.

Public API (unchanged signatures):
    get_next_best_action(missing_skills, focus_role, user_level,
                         matched_skills, github_confidence) → dict
    rank_all_actions(missing_skills, focus_role, user_level,
                     top_k, github_confidence) → list[dict]
    get_improvements_for_skill(skill_name) → list[dict]
"""

from __future__ import annotations

import logging
import math
from typing import Optional

log = logging.getLogger("SkillRadar.improvement")

# ── Action database ────────────────────────────────────────────────────────────
ACTIONS_DB: dict[str, list[dict]] = {
    "python": [
        {"task": "Solve 10 LeetCode array/string problems in Python",         "time_days": 2,   "impact": 8,  "resource": "https://leetcode.com"},
        {"task": "Build a CLI tool or web scraper with requests/BeautifulSoup","time_days": 1,   "impact": 12, "resource": "https://realpython.com"},
        {"task": "Complete Python OOP mini-course",                            "time_days": 3,   "impact": 14, "resource": "https://realpython.com/python3-object-oriented-programming"},
    ],
    "rest apis": [
        {"task": "Build a CRUD REST API with Flask + SQLite",                  "time_days": 2,   "impact": 16, "resource": "https://flask.palletsprojects.com"},
        {"task": "Implement JWT Authentication on an existing API",            "time_days": 1,   "impact": 12, "resource": "https://jwt.io/introduction"},
        {"task": "Write OpenAPI/Swagger docs for your API",                    "time_days": 0.5, "impact": 8,  "resource": "https://swagger.io/docs"},
    ],
    "api design": [
        {"task": "Design a RESTful API for a todo app following best practices","time_days": 1,  "impact": 14, "resource": "https://restfulapi.net"},
        {"task": "Implement versioning and pagination in an API",               "time_days": 1,  "impact": 10, "resource": "https://restfulapi.net/resource-naming"},
    ],
    "docker": [
        {"task": "Write a Dockerfile for a Python Flask app",                  "time_days": 1,   "impact": 16, "resource": "https://docs.docker.com/get-started"},
        {"task": "Set up docker-compose with app + PostgreSQL",                "time_days": 2,   "impact": 20, "resource": "https://docs.docker.com/compose"},
        {"task": "Publish your image to Docker Hub",                           "time_days": 0.5, "impact": 8,  "resource": "https://hub.docker.com"},
    ],
    "react": [
        {"task": "Build a stateful todo app using React Hooks",                "time_days": 1,   "impact": 12, "resource": "https://react.dev/learn"},
        {"task": "Implement React Router in a multi-page demo app",            "time_days": 2,   "impact": 14, "resource": "https://reactrouter.com"},
        {"task": "Fetch data from a public API and render it with React",      "time_days": 1,   "impact": 12, "resource": "https://react.dev/learn/synchronizing-with-effects"},
    ],
    "machine learning": [
        {"task": "Complete Andrew Ng's ML Specialization Week 1–3",            "time_days": 7,   "impact": 18, "resource": "https://www.coursera.org/specializations/machine-learning-introduction"},
        {"task": "Build a classification model with scikit-learn on Kaggle",   "time_days": 3,   "impact": 16, "resource": "https://kaggle.com"},
        {"task": "Document model performance with a Jupyter notebook report",  "time_days": 1,   "impact": 10, "resource": "https://jupyter.org"},
    ],
    "scikit-learn": [
        {"task": "Train a regression + classification model on Titanic dataset","time_days": 2,  "impact": 14, "resource": "https://scikit-learn.org/stable/tutorial"},
        {"task": "Build a cross-validated pipeline with preprocessing + model", "time_days": 2,  "impact": 16, "resource": "https://scikit-learn.org/stable/modules/pipeline.html"},
    ],
    "sql": [
        {"task": "Complete SQLZoo or Mode SQL Tutorial (intermediate level)",  "time_days": 2,   "impact": 14, "resource": "https://sqlzoo.net"},
        {"task": "Design a normalised schema for a blog and write 10 queries", "time_days": 1,   "impact": 12, "resource": "https://www.postgresqltutorial.com"},
    ],
    "git": [
        {"task": "Learn branching, rebasing, and PR workflows via learngitbranching.js.org","time_days": 1,"impact": 10,"resource": "https://learngitbranching.js.org"},
        {"task": "Set up a GitHub Actions CI pipeline for a project",          "time_days": 1,   "impact": 14, "resource": "https://docs.github.com/en/actions"},
    ],
    "pytorch": [
        {"task": "Implement a feedforward neural net on MNIST from scratch",   "time_days": 3,   "impact": 16, "resource": "https://pytorch.org/tutorials"},
        {"task": "Fine-tune a pre-trained ResNet on a custom image dataset",   "time_days": 4,   "impact": 18, "resource": "https://pytorch.org/tutorials/beginner/transfer_learning_tutorial.html"},
    ],
    "tensorflow": [
        {"task": "Complete TensorFlow's official Keras beginner guide",        "time_days": 2,   "impact": 14, "resource": "https://www.tensorflow.org/tutorials"},
        {"task": "Train a text classification model with TF Hub embeddings",   "time_days": 3,   "impact": 16, "resource": "https://www.tensorflow.org/hub"},
    ],
    "django": [
        {"task": "Build a blog app with Django (models, views, templates)",    "time_days": 3,   "impact": 16, "resource": "https://docs.djangoproject.com/en/stable/intro/tutorial01"},
        {"task": "Add DRF (Django REST Framework) to expose a JSON API",       "time_days": 2,   "impact": 18, "resource": "https://www.django-rest-framework.org"},
    ],
    "flask": [
        {"task": "Build a REST API backend with Flask-SQLAlchemy + Marshmallow","time_days": 2,  "impact": 14, "resource": "https://flask-sqlalchemy.palletsprojects.com"},
    ],
    "postgresql": [
        {"task": "Set up PostgreSQL locally, migrate a SQLite project to it",  "time_days": 1,   "impact": 12, "resource": "https://www.postgresql.org/docs/current/tutorial.html"},
        {"task": "Write window functions and CTEs for analytical queries",     "time_days": 1,   "impact": 10, "resource": "https://mode.com/sql-tutorial/sql-window-functions"},
    ],
    "html": [
        {"task": "Build a semantic HTML5 personal portfolio page",             "time_days": 1,   "impact": 8,  "resource": "https://developer.mozilla.org/en-US/docs/Web/HTML"},
    ],
    "css": [
        {"task": "Recreate a Dribbble UI card in pure CSS (Flexbox + Grid)",   "time_days": 1,   "impact": 10, "resource": "https://css-tricks.com/snippets/css/a-guide-to-flexbox"},
    ],
    "javascript": [
        {"task": "Solve 10 JS Codewars katas (string/array manipulation)",     "time_days": 2,   "impact": 10, "resource": "https://www.codewars.com"},
        {"task": "Build a vanilla JS single-page app with fetch + DOM",        "time_days": 2,   "impact": 14, "resource": "https://javascript.info"},
    ],
    "typescript": [
        {"task": "Migrate a small JS project to TypeScript with strict mode",  "time_days": 1,   "impact": 12, "resource": "https://www.typescriptlang.org/docs"},
    ],
    "pandas": [
        {"task": "Perform EDA on a Kaggle CSV dataset (cleaning, groupby, plots)","time_days": 2,"impact": 14, "resource": "https://pandas.pydata.org/docs/getting_started/index.html"},
    ],
    "numpy": [
        {"task": "Implement linear algebra operations using NumPy arrays",     "time_days": 1,   "impact": 10, "resource": "https://numpy.org/learn"},
    ],
    "fastapi": [
        {"task": "Build a FastAPI microservice with async endpoints + Pydantic","time_days": 2,  "impact": 16, "resource": "https://fastapi.tiangolo.com/tutorial"},
    ],
    "redis": [
        {"task": "Add Redis caching to an existing Flask/FastAPI endpoint",    "time_days": 1,   "impact": 12, "resource": "https://redis.io/docs/clients/python"},
    ],
    "linux": [
        {"task": "Complete LinuxCommand.org chapters 1–12 + write 5 shell scripts","time_days": 3,"impact": 10,"resource": "https://linuxcommand.org/tlcl.php"},
    ],
    "statistics": [
        {"task": "Complete Khan Academy Statistics & Probability unit",        "time_days": 5,   "impact": 16, "resource": "https://www.khanacademy.org/math/statistics-probability"},
    ],
    "next.js": [
        {"task": "Build a blog with Next.js SSG + dynamic routes",             "time_days": 3,   "impact": 14, "resource": "https://nextjs.org/learn"},
    ],
    "tailwind": [
        {"task": "Redesign an HTML project using Tailwind utility classes",    "time_days": 1,   "impact": 10, "resource": "https://tailwindcss.com/docs"},
    ],
    "node.js": [
        {"task": "Build a REST API with Express.js + MongoDB (Mongoose)",      "time_days": 3,   "impact": 14, "resource": "https://expressjs.com/en/starter/installing.html"},
    ],
    "mlops": [
        {"task": "Track ML experiments with MLflow on a scikit-learn project", "time_days": 2,   "impact": 16, "resource": "https://mlflow.org/docs/latest/tutorials-and-examples/tutorial.html"},
        {"task": "Deploy a model as a FastAPI endpoint and containerise it",   "time_days": 2,   "impact": 18, "resource": "https://fastapi.tiangolo.com"},
    ],
    "cuda": [
        {"task": "Run a PyTorch model on GPU and benchmark CPU vs GPU speed",  "time_days": 1,   "impact": 12, "resource": "https://pytorch.org/tutorials/beginner/blitz/tensor_tutorial.html"},
    ],
    "celery": [
        {"task": "Add background tasks to a Flask app with Celery + Redis",    "time_days": 2,   "impact": 12, "resource": "https://docs.celeryq.dev/en/stable/getting-started/first-steps-with-celery.html"},
    ],
    "testing": [
        {"task": "Write pytest unit tests for a Python module (aim 80% coverage)","time_days": 2,"impact": 14,"resource": "https://docs.pytest.org/en/stable/getting-started.html"},
    ],
    "matplotlib": [
        {"task": "Produce 5 publication-quality plots from a dataset",         "time_days": 1,   "impact": 8,  "resource": "https://matplotlib.org/stable/tutorials/index.html"},
    ],
    "jupyter": [
        {"task": "Convert a Python analysis script into a Jupyter notebook",   "time_days": 0.5, "impact": 6,  "resource": "https://jupyter.org/documentation"},
    ],
}

# ── Market demand multipliers ─────────────────────────────────────────────────
MARKET_DEMAND: dict[str, float] = {
    "python": 2.0, "javascript": 1.9, "react": 1.8, "sql": 1.8,
    "docker": 1.7, "git": 1.6, "machine learning": 1.8, "rest apis": 1.7,
    "api design": 1.6, "typescript": 1.6, "node.js": 1.5, "postgresql": 1.4,
    "fastapi": 1.5, "django": 1.4, "flask": 1.3, "pytorch": 1.6,
    "tensorflow": 1.5, "scikit-learn": 1.5, "pandas": 1.5, "numpy": 1.4,
    "mlops": 1.7, "next.js": 1.5, "redis": 1.3, "linux": 1.3,
    "statistics": 1.5, "css": 1.4, "html": 1.3, "tailwind": 1.4,
    "testing": 1.5, "celery": 1.1, "cuda": 1.3, "jupyter": 1.1,
    "matplotlib": 1.1,
}
DEFAULT_MARKET_DEMAND = 1.0

# ── Effort cap per user level ─────────────────────────────────────────────────
_EFFORT_CAP: dict[str, float] = {
    "beginner":     2.0,
    "intermediate": 5.0,
    "advanced":     10.0,
}
_DEFAULT_EFFORT_CAP = 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _priority_score(
    gap_weight: float,
    market_demand: float,
    effort_days: float,
    impact: float,
) -> float:
    """
    Compute a priority score for a single (skill, action) pair.

    Formula:
        priority = (market_demand × gap_weight × impact) / log2(1 + effort_days)

    All inputs are clamped to prevent degenerate values.
    """
    gap_weight    = max(0.0, min(1.0, gap_weight))
    market_demand = max(0.1, market_demand)
    effort_days   = max(0.5, effort_days)
    impact        = max(0.0, impact)

    numerator     = market_demand * gap_weight * impact
    denominator   = math.log2(1.0 + effort_days)
    return round(numerator / denominator, 4)


def _get_actions_for_skill(skill: str) -> list[dict]:
    """
    Return the action list for a skill.
    Falls back to semantic nearest-neighbour, then generic tasks.
    """
    skill_lower = skill.lower()

    if skill_lower in ACTIONS_DB:
        return ACTIONS_DB[skill_lower]

    # Semantic nearest-neighbour
    try:
        from .embedding_engine import top_matches
        hits = top_matches(skill_lower, list(ACTIONS_DB.keys()), threshold=0.50, top_k=1)
        if hits:
            nearest, sim = hits[0]
            log.debug("[ImprovementEngine] '%s' → nearest: '%s' (sim=%.2f)", skill, nearest, sim)
            return ACTIONS_DB[nearest]
    except Exception:
        pass

    # Generic fallback
    return [
        {
            "task":     f"Complete a structured course on {skill}",
            "time_days": 3,
            "impact":   10,
            "resource": f"https://www.google.com/search?q={skill}+tutorial",
        },
        {
            "task":     f"Build a mini-project that prominently uses {skill}",
            "time_days": 3,
            "impact":   14,
            "resource": f"https://github.com/search?q={skill}+tutorial",
        },
    ]


def _effective_gap(
    gap_ratio: float,
    skill: str,
    github_confidence: dict[str, float],
) -> float:
    """
    Discount the gap weight if the user already has GitHub exposure to this skill.

    A skill where GitHub confidence = 1.0 is discounted to 60% of the gap_ratio.
    A skill with no GitHub exposure keeps the full gap_ratio.
    """
    gh_conf = github_confidence.get(skill.lower(), 0.0)
    return gap_ratio * (1.0 - gh_conf * 0.4)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_improvements_for_skill(skill_name: str) -> list[dict]:
    """Return the top-3 improvement tasks for a single skill."""
    actions = _get_actions_for_skill(skill_name)
    return [
        {
            "task":     a["task"],
            "time":     f"{a['time_days']} day{'s' if a['time_days'] != 1 else ''}",
            "resource": a.get("resource", ""),
        }
        for a in actions[:3]
    ]


def get_next_best_action(
    missing_skills: list[str],
    focus_role: str,
    user_level: str = "intermediate",
    matched_skills: Optional[list[str]] = None,
    github_confidence: Optional[dict[str, float]] = None,
) -> dict:
    """
    Return the single highest-priority next action across all missing skills.

    GitHub confidence properly discounts skills the user already has
    partial evidence for, surfacing truly unknown skills to the top.
    """
    if not missing_skills:
        return {
            "skill":          "",
            "task":           "Polish your resume and start applying!",
            "time":           "1 day",
            "impact":         "+5% Confidence",
            "resource":       "",
            "reason":         "You meet all core requirements for this role.",
            "priority_score": 0.0,
            "github_exposure": 0.0,
        }

    github_confidence = github_confidence or {}
    matched_count     = len(matched_skills) if matched_skills else 0
    total_skills      = matched_count + len(missing_skills)
    gap_ratio         = len(missing_skills) / max(total_skills, 1)
    max_effort        = _EFFORT_CAP.get(user_level, _DEFAULT_EFFORT_CAP)

    candidates: list[dict] = []
    for skill in missing_skills:
        actions  = _get_actions_for_skill(skill)
        demand   = MARKET_DEMAND.get(skill.lower(), DEFAULT_MARKET_DEMAND)
        eff_gap  = _effective_gap(gap_ratio, skill, github_confidence)
        gh_conf  = github_confidence.get(skill.lower(), 0.0)

        for action in actions:
            effort = min(action["time_days"], max_effort)
            score  = _priority_score(eff_gap, demand, effort, action["impact"])
            candidates.append({
                "skill":           skill,
                "task":            action["task"],
                "time_days":       action["time_days"],
                "impact":          action["impact"],
                "resource":        action.get("resource", ""),
                "priority_score":  score,
                "market_demand":   demand,
                "github_exposure": round(gh_conf, 3),
            })

    if not candidates:
        return {
            "skill":           missing_skills[0],
            "task":            f"Complete a crash course on {missing_skills[0]}",
            "time":            "2 days",
            "resource":        "",
            "impact":          "+10% Score",
            "reason":          f"{missing_skills[0]} is critical for {focus_role}.",
            "priority_score":  0.0,
            "github_exposure": 0.0,
        }

    best    = max(candidates, key=lambda x: x["priority_score"])
    gh_note = (
        f" (partial GitHub exposure: {best['github_exposure']:.0%})"
        if best["github_exposure"] > 0
        else ""
    )

    return {
        "skill":           best["skill"],
        "task":            best["task"],
        "time":            f"{best['time_days']} day{'s' if best['time_days'] != 1 else ''}",
        "resource":        best["resource"],
        "impact":          f"+{best['impact']}% Score",
        "priority_score":  best["priority_score"],
        "github_exposure": best["github_exposure"],
        "reason": (
            f"{best['skill']} has high market demand (×{best['market_demand']})"
            f" and is critical for the {focus_role} role{gh_note}."
        ),
    }


def rank_all_actions(
    missing_skills: list[str],
    focus_role: str,
    user_level: str = "intermediate",
    top_k: int = 5,
    github_confidence: Optional[dict[str, float]] = None,
) -> list[dict]:
    """
    Return the top-k ranked actions across all missing skills.

    GitHub confidence discounts skills with partial evidence so truly
    unknown, high-demand skills appear first.
    """
    if not missing_skills:
        return []

    github_confidence = github_confidence or {}
    max_effort        = _EFFORT_CAP.get(user_level, _DEFAULT_EFFORT_CAP)

    all_candidates: list[dict] = []
    for skill in missing_skills:
        actions  = _get_actions_for_skill(skill)
        demand   = MARKET_DEMAND.get(skill.lower(), DEFAULT_MARKET_DEMAND)
        # In rank_all_actions every skill is fully missing (gap_ratio = 1.0)
        # but GitHub exposure still discounts the effective weight.
        eff_gap  = _effective_gap(1.0, skill, github_confidence)
        gh_conf  = github_confidence.get(skill.lower(), 0.0)

        for action in actions:
            effort = min(action["time_days"], max_effort)
            score  = _priority_score(eff_gap, demand, effort, action["impact"])
            all_candidates.append({
                "skill":           skill,
                "task":            action["task"],
                "time":            f"{action['time_days']} day{'s' if action['time_days'] != 1 else ''}",
                "resource":        action.get("resource", ""),
                "impact":          f"+{action['impact']}% Score",
                "priority_score":  round(score, 2),
                "market_demand":   demand,
                "github_exposure": round(gh_conf, 3),
            })

    return sorted(all_candidates, key=lambda x: x["priority_score"], reverse=True)[:top_k]