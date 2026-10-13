"""Render the static site from the database."""

import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from . import charts, config, db, export, status

TEMPLATES = Path(__file__).parent / "templates"
NOTABLE_N = 48
COLUMN_N = 10        # entries per stance column on the homepage
COLUMN_DAYS = 30     # columns rank the opinions of the last this-many days...
COLUMN_MIN = 5       # ...unless fewer than this many, then all of them
TOP_PEOPLE = 30      # on the homepage: 10 shown, the rest behind "Show more"
STANCE_PAGE_MAX = 1000

TOKEN_RE = re.compile(r"(https?://\S+|@\w{1,15}|#\w+)")
LEADING_MENTIONS_RE = re.compile(r"^(?:@\w{1,15}\s+)+")


def compact(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def render_text(text: str, urls: list, drop_trailing: bool) -> Markup:
    """Tweet text to safe HTML: expand t.co links, linkify mentions/hashtags,
    drop the trailing t.co link that points at attached media or a quote."""
    text = html.unescape(text or "").strip()
    expand = {u["url"]: u for u in urls}
    if drop_trailing:
        text = re.sub(r"\s*https://t\.co/\w+$", "", text)
    out = []
    for part in TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith("http"):
            u = expand.get(part)
            href, label = (u["expanded"], u["display"]) if u else (part, part.split("//", 1)[1])
            out.append(Markup('<a href="{}" rel="nofollow noopener" target="_blank">{}</a>').format(href, label))
        elif part.startswith("@"):
            out.append(Markup('<a href="https://x.com/{}" target="_blank" rel="noopener">{}</a>').format(part[1:], part))
        elif part.startswith("#"):
            out.append(Markup('<a href="https://x.com/hashtag/{}" target="_blank" rel="noopener">{}</a>').format(part[1:], part))
        else:
            out.append(escape(part))
    return Markup("").join(out)


def avatar(url: str | None, size="bigger") -> str | None:
    return url.replace("_normal.", f"_{size}.") if url and url.startswith("http") else None


class Store:
    def __init__(self, con):
        self.con = con
        self._cache = {}

    def tweet(self, tid, depth=0):
        """A tweet ready for the template, with parent/quote attached."""
        if not tid:
            return None
        key = (tid, depth)
        if key in self._cache:
            return self._cache[key]
        row = self.con.execute("SELECT * FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
        if not row:
            return None
        t = dict(row)
        t["media"] = json.loads(t["media"] or "[]")
        urls = t["urls_raw"] = json.loads(t["urls"] or "[]")
        text = t["full_text"] or ""
        if t["reply_to_tweet_id"] or t["reply_to_username"]:
            text = LEADING_MENTIONS_RE.sub("", html.unescape(text))
        t["html"] = render_text(text, urls, bool(t["media"] or t["quoted_tweet_id"]))
        t["avatar"] = avatar(t["avatar"])
        t["url"] = f"https://x.com/{t['username'] or 'i'}/status/{tid}"
        t["ts"] = t["created_at"]
        t["stamp"] = (t["created_at"] or "")[:16].replace("T", " ") + "Z"
        t["likes_c"], t["retweets_c"] = compact(t["likes"]), compact(t["retweets"])
        t["display_name"] = t["display_name"] or t["username"]
        if depth == 0:
            t["quoted"] = self.tweet(t["quoted_tweet_id"], depth + 1)
            t["parent"] = self.tweet(t["reply_to_tweet_id"], depth + 1)
        self._cache[key] = t
        return t

    def report(self, r):
        t = self.tweet(r["tweet_id"])
        if not t:
            return None
        t = dict(t)
        t["stance"] = config.STANCE_BY_SLUG.get(r["stance"])
        t["claim"] = r["claim"]
        t["score"] = r["score"]
        t["kind"] = r["kind"]
        t["quotes"] = r["quotes"] or 0
        t["rank"] = round(config.rank(t["quotes"], t["retweets"], t["score"]))
        t["replies"] = [x for x in (self.tweet(rid, 1) for rid in json.loads(r["reply_ids"] or "[]")) if x]
        t["thread"] = [x for x in (self.tweet(tid, 1) for tid in json.loads(r["thread_ids"] or "[]")) if x]
        from .arguments import BY_ID
        t["arguments"] = [BY_ID[a] for a in json.loads(r["arguments"] or "[]") if a in BY_ID]
        return t


SHOWN = db.shown()
BY_RANK = "ORDER BY aco_rank(r.quotes, t.retweets, r.score) DESC, t.likes DESC"


def build(con, out_dir: Path = config.OUT_DIR):
    con.create_function("aco_rank", 3, config.rank, deterministic=True)
    store = Store(con)
    now = datetime.now(timezone.utc)
    q = lambda sql, *a: con.execute(sql, a).fetchall()

    def reports(sql, *args):
        return [t for t in (store.report(r) for r in q(sql, *args)) if t]

    counts = {row["stance"]: row["n"] for row in q(
        f"SELECT r.stance, COUNT(*) n FROM reports r WHERE {SHOWN} GROUP BY 1", config.MIN_SCORE)}
    total_n = sum(counts.values()) or 1
    summaries = {row["slug"]: dict(row) for row in q("SELECT * FROM stance_summaries")}
    items_by_slug, stances = {}, []
    for s in config.STANCES:
        items = items_by_slug[s["slug"]] = reports(
            f"SELECT r.* FROM reports r JOIN tweets t USING(tweet_id) WHERE r.stance=? AND {SHOWN} {BY_RANK} "
            f"LIMIT {STANCE_PAGE_MAX}", s["slug"], config.MIN_SCORE)
        summary = summaries.get(s["slug"])
        one = (summary or {}).get("one_liner") or ""
        head, _, rest = one.partition(". ")
        n = counts.get(s["slug"], 0)
        stances.append(dict(s, n=n, share=round(100 * n / total_n), summary=summary, letter="ABC"[len(stances)],
                            one_head=head + "." if rest else one, one_rest=rest,
                            top=items[0] if items else None))

    # Most notable, balanced: each round takes every stance's next top-ranked
    # opinion, so the first screen shows one per stance.
    by_rank = lambda t: (-t["rank"], -(t["likes"] or 0))
    queues = [list(items_by_slug[s["slug"]][:NOTABLE_N]) for s in config.STANCES]
    notable = []
    while len(notable) < NOTABLE_N and any(queues):
        notable += sorted((queue.pop(0) for queue in queues if queue), key=by_rank)
    notable = notable[:NOTABLE_N]
    for i, t in enumerate(notable, 1):
        t["obs"] = i

    # Homepage columns: each stance's top opinions of the last COLUMN_DAYS, and its newest.
    cutoff = (now - timedelta(days=COLUMN_DAYS)).isoformat()
    for s in stances:
        items = items_by_slug[s["slug"]]
        recent = [t for t in items if t["ts"] >= cutoff]
        s["window"] = f"last {COLUMN_DAYS} days" if len(recent) >= COLUMN_MIN else "all time"
        s["top_items"] = (recent if len(recent) >= COLUMN_MIN else items)[:COLUMN_N]
        s["new_items"] = sorted(items, key=lambda t: t["ts"], reverse=True)[:COLUMN_N]

    # Everyone with a shown opinion, most opinions first.
    people = []
    for row in q(f"SELECT MAX(t.username) username, MAX(t.display_name) name, "
                 f"MAX(CASE WHEN t.avatar LIKE 'http%' THEN t.avatar END) avatar, COUNT(*) n "
                 f"FROM reports r JOIN tweets t USING(tweet_id) "
                 f"WHERE {SHOWN} AND COALESCE(t.username,'') != '' GROUP BY lower(t.username) "
                 f"ORDER BY COUNT(*) DESC, MAX(t.likes) DESC", config.MIN_SCORE):
        people.append(dict(row, avatar=avatar(row["avatar"], "200x200"), key=row["username"].lower()))
    for i, p in enumerate(people, 1):
        p["rank"], p["pct"] = f"{i:02d}", round(100 * p["n"] / people[0]["n"])
    last_entry = q(f"SELECT MAX(r.created_at) m FROM reports r WHERE {SHOWN}", config.MIN_SCORE)[0]["m"] or ""

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    env.filters["compact"] = compact
    common = dict(site_name=config.SITE_NAME, base=config.BASE, umami_script=config.UMAMI_SCRIPT,
                  umami_id=config.UMAMI_WEBSITE_ID, built_at=now.isoformat(timespec="seconds"),
                  n_reports=sum(counts.values()), n_people=len(people), github=config.GITHUB_URL,
                  upstream=config.UPSTREAM_URL, classifier=config.CLASSIFY_MODEL_NAME,
                  summarizer=config.SUMMARY_MODEL_NAME, min_score=config.MIN_SCORE, stances=stances,
                  rank_quote=config.RANK_QUOTE, rank_rt=config.RANK_RT, rank_cap=config.RANK_RT_CAP,
                  rank_score=config.RANK_SCORE, searches=config.SEARCHES, start_date=config.START_DATE,
                  last_entry=last_entry[:16].replace("T", " ") + "Z" if last_entry else "")

    tmp = out_dir.with_name(out_dir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    def page(path: str, template: str, **kw):
        d = tmp / path
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(env.get_template(template).render(**common, **kw))

    page("", "index.html", notable=notable, people=people[:TOP_PEOPLE], viz=charts.build(con, now.date()))
    for s in stances:
        page(f"stance/{s['slug']}", "stance.html", stance=s, items=items_by_slug[s["slug"]])
    for p in people:
        items = reports(f"SELECT r.* FROM reports r JOIN tweets t USING(tweet_id) "
                        f"WHERE lower(t.username)=? AND {SHOWN} {BY_RANK}", p["key"], config.MIN_SCORE)
        per_stance = {}
        for t in items:
            per_stance[t["stance"]["slug"]] = per_stance.get(t["stance"]["slug"], 0) + 1
        chips = [dict(s, count=per_stance[s["slug"]]) for s in config.STANCES if s["slug"] in per_stance]
        page(f"person/{p['key']}", "person.html", person=p, items=items, chips=chips)
    page("status", "status.html", s=status.collect(con))
    page("about", "about.html")
    shutil.copy(TEMPLATES / "favicon.svg", tmp / "favicon.svg")
    export.write(tmp, stances, items_by_slug, common["built_at"])

    # Swap the new build in place.
    old = out_dir.with_name(out_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out_dir.exists():
        out_dir.rename(old)
    tmp.rename(out_dir)
    shutil.rmtree(old, ignore_errors=True)
    print(f"built {out_dir}: {sum(counts.values())} opinions shown, {len(people)} people")
