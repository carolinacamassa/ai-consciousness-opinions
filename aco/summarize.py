"""Per-stance summaries of the case each side makes, written from the collected opinions."""

import re
from datetime import datetime, timedelta, timezone

from . import config, llm, runlog
from .classify import context_for

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["themes", "one_liner", "aggregate", "portrait"],
    "properties": {
        "themes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["theme", "reports"],
            "properties": {"theme": {"type": "string"},
                           "reports": {"type": "array", "items": {"type": "integer"}}}}},
        "one_liner": {"type": "string"},
        "aggregate": {"type": "string"},
        "portrait": {"type": "string"},
    },
}

PROMPT = """Below are {n} tweets, by {authors} different accounts, from people taking a "{name}" stance on AI consciousness and the moral status of AI systems. On this site, "{name}" means: {blurb}

Each tweet is marked [THE TWEET]. The tweet it replies to, the tweet it quotes and its top replies are shown as context: they help you understand the tweet, but they are NOT separate opinions, and they may come from people on other sides.

Write, in plain, concrete English, without hype:

1. `themes`: first, the recurring arguments or positions, each with the numbers of the tweets that make it (e.g. {{"theme": "LLMs only imitate the text of people who are conscious", "reports": [2, 7, 11]}}). Only cite a tweet for a theme if the tweet itself makes that point. A theme may have a single tweet.
2. `one_liner`: the core of this side's case, in this exact format: a two-to-five word noun phrase, then one sentence explaining it. Example: "Mimicry, not mind. Most of these tweets argue that a model trained on human text will sound conscious whether or not anything is felt."
3. `aggregate`: two short sentences, 45 words at most in total, about the 2-3 biggest themes. Every count must equal the number of tweets you listed for that theme. Numbers count tweets only, never replies or quoted tweets. With {n} tweets in total, no count can exceed {n}. When a theme rests on one tweet, say "one tweet" rather than implying a trend. Do not list examples in parentheses.
4. `portrait`: five or six sentences on this side of the debate as it shows up here: its main arguments, what or whom it is responding to, where people on this side disagree with each other, and which points are argued well versus only asserted. Be honest about thin evidence: if most themes rest on one or two tweets, say so. Describe what these accounts say; don't present it as public opinion or as the truth of the matter.

Weigh a tweet more when it gives reasons or was widely discussed (quotes by community accounts are the best signal; likes and retweets are also given).

## Tweets
{reports}"""

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _problems(out: dict, n: int) -> list[str]:
    """Counts that can't be right: more tweets than exist, or cited tweets that don't exist."""
    issues = []
    cited = {i for t in out["themes"] for i in t["reports"]}
    if any(i < 1 or i > n for i in cited):
        issues.append(f"themes cite tweet numbers outside 1..{n}")
    sizes = {len(set(t["reports"])) for t in out["themes"]}
    for num, _ in re.findall(r"\b(\d+|" + "|".join(WORDS) + r")\s+(?:\w+\s+){0,2}(tweets?|posts?|reports?|users|people|accounts)\b",
                             out["aggregate"], re.I):
        k = int(num) if num.isdigit() else WORDS[num.lower()]
        if k > n:
            issues.append(f"aggregate says {k} tweets but there are only {n}")
        elif k not in sizes and k > 1:
            issues.append(f"aggregate says {k} tweets, but no theme lists {k} tweets")
    return issues


def _reports_for(con, slug):
    """The stance's best opinions, at most SUMMARY_PER_AUTHOR per account."""
    rows = con.execute(
        "SELECT r.*, t.likes, t.retweets, t.account_id FROM reports r JOIN tweets t USING(tweet_id) "
        "WHERE r.stance=? AND r.is_report=1 AND r.score>=? "
        "ORDER BY r.score DESC, r.quotes DESC, t.retweets DESC",
        (slug, config.MIN_SCORE)).fetchall()
    per_author, out = {}, []
    for r in rows:
        k = r["account_id"] or r["tweet_id"]
        if per_author.get(k, 0) < config.SUMMARY_PER_AUTHOR:
            per_author[k] = per_author.get(k, 0) + 1
            out.append(r)
        if len(out) == config.SUMMARY_MAX_REPORTS:
            break
    return out


def count_shown(con, slug) -> int:
    return con.execute("SELECT COUNT(*) FROM reports WHERE stance=? AND is_report=1 AND score>=?",
                       (slug, config.MIN_SCORE)).fetchone()[0]


def summarize(con, force=False):
    now = datetime.now(timezone.utc)
    for s in config.STANCES:
        n = count_shown(con, s["slug"])
        if n < 5:
            continue
        old = con.execute("SELECT * FROM stance_summaries WHERE slug=?", (s["slug"],)).fetchone()
        if old and not force:
            fresh = now - datetime.fromisoformat(old["updated_at"]) < timedelta(hours=config.SUMMARY_EVERY_HOURS)
            shrunk = n < old["n_reports"]
            if not shrunk and (fresh or n - old["n_reports"] < config.SUMMARY_MIN_NEW):
                continue
        rows = _reports_for(con, s["slug"])
        blocks = []
        for i, r in enumerate(rows, 1):
            text, _ = context_for(con, r)
            blocks.append(f"### Tweet {i} ({r['created_at'][:10]}, {r['quotes']} quotes by community accounts, {r['likes']} likes, {r['retweets']} retweets)"
                          f"\nGist: {r['claim']}\n{text}")
        authors = len({r["account_id"] for r in rows})
        print(f"summarizing '{s['name']}' from {len(rows)} opinions by {authors} accounts with {config.SUMMARY_MODEL}", flush=True)
        prompt = PROMPT.format(n=len(rows), authors=authors, name=s["name"], blurb=s["blurb"], reports="\n".join(blocks))
        out = llm.chat_json(config.SUMMARY_MODEL, prompt, SCHEMA, "stance_summary", max_tokens=6000, purpose="summaries")
        issues = _problems(out, len(rows))
        if issues:
            print(f"  counts look wrong ({'; '.join(issues)}), rewriting once", flush=True)
            out = llm.chat_json(config.SUMMARY_MODEL, prompt + "\n\n## Correction\nA previous draft had these errors: "
                                + "; ".join(issues) + ". Recount from the tweet numbers.",
                                SCHEMA, "stance_summary", max_tokens=6000, purpose="summaries")
        con.execute("INSERT OR REPLACE INTO stance_summaries VALUES (?,?,?,?,?,?)",
                    (s["slug"], out["one_liner"].strip(), out["aggregate"].strip(), out["portrait"].strip(),
                     n, now.isoformat(timespec="seconds")))
        con.commit()
        runlog.add("summaries")
