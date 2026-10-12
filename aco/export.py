"""Plain-text exports for LLMs: /llms.txt, /llms-full.txt and /data/<stance>.md."""

import html
from pathlib import Path

from . import config

INTRO = """# {site_name}

> What people on X say about AI consciousness and the moral status of AI systems, sorted by stance. Built from
> the Community Archive (https://www.community-archive.org), an opt-in archive of X, so this reflects the accounts in
> that archive, not public opinion. Tweets matching a set of full-text searches are graded by {classifier}: is it a
> human opinion on the topic, which stance (affirming, skeptical, uncertain), and how much substance it has (1-10).
> Opinions scoring {min_score}+ are kept. Per-stance summaries are written by {summarizer}. This file was built {built_at}.

Ranking: rank = {rq} * quotes + {rr} * log2(1 + min(retweets, {cap})) + {rs} * substance,
where `quotes` counts distinct Community Archive accounts that quote-tweeted the opinion.
"""


def _url(path: str) -> str:
    return f"{config.SITE_URL.rstrip('/')}{config.BASE}{path}"


def _text(t) -> str:
    text = html.unescape(t["full_text"] or "").strip()
    for u in t.get("urls_raw", []):
        text = text.replace(u["url"], u["expanded"])
    return text


def _quote_block(label: str, t) -> str:
    body = _text(t).replace("\n", "\n> ")
    return f"> [{label} @{t['username']}] {body}\n"


def _media_line(t) -> str:
    items = []
    for m in t.get("media") or []:
        items.append(f"video {m['video']}" if m.get("video") else f"{m['type']} {m['url']}")
    return f"Media: {'; '.join(items)}\n" if items else ""


def report_md(i: int, t) -> str:
    out = [f"### Opinion {i}: {t['claim'] or '(no gist)'}\n",
           f"- URL: {t['url']}\n",
           f"- Author: @{t['username']} ({t['display_name']}), {t['created_at'][:10]}\n",
           f"- Stance: {t['stance']['name']}\n",
           f"- Substance: {t['score']}/10 ({t['kind']}) · quotes by archive accounts: {t['quotes']} · "
           f"retweets: {t['retweets']} · likes: {t['likes']} · rank: {t['rank']}\n\n"]
    if t.get("parent"):
        out.append(_quote_block("in reply to", t["parent"]))
        out.append("\n")
    out.append(f"@{t['username']}: {_text(t)}\n")
    out.append(_media_line(t))
    if t.get("quoted"):
        out.append("\n" + _quote_block("quoting", t["quoted"]))
    if t.get("replies"):
        out.append("\nTop replies:\n")
        out += [f"- @{r['username']}: {_text(r).replace(chr(10), ' ')}\n" for r in t["replies"]]
    return "".join(out) + "\n"


def stance_md(s, items) -> str:
    summary = s.get("summary") or {}
    source = _url(f"stance/{s['slug']}/")
    head = [f"# {s['name']}: opinions on AI consciousness and moral status\n\n",
            f"{s['blurb']}\n\n{s['n']} opinions. Source: {source}\n\n"]
    if summary:
        head += ["## Summary\n\n", f"**{summary['one_liner']}**\n\n", f"{summary['aggregate']}\n\n",
                 f"{summary['portrait']}\n\n", f"(Summary written {summary['updated_at'][:10]}.)\n\n"]
    head.append("## Opinions (highest rank first)\n\n")
    return "".join(head) + "".join(report_md(i, t) for i, t in enumerate(items, 1))


def write(out: Path, stances, items_by_slug, built_at: str):
    intro = INTRO.format(site_name=config.SITE_NAME, classifier=config.CLASSIFY_MODEL, summarizer=config.SUMMARY_MODEL,
                         min_score=config.MIN_SCORE, built_at=built_at, rq=config.RANK_QUOTE,
                         rr=config.RANK_RT, cap=config.RANK_RT_CAP, rs=config.RANK_SCORE)
    index = [intro, "\n## Stances\n\n"]
    for s in stances:
        summary = s.get("summary") or {}
        link = _url(f"data/{s['slug']}.md")
        index.append(f"### [{s['name']}]({link}), {s['n']} opinions\n\n{s['blurb']}\n\n")
        if summary:
            index.append(f"{summary['one_liner']} {summary['aggregate']}\n\n")
    index.append(f"\n## Everything in one file\n\n- [llms-full.txt]({_url('llms-full.txt')}): "
                 "all summaries and all opinions with their context.\n")
    (out / "llms.txt").write_text("".join(index))

    data = out / "data"
    data.mkdir()
    full = [intro, "\n"]
    for s in stances:
        md = stance_md(s, items_by_slug[s["slug"]])
        (data / f"{s['slug']}.md").write_text(md)
        full.append(md + "\n---\n\n")
    (out / "llms-full.txt").write_text("".join(full))
