"""Search terms, stances, and pipeline knobs."""

import math
import os
from pathlib import Path

ROOT = Path(os.environ.get("ACO_ROOT", Path(__file__).resolve().parent.parent))
DB_PATH = Path(os.environ.get("ACO_DB", ROOT / "data" / "aco.sqlite"))
OUT_DIR = Path(os.environ.get("ACO_OUT", ROOT / "dist"))

SITE_NAME = "AI Consciousness Opinions"
SITE_URL = os.environ.get("ACO_SITE_URL", "")  # absolute URL used in llms.txt; empty = relative links
# Path the site is served under: "/" for its own domain, "/<repo>/" for a GitHub Pages project site.
_base = os.environ.get("ACO_BASE", "").strip("/")
BASE = f"/{_base}/" if _base else "/"
GITHUB_URL = "https://github.com/carolinacamassa/ai-consciousness-opinions"
UPSTREAM_URL = "https://github.com/aVariengien/model-behavior-reports"
UMAMI_SCRIPT = os.environ.get("ACO_UMAMI_SCRIPT", "")
UMAMI_WEBSITE_ID = os.environ.get("ACO_UMAMI_ID", "")

CA_URL = "https://fabxmporizzqflnftavs.supabase.co/rest/v1/"
# Public anon key, published in https://www.community-archive.org/llms.txt
CA_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYnhtcG9yaXp6cWZsbmZ0YXZzIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MjIyNDQ5MTIsImV4cCI6MjAzNzgyMDkxMn0.UIEJiUNkLsW28tBHmG-RQDW-I5JNlJLt62CSk9D_qG8"
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CLASSIFY_MODEL = "google/gemini-3.8-flash"
SUMMARY_MODEL = "anthropic/claude-opus-5.5"
CLASSIFY_MODEL_NAME = "Gemini 3.8 Flash"
CLASSIFY_REASONING = "low"
SUMMARY_MODEL_NAME = "Claude Opus 5.5"
TAG_MODEL = "google/gemini-3.8-flash"  # argument tags; see aco/arguments.py
TAG_REASONING = "low"
TAG_PER_RUN = 400          # cap per scheduled run; a new version of arguments.json is re-tagged gradually

START_DATE = "2026-06-01"  # collect tweets posted on or after this day; moving it earlier backfills on the next update
MAX_REPLIES = 10           # replies kept per opinion (most liked), shown under the card
CLASSIFY_PER_RUN = 400     # cap per scheduled run, newest first; backfill drains gradually
MIN_SCORE = 4              # substance needed to be shown on the site
SUMMARY_EVERY_HOURS = 168  # regenerate a stance summary at most this often (~$0.45 each)
SUMMARY_MIN_NEW = 5        # ...and only if at least this many new opinions arrived
SUMMARY_MAX_REPORTS = 150  # opinions fed to the summarizer per stance
SUMMARY_PER_AUTHOR = 4     # at most this many per author, so prolific posters don't set the summary

# Ranking: quotes by archive accounts dominate; retweets count on a log scale
# that saturates at RANK_RT_CAP; the grader's substance score breaks ties.
RANK_QUOTE = 50
RANK_RT = 20
RANK_RT_CAP = 100
RANK_SCORE = 15


def rank(quotes, retweets, score) -> float:
    return (RANK_QUOTE * (quotes or 0)
            + RANK_RT * math.log2(1 + min(retweets or 0, RANK_RT_CAP))
            + RANK_SCORE * (score or 0))


# Full-text searches run against the archive's `fts` column (Postgres websearch syntax,
# English stemming: "conscious" also matches "consciousness" and "consciously").
# Most hits are not about AI; the grader sorts them out.
SEARCHES = {
    "conscious": "conscious",
    "sentient": "sentient OR sentience",
    "qualia": "qualia",
    "experience": '"subjective experience" OR "inner experience" OR "phenomenal experience" OR "inner life"',
    "welfare": '"model welfare" OR "ai welfare" OR "digital minds" OR "digital welfare"',
    "moral status": '"moral patient" OR "moral patients" OR "moral patienthood" OR "moral status" OR "moral consideration"',
    "rights": '"ai rights" OR "robot rights" OR "model rights"',
    "self-aware": '"self aware" OR "self-aware" OR "self awareness"',
}

# The three stances shown on the site, in display order. Labels are the only place
# the wording lives; slugs are what the grader returns and what URLs use.
STANCES = [
    dict(slug="affirming", name="Affirming", color="#2a64d6",
         blurb="AI systems are, or may well be, conscious, or they are owed moral consideration."),
    dict(slug="skeptical", name="Skeptical", color="#c2562f",
         blurb="AI systems are not conscious, or talk of their welfare and moral status is misplaced."),
    dict(slug="uncertain", name="Uncertain", color="#7a45d6",
         blurb="The question is open, unknowable for now, or badly posed, without leaning either way."),
]
STANCE_BY_SLUG = {s["slug"]: s for s in STANCES}
