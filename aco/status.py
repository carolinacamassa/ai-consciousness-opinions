"""Data for the status page: pipeline health and LLM cost per day."""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db


def collect(con) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).date().isoformat()
    q = lambda sql, *a: con.execute(sql, a).fetchall()

    days = defaultdict(lambda: {"cost": defaultdict(float), "runs": 0, "errors": 0, "counters": defaultdict(int)})
    for r in q("SELECT substr(ts,1,10) day, purpose, SUM(cost) c FROM llm_calls WHERE ts >= ? GROUP BY 1, 2", since):
        days[r["day"]]["cost"][r["purpose"]] += r["c"]
    for r in q("SELECT substr(started_at,1,10) day, status, counters FROM runs WHERE started_at >= ?", since):
        d = days[r["day"]]
        d["runs"] += 1
        d["errors"] += r["status"] != "ok"
        for k, v in json.loads(r["counters"] or "{}").items():
            d["counters"][k] += v
    purposes = sorted({p for d in days.values() for p in d["cost"]})
    table = []
    for day in sorted(days, reverse=True):
        d = days[day]
        table.append(dict(day=day, total=sum(d["cost"].values()), cost=dict(d["cost"]), runs=d["runs"],
                          errors=d["errors"], counters=dict(d["counters"])))
    peak = max((t["total"] for t in table), default=0) or 1

    week = [t["total"] for t in table if t["day"] >= (now - timedelta(days=7)).date().isoformat()]
    runs = [dict(r, counters=json.loads(r["counters"] or "{}"))
            for r in q("SELECT * FROM runs ORDER BY started_at DESC LIMIT 30")]
    last_ok = next((r for r in runs if r["status"] == "ok" and r["command"] == "update"), None)
    tracking_since = q("SELECT MIN(ts) t FROM llm_calls")[0]["t"]

    pipeline = q(f"""SELECT COUNT(*) candidates, SUM(hydrated=0) unhydrated,
                     SUM(is_report IS NULL AND unit IN {db.UNITS}) ungraded,
                     SUM({db.shown('')}) shown FROM reports""", config.MIN_SCORE)[0]
    return dict(
        now=now.isoformat(timespec="seconds"), table=table, peak=peak, purposes=purposes, runs=runs,
        last_ok=last_ok, tracking_since=tracking_since,
        avg_week=sum(week) / 7 if week else 0, month=sum(t["total"] for t in table),
        pipeline=dict(pipeline), cursor=db.get_state(con, "updated_cursor"),
        summaries=q("SELECT slug, updated_at, n_reports FROM stance_summaries ORDER BY updated_at DESC"),
    )
