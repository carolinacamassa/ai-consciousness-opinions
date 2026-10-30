"""Find original tweets and quote tweets matching the searches, and hydrate them with their
quoted tweet, the author's own thread under them, top replies and media.

Replies to other people are not opinions here and never enter the pipeline. A tweet that
continues its author's own thread stands for that thread: the thread's first tweet becomes
the candidate, and the whole thread is graded as one opinion."""

import json
from datetime import datetime, timedelta, timezone

from . import archive, config, db, runlog

SCAN_SELECT = ("tweet_id,account_id,created_at,updated_at,full_text,favorite_count,retweet_count,"
               "reply_to_tweet_id")
SEARCH_HOURS = 3                # incremental searches cover this many hours of archive updates at a time
MIN_WINDOW = timedelta(minutes=10)  # smallest window a timed-out search is split down to
GRADE_COLS = ("is_report", "kind", "stance", "score", "claim", "classified_at", "arguments", "arguments_version",
              "tagged_at")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_reply(t: dict) -> bool:
    """Replies carry reply_to_tweet_id, but the archive sometimes drops it; a tweet that sits in
    someone else's conversation (conversation_id not its own id) is a reply too."""
    return bool(t.get("reply_to_tweet_id")) or bool(t.get("conversation_id") and t["conversation_id"] != t["tweet_id"])


def thread_roots(tweets: dict[str, dict]) -> dict[str, str | None]:
    """For replies (tweet id -> {account_id, reply_to_tweet_id, conversation_id?}), the first tweet
    of the author's own thread they continue, or None for a reply to someone else. When the archive
    lost the replied-to id, the conversation's first tweet being the author's own decides."""
    parents = archive.authors({t["reply_to_tweet_id"] for t in tweets.values() if t.get("reply_to_tweet_id")})
    own = {tid: t for tid, t in tweets.items() if t.get("account_id") and (
        parents.get(t["reply_to_tweet_id"]) == t["account_id"] if t.get("reply_to_tweet_id") else True)}
    missing = [tid for tid, t in own.items() if not t.get("conversation_id")]
    conv = {tid: t.get("conversation_id") for tid, t in own.items()}
    conv.update(archive.conversations(missing) if missing else {})
    starters = archive.authors({c for c in conv.values() if c})
    out = {tid: None for tid in tweets}
    for tid, t in own.items():
        root = conv.get(tid)
        if root and root != tid and starters.get(root) == t["account_id"]:
            out[tid] = root
    return out


def _add_candidate(con, tid: str, labels: list[str]) -> bool:
    old = con.execute("SELECT searches FROM reports WHERE tweet_id=?", (tid,)).fetchone()
    if old:
        merged = sorted(set(json.loads(old["searches"] or "[]")) | set(labels))
        con.execute("UPDATE reports SET searches=? WHERE tweet_id=?", (json.dumps(merged), tid))
        return False
    con.execute("INSERT INTO reports(tweet_id, searches) VALUES (?,?)", (tid, json.dumps(sorted(set(labels)))))
    return True


def ingest(con, rows: list[dict], label: str) -> int:
    """Register new candidates from a page of search results and refresh counts of
    tweets we already hold. Returns the number of new candidates."""
    # Engagement counts move after capture; keep ours current.
    con.executemany(
        "UPDATE tweets SET likes=MAX(likes,?), retweets=MAX(retweets,?) WHERE tweet_id=?",
        [(r["favorite_count"] or 0, r["retweet_count"] or 0, r["tweet_id"]) for r in rows
         if not r["full_text"].startswith("RT @")])
    # A new reply to a known opinion: re-hydrate it to pick up the reply for display.
    reply_parents = {r["reply_to_tweet_id"] for r in rows if r.get("reply_to_tweet_id")}
    if reply_parents:
        con.executemany("UPDATE reports SET hydrated=0 WHERE tweet_id=? AND hydrated=1",
                        [(p,) for p in reply_parents])

    originals, replies, retweets = [], {}, {}
    for r in rows:
        if r["created_at"] < config.START_DATE:
            continue
        if r["full_text"].startswith("RT @"):
            retweets[r["tweet_id"]] = r
        elif r.get("reply_to_tweet_id"):
            replies[r["tweet_id"]] = r
        else:
            originals.append(r["tweet_id"])

    # Retweets are not opinions, but they carry the original's current retweet
    # count; use it to refresh originals we already hold.
    if retweets:
        for rt_id, orig_id in archive.retweet_targets(retweets).items():
            if orig_id:
                con.execute("UPDATE tweets SET retweets=MAX(retweets,?) WHERE tweet_id=?",
                            (retweets[rt_id]["retweet_count"] or 0, orig_id))

    roots = thread_roots(replies) if replies else {}
    threads = sorted({root for root in roots.values() if root})
    runlog.add("scanned", len(rows))
    runlog.add("replies skipped", sum(1 for root in roots.values() if not root))
    new = sum(_add_candidate(con, tid, [label]) for tid in originals)
    for root in threads:
        if not _add_candidate(con, root, [label]):
            # The thread may have grown: fetch it again.
            con.execute("UPDATE reports SET hydrated=0 WHERE tweet_id=? AND hydrated=1", (root,))
        else:
            new += 1
    con.commit()
    runlog.add("candidates", new)
    return new


def _windows(since: str, span: timedelta) -> list[tuple[str, str]]:
    """[since, now) as consecutive (start, end) windows of `span`, oldest first."""
    start, now, out = datetime.fromisoformat(since), _now(), []
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    while start < now:
        end = min(start + span, now)
        out.append((_iso(start), _iso(end)))
        start = end
    return out


def _scan(column: str, start: str, end: str, query: str) -> list[list[dict]]:
    """Pages of one search over `column` in [start, end]. The archive cancels queries that run
    past its statement timeout, which a busy stretch (a large archive uploaded at once) causes
    even over a few hours: then the window is halved and each half searched, down to MIN_WINDOW."""
    try:
        return list(archive.scan(column, start, end, select=SCAN_SELECT, fts=query))
    except archive.StatementTimeout:
        lo, hi = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if hi - lo <= MIN_WINDOW:
            print(f"::warning::Skipped '{query}' for {column} {start} to {end}: the archive timed out "
                  f"even on {MIN_WINDOW}.", flush=True)
            runlog.add("search windows skipped")
            return []
        runlog.add("search windows split")
        mid = _iso(lo + (hi - lo) / 2)
        return _scan(column, start, mid, query) + _scan(column, mid, end, query)


def _search_window(con, column: str, start: str, end: str, totals: dict) -> int:
    """Run every search over one window; add each search's matches and new candidates to totals."""
    new = 0
    for label, query in config.SEARCHES.items():
        pages = _scan(column, start, end, query)
        found = sum(ingest(con, page, label) for page in pages)
        matches, before = totals.get(label, (0, 0))
        totals[label] = (matches + sum(map(len, pages)), before + found)
        new += found
    return new


def _report(verb: str, totals: dict):
    for label, (matches, found) in totals.items():
        print(f"  {verb} '{label}': {matches} matches, {found} new candidates", flush=True)


def backfill(con, since: str | None = None):
    """Run every search over tweets created since `since` (default config.START_DATE)."""
    since = since or config.START_DATE
    if not db.get_state(con, "updated_cursor"):
        db.set_state(con, "updated_cursor", _iso(_now()))
    # Newest first, so a long backfill shows recent opinions early.
    totals = {}
    new = sum(_search_window(con, "created_at", start, end, totals)
              for start, end in _windows(since, timedelta(days=10))[::-1])
    _report("backfill", totals)
    done = db.get_state(con, "searched_since")
    db.set_state(con, "searched_since", min(done, since) if done else since)
    print(f"backfill done: {new} new candidates since {since}")


def needs_backfill(con) -> bool:
    """True when config.START_DATE is earlier than anything searched so far."""
    done = db.get_state(con, "searched_since")
    return not done or config.START_DATE < done


def incremental(con):
    """Run every search over tweets inserted or updated since the last run, a few hours of updates
    at a time, oldest first. The cursor moves after each window, so a run that fails partway
    leaves the next one only the rest to do."""
    cursor = db.get_state(con, "updated_cursor") or _iso(_now() - timedelta(hours=2))
    # Overlap the previous run a little: rows committed late can carry an earlier updated_at.
    since = _iso(datetime.fromisoformat(cursor) - timedelta(minutes=10))
    totals, new = {}, 0
    for start, end in _windows(since, timedelta(hours=SEARCH_HOURS)):
        new += _search_window(con, "updated_at", start, end, totals)
        db.set_state(con, "updated_cursor", end)
    _report("incremental", totals)
    print(f"incremental: {new} new candidates since {since}")


def exclude_accounts(con) -> int:
    """Set aside candidates from config.EXCLUDED_ACCOUNTS (their grades, if any, stay)."""
    if not config.EXCLUDED_ACCOUNTS:
        return 0
    n = con.execute(
        f"UPDATE reports SET unit='excluded' WHERE unit IN {db.UNITS} AND tweet_id IN "
        f"(SELECT tweet_id FROM tweets WHERE lower(username) IN ({','.join('?' * len(config.EXCLUDED_ACCOUNTS))}))",
        config.EXCLUDED_ACCOUNTS).rowcount
    con.commit()
    if n:
        print(f"  excluded {n} candidates from {', '.join(config.EXCLUDED_ACCOUNTS)}")
    return n


def hydrate(con, batch: int = 200):
    """Fetch each pending candidate with its quoted tweet, the author's thread under it and top
    replies. Candidates that turn out to be replies (saved by older versions) are dropped, or
    replaced by the first tweet of the author's thread they continue."""
    while True:
        rows = {r["tweet_id"]: r for r in con.execute(
            "SELECT tweet_id, searches, thread_ids FROM reports WHERE hydrated=0 LIMIT ?", (batch,))}
        if not rows:
            exclude_accounts(con)
            return
        main = archive.fetch_tweets(rows)

        # Vanished from the archive: nothing to grade.
        for tid in set(rows) - set(main):
            con.execute("UPDATE reports SET hydrated=1, unit='missing', is_report=0 WHERE tweet_id=?", (tid,))

        replies = {tid: t for tid, t in main.items() if is_reply(t)}
        for tid, root in (thread_roots(replies) if replies else {}).items():
            con.execute("DELETE FROM reports WHERE tweet_id=?", (tid,))
            if root and not _add_candidate(con, root, json.loads(rows[tid]["searches"] or "[]")):
                con.execute("UPDATE reports SET hydrated=0 WHERE tweet_id=?", (root,))
        runlog.add("replies dropped", len(replies))

        # A thread started before START_DATE isn't an opinion from the collected period.
        early = {tid for tid, t in main.items() if not is_reply(t) and t["created_at"] < config.START_DATE}
        for tid in early:
            con.execute("UPDATE reports SET hydrated=1, unit='early', is_report=0, created_at=? WHERE tweet_id=?",
                        (main[tid]["created_at"], tid))
        tops = {tid: t for tid, t in main.items() if not is_reply(t) and tid not in early}
        if tops:
            quoted = {t["quoted_tweet_id"] for t in tops.values() if t["quoted_tweet_id"]}
            extra = archive.fetch_tweets(quoted - set(main)) if quoted else {}
            threads = archive.fetch_threads({tid: t["account_id"] for tid, t in tops.items() if t["account_id"]})
            in_thread = {x["tweet_id"] for ts in threads.values() for x in ts}
            top_replies = archive.fetch_top_replies(tops, config.MAX_REPLIES + 5)
            for t in [*tops.values(), *extra.values(), *(x for ts in threads.values() for x in ts)]:
                db.upsert_tweet(con, t)
            for tid, t in tops.items():
                thread_ids = [x["tweet_id"] for x in threads.get(tid, [])]
                reply_list = [x for x in top_replies.get(tid, []) if x["tweet_id"] not in in_thread][:config.MAX_REPLIES]
                for x in reply_list:
                    db.upsert_tweet(con, x)
                if thread_ids != json.loads(rows[tid]["thread_ids"] or "[]"):
                    # New or longer thread: the opinion has to be graded and tagged again.
                    con.execute(f"UPDATE reports SET {', '.join(f'{c}=NULL' for c in GRADE_COLS)} WHERE tweet_id=?",
                                (tid,))
                con.execute("UPDATE reports SET hydrated=1, unit=?, thread_ids=?, reply_ids=?, created_at=? "
                            "WHERE tweet_id=?",
                            ("quote" if t["quoted_tweet_id"] else "original", json.dumps(thread_ids),
                             json.dumps([x["tweet_id"] for x in reply_list]), t["created_at"], tid))
        con.commit()
        runlog.add("hydrated", len(tops))
        print(f"  hydrated {len(tops)} candidates, dropped {len(replies)} replies", flush=True)


def refresh_quotes(con, days: int = 30):
    """Count distinct archive accounts quoting each shown opinion (author excluded).
    Recent opinions are refreshed every run; older ones once."""
    since = _iso(_now() - timedelta(days=days))
    rows = con.execute(
        "SELECT r.tweet_id, t.account_id FROM reports r JOIN tweets t USING(tweet_id) "
        f"WHERE {db.shown()} AND (r.created_at >= ? OR r.quotes_at IS NULL)", (config.MIN_SCORE, since)).fetchall()
    if not rows:
        return
    quoters = archive.quoting_accounts([r["tweet_id"] for r in rows])
    now = _iso(_now())
    con.executemany("UPDATE reports SET quotes=?, quotes_at=? WHERE tweet_id=?",
                    [(len(quoters.get(r["tweet_id"], set()) - {r["account_id"]}), now, r["tweet_id"]) for r in rows])
    con.commit()
    print(f"quotes: refreshed {len(rows)} opinions, {sum(1 for r in rows if quoters.get(r['tweet_id']))} quoted")
