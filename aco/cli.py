"""aco backfill | update | classify | summarize | build"""

import argparse
import fcntl
import sys
from datetime import datetime, timezone

from . import collect, config, db, runlog


def main():
    p = argparse.ArgumentParser(prog="aco")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="run the searches over every tweet since a date (default: START_DATE)")
    b.add_argument("--since", help="YYYY-MM-DD; defaults to config.START_DATE")
    sub.add_parser("update", help="scheduled job: search new tweets, hydrate, grade, summarize, build")
    c = sub.add_parser("classify", help="hydrate and grade pending candidates")
    c.add_argument("--limit", type=int)
    s = sub.add_parser("summarize")
    s.add_argument("--force", action="store_true")
    sub.add_parser("build")
    sub.add_parser("quotes", help="refresh quote counts")
    sub.add_parser("videos", help="look up playable video URLs and missing avatars")
    sub.add_parser("stats")
    sub.add_parser("prune", help="delete stored tweets the site doesn't show or use as context")
    tg = sub.add_parser("tag", help="tag shown opinions with arguments from aco/arguments.json")
    tg.add_argument("--limit", type=int)
    args = p.parse_args()

    # One run at a time: a scheduled run must not overlap a long backfill.
    con = db.connect()
    lock = open(config.DB_PATH.parent / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if args.cmd not in ("build", "stats"):  # read-only commands may run alongside
            sys.exit("another aco run is in progress")

    started = datetime.now(timezone.utc)
    logged = args.cmd not in ("build", "stats", "prune")
    try:
        if args.cmd == "backfill":
            collect.backfill(con, args.since)
        elif args.cmd == "update":
            from . import arguments, build, classify, summarize, xmedia
            if collect.needs_backfill(con):  # START_DATE moved earlier, or a fresh database
                collect.backfill(con)
            collect.incremental(con)
            collect.hydrate(con)
            xmedia.attach_videos(con)
            xmedia.fill_authors(con)
            xmedia.fill_avatars(con)
            classify.classify(con, config.CLASSIFY_PER_RUN)
            arguments.tag(con, config.TAG_PER_RUN)
            collect.refresh_quotes(con)
            summarize.summarize(con)
            build.build(con)
        elif args.cmd == "classify":
            from . import classify
            collect.hydrate(con)
            classify.classify(con, args.limit)
        elif args.cmd == "summarize":
            from . import summarize
            summarize.summarize(con, args.force)
        elif args.cmd == "videos":
            from . import xmedia
            xmedia.attach_videos(con)
            xmedia.fill_authors(con)
            xmedia.fill_avatars(con, limit=5000)
        elif args.cmd == "quotes":
            collect.refresh_quotes(con)
        elif args.cmd == "build":
            from . import build
            build.build(con)
        elif args.cmd == "tag":
            from . import arguments
            arguments.tag(con, args.limit)
        elif args.cmd == "prune":
            print(f"prune: deleted {db.prune(con, config.MIN_SCORE)} tweets the site doesn't use")
        elif args.cmd == "stats":
            for row in con.execute(
                    "SELECT COUNT(*) candidates, SUM(hydrated) hydrated, SUM(is_report IS NOT NULL) graded, "
                    f"SUM(is_report) opinions, SUM({db.shown('')}) shown FROM reports", (config.MIN_SCORE,)):
                print(dict(row))
            for row in con.execute(
                    "SELECT COALESCE(kind, '(ungraded)') kind, COALESCE(stance, '-') stance, COUNT(*) n, "
                    f"SUM({db.shown('')}) shown FROM reports GROUP BY 1, 2 ORDER BY 3 DESC",
                    (config.MIN_SCORE,)):
                print(f"  {row['kind']:<13} {row['stance']:<10} {row['n']:>6} {row['shown'] or 0:>6}")

    except BaseException as e:
        if logged:
            runlog.save(con, args.cmd, started, "error", repr(e))
        raise
    if logged:
        runlog.save(con, args.cmd, started, "ok")


if __name__ == "__main__":
    main()
