"""SQLite storage. `tweets` caches every tweet we render (opinions, their quoted tweets,
thread continuations and top replies); `reports` holds the candidate opinions and their grades.

An opinion is an original tweet or a quote tweet, together with the author's own thread under
it. Replies to other people never become candidates."""

import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id TEXT PRIMARY KEY,
    account_id TEXT,
    username TEXT,
    display_name TEXT,
    avatar TEXT,
    created_at TEXT,
    full_text TEXT,
    likes INTEGER DEFAULT 0,
    retweets INTEGER DEFAULT 0,
    reply_to_tweet_id TEXT,
    reply_to_username TEXT,
    quoted_tweet_id TEXT,
    conversation_id TEXT,
    media TEXT DEFAULT '[]',
    urls TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS reports (
    tweet_id TEXT PRIMARY KEY,
    created_at TEXT,
    searches TEXT,             -- labels of the searches that matched (config.SEARCHES)
    hydrated INTEGER DEFAULT 0,
    unit TEXT,                 -- 'original' or 'quote' once hydrated (see UNITS)
    thread_ids TEXT DEFAULT '[]', -- the author's own continuation of the tweet, oldest first
    reply_ids TEXT DEFAULT '[]',
    is_report INTEGER,         -- NULL until graded; 1 = a human opinion on the topic with a stance
    kind TEXT,
    stance TEXT,               -- a config.STANCES slug, or 'none'
    score INTEGER,             -- substance, 1-10
    claim TEXT,                -- the author's point in a few words
    classified_at TEXT,
    quotes INTEGER DEFAULT 0,
    quotes_at TEXT,
    arguments TEXT,            -- ids from arguments.json the opinion makes (JSON list); shown opinions only
    arguments_version INTEGER, -- version of arguments.json they were tagged with
    tagged_at TEXT
);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);

CREATE TABLE IF NOT EXISTS stance_summaries (
    slug TEXT PRIMARY KEY,
    one_liner TEXT,
    aggregate TEXT,
    portrait TEXT,
    n_reports INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT, command TEXT, seconds INTEGER, status TEXT, error TEXT,
    counters TEXT, cost REAL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    ts TEXT, purpose TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL
);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(reports)")}
    for col, kind in (("arguments", "TEXT"), ("arguments_version", "INTEGER"), ("tagged_at", "TEXT"),
                      ("unit", "TEXT"), ("thread_ids", "TEXT DEFAULT '[]'")):
        if col not in cols:  # databases saved by older versions
            con.execute(f"ALTER TABLE reports ADD COLUMN {col} {kind}")
    if "unit" not in cols:
        # Saved before replies were excluded: fetch every candidate again, which drops replies,
        # turns thread continuations into their thread's first tweet and records units.
        con.execute("UPDATE reports SET hydrated=0")
    if "conversation_id" not in {r["name"] for r in con.execute("PRAGMA table_info(tweets)")}:
        con.execute("ALTER TABLE tweets ADD COLUMN conversation_id TEXT")
    con.commit()
    return con


UNITS = ("original", "quote")


def shown(alias: str = "r") -> str:
    """SQL condition for opinions the site shows; takes MIN_SCORE as its one parameter."""
    a = f"{alias}." if alias else ""
    return f"{a}is_report=1 AND {a}score>=? AND {a}unit IN ('original','quote')"


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, value))
    con.commit()


def prune(con, min_score: int) -> int:
    """Delete stored tweets the site neither shows nor uses as context, and the gists of
    graded tweets it doesn't show. Their rows in `reports` keep the grades, so they are never
    graded again (set hydrated=0 on one to fetch its tweet back). Returns tweets deleted."""
    keep = set()
    for r in con.execute("SELECT r.tweet_id, r.reply_ids, r.thread_ids, t.reply_to_tweet_id, t.quoted_tweet_id "
                         "FROM reports r LEFT JOIN tweets t USING(tweet_id) "
                         f"WHERE (r.is_report IS NULL AND (r.unit IS NULL OR r.unit IN {UNITS})) OR ({shown()})",
                         (min_score,)):
        keep.update(x for x in (r["tweet_id"], r["reply_to_tweet_id"], r["quoted_tweet_id"]) if x)
        keep.update(json.loads(r["reply_ids"] or "[]"))
        keep.update(json.loads(r["thread_ids"] or "[]"))
    con.execute("CREATE TEMP TABLE keep(tweet_id TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO keep VALUES (?)", [(k,) for k in keep])
    deleted = con.execute("DELETE FROM tweets WHERE tweet_id NOT IN (SELECT tweet_id FROM keep)").rowcount
    con.execute(f"UPDATE reports SET claim='' WHERE is_report IS NOT NULL AND NOT ({shown('')})", (min_score,))
    con.execute("DROP TABLE keep")
    con.commit()
    con.execute("VACUUM")
    return deleted


TWEET_COLS = ["tweet_id", "account_id", "username", "display_name", "avatar", "created_at",
              "full_text", "likes", "retweets", "reply_to_tweet_id", "reply_to_username",
              "quoted_tweet_id", "conversation_id", "media", "urls"]


def upsert_tweet(con, t: dict):
    row = {c: t.get(c) for c in TWEET_COLS}
    row["media"] = json.dumps(t.get("media") or [])
    row["urls"] = json.dumps(t.get("urls") or [])
    con.execute(
        f"INSERT INTO tweets({','.join(TWEET_COLS)}) VALUES ({','.join('?' * len(TWEET_COLS))}) "
        "ON CONFLICT(tweet_id) DO UPDATE SET "
        + ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in TWEET_COLS[1:]),
        [row[c] for c in TWEET_COLS],
    )
