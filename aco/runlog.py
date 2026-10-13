"""Per-run counters and LLM usage, saved to the DB for the status page."""

import json
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
COUNTERS: dict[str, int] = {}
CALLS: list[tuple] = []  # (ts, purpose, model, prompt_tokens, completion_tokens, cost_usd)


def add(key: str, n: int = 1):
    with _lock:
        COUNTERS[key] = COUNTERS.get(key, 0) + n


def llm_call(purpose: str, model: str, usage: dict):
    with _lock:
        CALLS.append((datetime.now(timezone.utc).isoformat(timespec="seconds"), purpose, model,
                      usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0,
                      float(usage.get("cost") or 0)))


def save(con, command: str, started: datetime, status: str, error: str = "") -> int:
    """Write this run and its LLM calls; called once per run. Returns the run's row id."""
    now = datetime.now(timezone.utc)
    with _lock:
        cost = sum(c[5] for c in CALLS)
        con.executemany("INSERT INTO llm_calls(ts, purpose, model, prompt_tokens, completion_tokens, cost) "
                        "VALUES (?,?,?,?,?,?)", CALLS)
        rowid = con.execute("INSERT INTO runs(started_at, command, seconds, status, error, counters, cost) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (started.isoformat(timespec="seconds"), command, round((now - started).total_seconds()),
                             status, error[:500], json.dumps(COUNTERS), cost)).lastrowid
        CALLS.clear()
        COUNTERS.clear()
    con.commit()
    return rowid


def fail(con, rowid: int, error: str):
    """Mark a run saved early (before the build) as failed after all."""
    con.execute("UPDATE runs SET status='error', error=? WHERE rowid=?", (error[:500], rowid))
    con.commit()
