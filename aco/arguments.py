"""Tag shown opinions with the arguments they make, from the fixed, versioned list in arguments.json."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, llm, runlog
from .classify import context_for

TAXONOMY_PATH = Path(__file__).parent / "arguments.json"
TAXONOMY = json.loads(TAXONOMY_PATH.read_text())
VERSION = TAXONOMY["version"]
ARGUMENTS = [dict(a, family=f["name"]) for f in TAXONOMY["families"] for a in f["arguments"]]
BY_ID = {a["id"]: a for a in ARGUMENTS}
MAX_TAGS = 3

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["arguments"],
    "properties": {"arguments": {"type": "array", "maxItems": MAX_TAGS,
                                 "items": {"type": "string", "enum": list(BY_ID)}}},
}

_LISTING = "\n\n".join(
    f"## {f['name']}\n" + "\n".join(f"- {a['id']} ({a['label']}): {a['definition']} Example: \"{a['example']}\""
                                    for a in f["arguments"])
    for f in TAXONOMY["families"])
_NOT = "\n".join(f"- {x}" for x in TAXONOMY["not_arguments"])

PROMPT = """You tag tweets about AI consciousness and the moral status of AI systems with the arguments their author makes.

Return the ids of the arguments THE TWEET's author actually makes: the reasons they give for their view, at most {max_tags}, most central first. Use the tweet it replies to and the tweet it quotes only to understand what the author means. Don't tag an argument the author only mentions, quotes or attacks; if they attack one by giving a reason of their own, tag that reason if it is listed. Return [] when the author gives no listed reason.

These get no tag:
{not_arguments}

# Arguments
{listing}

# The tweet, with its context
{context}

Images attached to the tweet (if any) follow."""


def tag_one(text: str, images: list[str], model: str = None, reasoning: str | None = None) -> list[str]:
    content = [{"type": "text", "text": PROMPT.format(max_tags=MAX_TAGS, not_arguments=_NOT, listing=_LISTING,
                                                      context=text)}]
    content += [{"type": "image_url", "image_url": {"url": f"{u}?name=small"}} for u in images[:2]]
    model = model or config.TAG_MODEL
    reasoning = config.TAG_REASONING if reasoning is None else reasoning or None
    try:
        out = llm.chat_json(model, content, SCHEMA, "argument_tags", purpose="argument tags", reasoning=reasoning)
    except RuntimeError:
        if len(content) == 1:
            raise
        # An expired image URL can fail the whole call; retry text-only.
        out = llm.chat_json(model, content[:1], SCHEMA, "argument_tags", purpose="argument tags", reasoning=reasoning)
    seen = []
    for a in out["arguments"]:
        if a in BY_ID and a not in seen:
            seen.append(a)
    return seen[:MAX_TAGS]


def tag(con, limit: int | None = None, workers: int = 8):
    """Tag shown opinions that have no tags yet, or tags from an older version of the list."""
    rows = con.execute(
        f"SELECT * FROM reports WHERE {db.shown('')} AND (arguments_version IS NULL OR arguments_version!=?) "
        "ORDER BY created_at DESC" + (f" LIMIT {int(limit)}" if limit else ""),
        (config.MIN_SCORE, VERSION)).fetchall()
    if not rows:
        return
    print(f"tagging arguments (list v{VERSION}) on {len(rows)} opinions with {config.TAG_MODEL}", flush=True)
    contexts = {r["tweet_id"]: context_for(con, r, replies=False) for r in rows}
    done = failed = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(tag_one, *contexts[r["tweet_id"]]): r for r in rows if contexts[r["tweet_id"]][0]}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                tags = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad tweet must not stop the run
                failed += 1
                print(f"  ! {r['tweet_id']}: {e}", flush=True)
                continue
            con.execute("UPDATE reports SET arguments=?, arguments_version=?, tagged_at=? WHERE tweet_id=?",
                        (json.dumps(tags), VERSION, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         r["tweet_id"]))
            done += 1
            runlog.add("tagged")
            if done % 50 == 0:
                con.commit()
                print(f"  tagged {done}/{len(rows)}", flush=True)
    con.commit()
    print(f"tagged {done}, failed {failed}")
