"""Thin client for the Community Archive PostgREST API."""

import time
from collections.abc import Iterator

import httpx

from . import config

PAGE = 1000  # PostgREST row cap
SEARCH_PAGE = 500  # full-text pages of 1000 sometimes hit the statement timeout

_client = httpx.Client(
    base_url=config.CA_URL,
    headers={"apikey": config.CA_ANON_KEY, "Authorization": f"Bearer {config.CA_ANON_KEY}"},
    timeout=90,
)


class StatementTimeout(RuntimeError):
    """The archive cancelled the query for running past its statement timeout (Postgres 57014)."""


def get(table: str, params: list[tuple[str, str]], retry_timeouts=True) -> list[dict]:
    error = None
    for attempt in range(5):
        try:
            r = _client.get(table, params=params)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.json()
            error = f"HTTP {r.status_code}: {r.text[:200]}"
            if '"57014"' in r.text and not retry_timeouts:
                raise StatementTimeout(f"{table}: {error}")
        except httpx.TransportError as e:
            error = repr(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {table}: {error}")


def scan(column: str, since: str, until: str | None = None,
         select="tweet_id,created_at,updated_at,full_text", fts: str | None = None) -> Iterator[list[dict]]:
    """Yield pages of tweets with `column` in [since, until), newest first,
    optionally limited to a full-text search (websearch syntax, English stemming).

    Keyset pagination on `column`; rows sharing the boundary timestamp are
    re-fetched and de-duplicated by id. Raises StatementTimeout rather than retrying a search
    that ran too long: the same query would time out again, so the caller narrows the range."""
    cursor, seen = until, set()
    page = SEARCH_PAGE if fts else PAGE
    while True:
        params = [("select", select), (column, f"gte.{since}"),
                  ("order", f"{column}.desc,tweet_id.desc"), ("limit", str(page))]
        if fts:
            params.append(("fts", f"wfts(english).{fts}"))
        if cursor:
            params.append((column, f"lte.{cursor}"))
        rows = get("tweets", params, retry_timeouts=False)
        fresh = [r for r in rows if r["tweet_id"] not in seen]
        if not fresh:
            return
        seen.update(r["tweet_id"] for r in fresh)
        yield fresh
        if len(rows) < page:
            return
        cursor = rows[-1][column]


def _in(ids) -> str:
    return f"in.({','.join(ids)})"


def _chunks(ids, n=80):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


ENRICHED = ("tweet_id,account_id,username,account_display_name,created_at,full_text,"
            "retweet_count,favorite_count,reply_to_tweet_id,reply_to_username,"
            "quoted_tweet_id,conversation_id,avatar_media_url")


def _normalize(r: dict) -> dict:
    return dict(
        tweet_id=r["tweet_id"], account_id=r.get("account_id"), username=r.get("username"),
        display_name=r.get("account_display_name"), avatar=r.get("avatar_media_url"),
        created_at=r.get("created_at"), full_text=r.get("full_text"),
        likes=r.get("favorite_count") or 0, retweets=r.get("retweet_count") or 0,
        reply_to_tweet_id=r.get("reply_to_tweet_id"), reply_to_username=r.get("reply_to_username"),
        quoted_tweet_id=r.get("quoted_tweet_id"), conversation_id=r.get("conversation_id"),
    )


def _attach_media_urls(tweets: dict[str, dict]):
    for chunk in _chunks(tweets):
        for m in get("tweet_media", [("select", "tweet_id,media_url,media_type,width,height"),
                                     ("tweet_id", _in(chunk))]):
            tweets[m["tweet_id"]].setdefault("media", []).append(
                dict(url=m["media_url"], type=m["media_type"], w=m["width"], h=m["height"]))
        for u in get("tweet_urls", [("select", "tweet_id,url,expanded_url,display_url"),
                                    ("tweet_id", _in(chunk))]):
            tweets[u["tweet_id"]].setdefault("urls", []).append(
                dict(url=u["url"], expanded=u["expanded_url"], display=u["display_url"]))


def fetch_tweets(ids) -> dict[str, dict]:
    """Full tweets (author, counts, media, urls) keyed by id; missing ids are absent."""
    out = {}
    for chunk in _chunks(set(ids)):
        for r in get("enriched_tweets", [("select", ENRICHED), ("tweet_id", _in(chunk))]):
            out[r["tweet_id"]] = _normalize(r)
    _attach_media_urls(out)
    return out


def fetch_top_replies(ids, per_tweet: int) -> dict[str, list[dict]]:
    """Most-liked direct replies for each tweet id."""
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(set(ids), 20):
        rows = get("enriched_tweets", [("select", ENRICHED), ("reply_to_tweet_id", _in(chunk)),
                                       ("order", "favorite_count.desc"), ("limit", str(PAGE))])
        for r in rows:
            bucket = out.setdefault(r["reply_to_tweet_id"], [])
            if len(bucket) < per_tweet:
                bucket.append(_normalize(r))
    flat = {t["tweet_id"]: t for ts in out.values() for t in ts}
    _attach_media_urls(flat)
    return out


def authors(ids) -> dict[str, str]:
    """account_id of each tweet id; tweets missing from the archive are absent."""
    out = {}
    for chunk in _chunks(set(ids)):
        for r in get("tweets", [("select", "tweet_id,account_id"), ("tweet_id", _in(chunk))]):
            if r["account_id"]:
                out[r["tweet_id"]] = r["account_id"]
    return out


def conversations(ids) -> dict[str, str]:
    """conversation_id (the id of the conversation's first tweet) of each tweet id."""
    out = {}
    for chunk in _chunks(set(ids)):
        for r in get("enriched_tweets", [("select", "tweet_id,conversation_id"), ("tweet_id", _in(chunk))]):
            if r["conversation_id"]:
                out[r["tweet_id"]] = r["conversation_id"]
    return out


def fetch_threads(roots: dict[str, str]) -> dict[str, list[dict]]:
    """For each root tweet id (mapped to its author), the author's own continuation of it:
    their tweets in that conversation that reply to the root or to an earlier continuation,
    oldest first. Their replies to other people in the same conversation are left out."""
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(roots, 20):
        rows = []
        offset = 0
        while True:
            page = get("enriched_tweets", [("select", ENRICHED), ("conversation_id", _in(chunk)),
                                           ("account_id", _in({roots[c] for c in chunk})),
                                           ("order", "created_at.asc,tweet_id.asc"), ("limit", str(PAGE)),
                                           ("offset", str(offset))])
            rows += page
            if len(page) < PAGE:
                break
            offset += PAGE
        for root in chunk:
            kept, thread = {root}, []
            for r in rows:
                if (r["conversation_id"] == root and r["account_id"] == roots[root]
                        and r["tweet_id"] != root and r["reply_to_tweet_id"] in kept):
                    kept.add(r["tweet_id"])
                    thread.append(_normalize(r))
            out[root] = thread
    flat = {t["tweet_id"]: t for ts in out.values() for t in ts}
    _attach_media_urls(flat)
    return out


def retweet_targets(ids) -> dict[str, str]:
    out = {}
    for chunk in _chunks(set(ids)):
        for r in get("retweets", [("select", "tweet_id,retweeted_tweet_id"), ("tweet_id", _in(chunk))]):
            out[r["tweet_id"]] = r["retweeted_tweet_id"]
    return out



def quoting_accounts(ids) -> dict[str, set[str]]:
    """For each tweet id, the accounts in the archive that quote-tweeted it."""
    quotes: dict[str, str] = {}  # quoting tweet -> quoted tweet
    for chunk in _chunks(set(ids)):
        offset = 0
        while True:
            rows = get("quote_tweets", [("select", "tweet_id,quoted_tweet_id"), ("quoted_tweet_id", _in(chunk)),
                                        ("order", "tweet_id"), ("limit", str(PAGE)), ("offset", str(offset))])
            quotes.update((r["tweet_id"], r["quoted_tweet_id"]) for r in rows if r["tweet_id"])
            if len(rows) < PAGE:
                break
            offset += PAGE
    out: dict[str, set[str]] = {}
    for chunk in _chunks(quotes):
        for r in get("tweets", [("select", "tweet_id,account_id"), ("tweet_id", _in(chunk))]):
            if r["account_id"]:
                out.setdefault(quotes[r["tweet_id"]], set()).add(r["account_id"])
    return out
