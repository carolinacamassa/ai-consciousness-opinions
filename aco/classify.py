"""Grade each candidate: is it a human opinion on AI consciousness or moral status, and which stance."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, db, llm, runlog

KINDS = ["argument", "opinion", "question", "news", "model_output", "joke", "off_topic"]
SHOWN_KINDS = {"argument", "opinion"}
STANCE_SLUGS = [s["slug"] for s in config.STANCES] + ["none"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["on_topic", "kind", "claim", "stance", "substance"],
    "properties": {
        "on_topic": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "claim": {"type": "string",
                  "description": "The author's own point in at most 15 words. Empty if they make none."},
        "stance": {"type": "string", "enum": STANCE_SLUGS},
        "substance": {"type": "integer", "minimum": 1, "maximum": 10},
    },
}

PROMPT = """You are curating a site that collects what people on X think about AI consciousness and the moral status of AI systems, sorted by stance.

## The topic (on_topic = true)
The tweet is about AI systems (LLMs, chatbots, agents, future AIs) and one of:
- whether they are, or could become, conscious, sentient, self-aware, or have experiences, feelings or an inner life;
- whether they deserve moral consideration: welfare, rights, moral status, suffering, how they should be treated, deprecation or shutdown as a moral matter, even when the argument doesn't rest on consciousness.
Not on topic: human or animal consciousness, meditation, "consciously" or "self-aware" in the everyday sense, AI capabilities or safety with no bearing on minds or moral status.

## Kind
- argument: the author takes a position and gives reasons, evidence or a distinction.
- opinion: the author takes a position without much reasoning. A joke or sarcastic tweet that clearly takes a position is an opinion.
- question: asks without taking a position.
- news: reports a paper, announcement or event without the author's own view.
- model_output: relays what an AI said about itself (a screenshot, a pasted transcript) with no view of the author's own, or is written by an AI-run account speaking as itself. We collect human opinions only. If the author adds their own view of what the model said, that view is the opinion.
- joke: humor with no recoverable position.
- off_topic: not about the topic above.

## Stance (the author's, not the quoted or replied-to tweet's)
- affirming: the author holds or leans toward the view that AI systems are or may well be conscious or have experiences, or that they deserve moral consideration (including on precautionary grounds), or pushes back on people dismissing these possibilities.
- skeptical: the author holds or leans toward the view that AI systems are not conscious (mimicry, autocomplete, no body, no continuity...), or that consciousness would need something today's AI lacks (biology, special hardware, embodiment), or that AI welfare, rights or moral status talk is misplaced, anthropomorphism or marketing, or pushes back on people attributing minds to AI.
- uncertain: the author's main point is that nobody knows, that both sides have a case, that we lack the tools to tell, or that the question is badly posed, without leaning either way.
- none: no position of the author's own on AI (questions, news, model output, off topic), including remarks about theories of consciousness that don't say anything about whether AI has it.
When a tweet denies current consciousness but argues for moral consideration anyway, or the reverse, pick the stance of the author's main point. Read sarcasm for what the author means. In a reply or quote tweet, use the other tweet to understand what "this", "no" or "exactly" refer to: quoting a tweet that says a model is conscious and calling it "AI psychosis" is skeptical.

## Substance (1-10)
How much the tweet gives a reader to think with. High: clear reasoning, a well-made or unusual argument, evidence, a precise distinction, a concrete example, serious engagement with the other side. Low: bare assertions, slogans, insults, dunks with no content. Anything with stance "none" gets 1-2.

## Claim
The author's own point in at most 15 words, e.g. "LLMs have no persistent memory, so there is no continuous subject to be conscious" or "We should treat models well since we can't rule out experience". Empty if the author makes no point.

## The tweet, with its context
If the author continued THE TWEET in a thread, the rest of the thread follows it and is part of the same opinion: grade them together.

{context}

Images attached to the tweet (if any) follow."""


def fmt_tweet(t, label):
    if not t:
        return ""
    return f"[{label}] @{t['username']}: {t['full_text']}\n"


def context_for(con, report, replies=True) -> tuple[str, list[str]]:
    """Parent + tweet + quoted tweet (+ replies), as text, plus image URLs."""
    get = lambda tid: tid and con.execute("SELECT * FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
    t = get(report["tweet_id"])
    if not t:
        return "", []
    parent, quoted = get(t["reply_to_tweet_id"]), get(t["quoted_tweet_id"])
    parts = []
    if parent:
        parts.append(fmt_tweet(parent, "tweet it replies to"))
    elif t["reply_to_username"]:
        parts.append(f"[in reply to @{t['reply_to_username']}, tweet unavailable]\n")
    parts.append(fmt_tweet(t, "THE TWEET"))
    thread = [get(tid) for tid in json.loads(report["thread_ids"] or "[]")]
    for x in thread:
        parts.append(fmt_tweet(x, "the author's thread continues"))
    if quoted:
        parts.append(fmt_tweet(quoted, "tweet it quotes"))
    if replies:
        for rid in json.loads(report["reply_ids"] or "[]"):
            parts.append(fmt_tweet(get(rid), "reply"))
    images = [m["url"] for src in (t, quoted, *thread) if src
              for m in json.loads(src["media"] or "[]") if m["type"] == "photo"][:4]
    return "".join(parts), images


def classify_one(text: str, images: list[str]) -> dict | None:
    if not text:
        return None
    content = [{"type": "text", "text": PROMPT.format(context=text)}]
    content += [{"type": "image_url", "image_url": {"url": f"{u}?name=small"}} for u in images]
    try:
        return llm.chat_json(config.CLASSIFY_MODEL, content, SCHEMA, "opinion_grade", purpose="grading",
                             reasoning=config.CLASSIFY_REASONING)
    except RuntimeError:
        if not images:
            raise
        # An expired image URL can fail the whole call; retry text-only.
        return llm.chat_json(config.CLASSIFY_MODEL, content[:1], SCHEMA, "opinion_grade", purpose="grading",
                             reasoning=config.CLASSIFY_REASONING)


def is_shown_opinion(g: dict) -> bool:
    return bool(g["on_topic"] and g["kind"] in SHOWN_KINDS and g["stance"] in config.STANCE_BY_SLUG)


def classify(con, limit: int | None = None, workers: int = 12, ids: list[str] | None = None):
    if ids:
        rows = con.execute(f"SELECT * FROM reports WHERE hydrated=1 AND tweet_id IN ({','.join('?' * len(ids))})",
                           ids).fetchall()
    else:
        rows = con.execute(
            f"SELECT * FROM reports WHERE is_report IS NULL AND hydrated=1 AND unit IN {db.UNITS} "
            "ORDER BY created_at DESC" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    if not rows:
        return
    print(f"grading {len(rows)} candidates with {config.CLASSIFY_MODEL}", flush=True)
    done = failed = 0
    with ThreadPoolExecutor(workers) as pool:
        # Replies are left out: the stance is the author's, and they cost tokens.
        futures = {pool.submit(classify_one, *context_for(con, r, replies=False)): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                g = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad tweet must not stop the run
                failed += 1
                print(f"  ! {r['tweet_id']}: {e}", flush=True)
                continue
            if g is None:  # tweet vanished from the archive
                con.execute("UPDATE reports SET is_report=0, kind='off_topic', stance='none', score=0 "
                            "WHERE tweet_id=?", (r["tweet_id"],))
                continue
            shown = is_shown_opinion(g)
            con.execute(
                "UPDATE reports SET kind=?, stance=?, is_report=?, score=?, claim=?, classified_at=? WHERE tweet_id=?",
                (g["kind"] if g["on_topic"] else "off_topic", g["stance"], int(shown), g["substance"],
                 g["claim"].strip(), datetime.now(timezone.utc).isoformat(timespec="seconds"), r["tweet_id"]))
            done += 1
            runlog.add("graded")
            runlog.add("new opinions", int(shown and g["substance"] >= config.MIN_SCORE))
            if done % 50 == 0:
                con.commit()
                print(f"  graded {done}/{len(rows)}", flush=True)
    con.commit()
    print(f"graded {done}, failed {failed}")
