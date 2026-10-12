"""Find tweets matching the searches and hydrate them with parent, quote and replies."""

import json
from datetime import datetime, timedelta, timezone

from . import archive, config, db, runlog

SCAN_SELECT = ("tweet_id,created_at,updated_at,full_text,favorite_count,retweet_count,"
               "reply_to_tweet_id")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ingest(con, rows: list[dict], label: str) -> int:
    """Register new candidates from a page of search results and refresh counts of
    tweets we already hold. Returns the number of new candidates."""
    cutoff = _iso(_now() - timedelta(days=config.WINDOW_DAYS))

    # Engagement counts move after capture; keep ours current.
    con.executemany(
        "UPDATE tweets SET likes=MAX(likes,?), retweets=MAX(retweets,?) WHERE tweet_id=?",
        [(r["favorite_count"] or 0, r["retweet_count"] or 0, r["tweet_id"]) for r in rows
         if not r["full_text"].startswith("RT @")])
    # A new reply to a known opinion: re-hydrate it to pick up the reply.
    reply_parents = {r["reply_to_tweet_id"] for r in rows if r.get("reply_to_tweet_id")}
    if reply_parents:
        con.executemany("UPDATE reports SET hydrated=0 WHERE tweet_id=? AND hydrated=1",
                        [(p,) for p in reply_parents])

    found, retweets = [], {}
    for r in rows:
        if r["created_at"] < cutoff:
            continue
        if r["full_text"].startswith("RT @"):
            retweets[r["tweet_id"]] = r
        else:
            found.append(r["tweet_id"])

    # Retweets are not opinions, but they carry the original's current retweet
    # count; use it to refresh originals we already hold.
    if retweets:
        for rt_id, orig_id in archive.retweet_targets(retweets).items():
            if orig_id:
                con.execute("UPDATE tweets SET retweets=MAX(retweets,?) WHERE tweet_id=?",
                            (retweets[rt_id]["retweet_count"] or 0, orig_id))

    runlog.add("scanned", len(rows))
    new = 0
    for tid in found:
        old = con.execute("SELECT searches FROM reports WHERE tweet_id=?", (tid,)).fetchone()
        if old:
            labels = json.loads(old["searches"] or "[]")
            if label not in labels:
                con.execute("UPDATE reports SET searches=? WHERE tweet_id=?",
                            (json.dumps(sorted(labels + [label])), tid))
        else:
            con.execute("INSERT INTO reports(tweet_id, searches) VALUES (?,?)", (tid, json.dumps([label])))
            new += 1
    con.commit()
    runlog.add("candidates", new)
    return new


def _search_all(con, column: str, since: str, verb: str) -> int:
    new = 0
    for label, query in config.SEARCHES.items():
        total = found = 0
        for page in archive.scan(column, since, select=SCAN_SELECT, fts=query):
            total += len(page)
            found += ingest(con, page, label)
        new += found
        print(f"  {verb} '{label}': {total} matches, {found} new candidates", flush=True)
    return new


def backfill(con, days: int):
    """Run every search over tweets created in the last `days` days."""
    if not db.get_state(con, "updated_cursor"):
        db.set_state(con, "updated_cursor", _iso(_now()))
    new = _search_all(con, "created_at", _iso(_now() - timedelta(days=days)), "backfill")
    print(f"backfill done: {new} new candidates from the last {days} days")


def incremental(con):
    """Run every search over tweets inserted or updated since the last run."""
    started = _now()
    cursor = db.get_state(con, "updated_cursor") or _iso(started - timedelta(hours=2))
    # Overlap the previous run a little: rows committed late can carry an earlier updated_at.
    since = _iso(datetime.fromisoformat(cursor) - timedelta(minutes=10))
    new = _search_all(con, "updated_at", since, "incremental")
    db.set_state(con, "updated_cursor", _iso(started))
    print(f"incremental: {new} new candidates since {since}")


def hydrate(con, batch: int = 200):
    """Fetch full tweet, parent, quoted tweet and top replies for pending candidates."""
    while True:
        ids = [r["tweet_id"] for r in con.execute(
            "SELECT tweet_id FROM reports WHERE hydrated=0 LIMIT ?", (batch,))]
        if not ids:
            return
        main = archive.fetch_tweets(ids)
        related = {t["reply_to_tweet_id"] for t in main.values() if t["reply_to_tweet_id"]}
        related |= {t["quoted_tweet_id"] for t in main.values() if t["quoted_tweet_id"]}
        # Stand-ins for retweeted originals may already sit in our table.
        related |= {r["quoted_tweet_id"] for r in con.execute(
            f"SELECT quoted_tweet_id FROM tweets WHERE tweet_id IN ({','.join('?' * len(ids))}) "
            "AND quoted_tweet_id IS NOT NULL", ids)}
        extra = archive.fetch_tweets(related - set(main)) if related else {}
        replies = archive.fetch_top_replies(ids, config.MAX_REPLIES)
        for t in [*main.values(), *extra.values(), *(r for rs in replies.values() for r in rs)]:
            db.upsert_tweet(con, t)
        for tid in ids:
            t = con.execute("SELECT created_at FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
            reply_ids = [r["tweet_id"] for r in replies.get(tid, [])]
            con.execute("UPDATE reports SET hydrated=1, reply_ids=?, created_at=? WHERE tweet_id=?",
                        (json.dumps(reply_ids), t["created_at"] if t else None, tid))
        con.commit()
        runlog.add("hydrated", len(ids))
        print(f"  hydrated {len(ids)} candidates", flush=True)


def refresh_quotes(con, days: int = 30):
    """Count distinct archive accounts quoting each shown opinion (author excluded).
    Recent opinions are refreshed every run; older ones once."""
    since = _iso(_now() - timedelta(days=days))
    rows = con.execute(
        "SELECT r.tweet_id, t.account_id FROM reports r JOIN tweets t USING(tweet_id) "
        "WHERE r.is_report=1 AND (r.created_at >= ? OR r.quotes_at IS NULL)", (since,)).fetchall()
    if not rows:
        return
    quoters = archive.quoting_accounts([r["tweet_id"] for r in rows])
    now = _iso(_now())
    con.executemany("UPDATE reports SET quotes=?, quotes_at=? WHERE tweet_id=?",
                    [(len(quoters.get(r["tweet_id"], set()) - {r["account_id"]}), now, r["tweet_id"]) for r in rows])
    con.commit()
    print(f"quotes: refreshed {len(rows)} opinions, {sum(1 for r in rows if quoters.get(r['tweet_id']))} quoted")
