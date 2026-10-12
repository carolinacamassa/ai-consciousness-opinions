"""SQLite storage. `tweets` caches every tweet we render (opinions, parents,
quotes, replies); `reports` holds the search matches and their grades."""

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
    media TEXT DEFAULT '[]',
    urls TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS reports (
    tweet_id TEXT PRIMARY KEY,
    created_at TEXT,
    searches TEXT,             -- labels of the searches that matched (config.SEARCHES)
    hydrated INTEGER DEFAULT 0,
    reply_ids TEXT DEFAULT '[]',
    is_report INTEGER,         -- NULL until graded; 1 = a human opinion on the topic with a stance
    kind TEXT,
    stance TEXT,               -- a config.STANCES slug, or 'none'
    score INTEGER,             -- substance, 1-10
    claim TEXT,                -- the author's point in a few words
    classified_at TEXT,
    quotes INTEGER DEFAULT 0,
    quotes_at TEXT
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
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, value))
    con.commit()


TWEET_COLS = ["tweet_id", "account_id", "username", "display_name", "avatar", "created_at",
              "full_text", "likes", "retweets", "reply_to_tweet_id", "reply_to_username",
              "quoted_tweet_id", "media", "urls"]


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
