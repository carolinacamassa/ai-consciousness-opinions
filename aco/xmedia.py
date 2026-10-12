"""Playable video URLs from X's public embed endpoint (the archive only keeps thumbnails)."""

import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx

_client = httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0 (ai-consciousness-opinions)"})
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _token(tweet_id: str) -> str:
    """The token embedded tweets send: (id / 1e15 * pi) in base 36, zeros and dot removed."""
    x = int(tweet_id) / 1e15 * math.pi
    whole, frac = int(x), x - int(x)
    s = ""
    while whole:
        s = DIGITS[whole % 36] + s
        whole //= 36
    for _ in range(12):
        frac *= 36
        s += DIGITS[int(frac)]
        frac -= int(frac)
    return s.replace("0", "")


def _pick(variants: list[dict]) -> str | None:
    """A mid-quality MP4: the lowest bitrate at or above ~600 kbps, else the best available."""
    mp4 = sorted((v for v in variants if v.get("content_type") == "video/mp4"), key=lambda v: v.get("bitrate") or 0)
    if not mp4:
        return None
    good = [v for v in mp4 if (v.get("bitrate") or 0) >= 600_000]
    return (good[0] if good else mp4[-1])["url"]


def tweet_result(tweet_id: str) -> dict:
    try:
        r = _client.get("https://cdn.syndication.twimg.com/tweet-result",
                        params={"id": tweet_id, "token": _token(tweet_id), "lang": "en"})
        return r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}


def video_urls(tweet_id: str) -> list[str | None]:
    """One entry per media item, in order; None for photos or on failure."""
    details = tweet_result(tweet_id).get("mediaDetails") or []
    return [_pick(m.get("video_info", {}).get("variants", [])) if m.get("type") != "photo" else None
            for m in details]


def attach_videos(con, tweet_ids=None):
    """Fill `video` on non-photo media items that don't have one yet."""
    rows = con.execute("SELECT tweet_id, media FROM tweets WHERE media LIKE '%\"type\": \"video\"%' "
                       "OR media LIKE '%\"type\": \"animated_gif\"%'").fetchall()
    todo = [r for r in rows if (tweet_ids is None or r["tweet_id"] in tweet_ids)
            and any(m["type"] != "photo" and "video" not in m for m in json.loads(r["media"]))]
    if not todo:
        return
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda r: video_urls(r["tweet_id"]), todo))
    found = 0
    for r, urls in zip(todo, results):
        media = json.loads(r["media"])
        videos = [u for u in urls if u] if len(urls) != len(media) else urls
        vi = iter(videos)
        for i, m in enumerate(media):
            if m["type"] == "photo":
                continue
            # Align by position when counts match; otherwise take videos in order.
            url = urls[i] if len(urls) == len(media) else next(vi, None)
            m["video"] = url or ""  # "" = looked up, none found (don't retry every hour)
            found += bool(url)
        con.execute("UPDATE tweets SET media=? WHERE tweet_id=?", (json.dumps(media), r["tweet_id"]))
    con.commit()
    print(f"videos: looked up {len(todo)} tweets, {found} playable")


def fill_authors(con):
    """Some archive accounts have a blank username/avatar; take them from X's embed data,
    falling back to the archive's mentioned_users table for the name."""
    from . import archive
    rows = con.execute("SELECT account_id, MIN(tweet_id) tweet_id FROM tweets "
                       "WHERE COALESCE(username,'')='' AND account_id IS NOT NULL GROUP BY account_id").fetchall()
    if not rows:
        return
    with ThreadPoolExecutor(4) as pool:
        users = list(pool.map(lambda r: tweet_result(r["tweet_id"]).get("user") or {}, rows))
    missing = [r["account_id"] for r, u in zip(rows, users) if not u.get("screen_name")]
    mentioned = {}
    for i in range(0, len(missing), 80):
        for m in archive.get("mentioned_users", [("select", "user_id,screen_name,name"),
                                                 ("user_id", f"in.({','.join(missing[i:i + 80])})")]):
            mentioned[m["user_id"]] = m
    fixed = 0
    for r, u in zip(rows, users):
        m = mentioned.get(r["account_id"], {})
        handle = u.get("screen_name") or m.get("screen_name")
        if not handle:
            continue
        con.execute("UPDATE tweets SET username=?, display_name=?, avatar=COALESCE(NULLIF(avatar,''), ?) "
                    "WHERE account_id=? AND COALESCE(username,'')=''",
                    (handle, u.get("name") or m.get("name") or handle, u.get("profile_image_url_https"),
                     r["account_id"]))
        fixed += 1
    con.commit()
    print(f"authors: filled {fixed}/{len(rows)} accounts with blank usernames")


def fill_avatars(con, limit=400):
    """Accounts seen only through the live stream often have no avatar in the archive."""
    rows = con.execute("SELECT account_id, MIN(tweet_id) tweet_id FROM tweets WHERE COALESCE(avatar,'')='' "
                       "AND account_id IS NOT NULL GROUP BY account_id LIMIT ?", (limit,)).fetchall()
    if not rows:
        return
    with ThreadPoolExecutor(4) as pool:
        users = list(pool.map(lambda r: tweet_result(r["tweet_id"]).get("user") or {}, rows))
    found = 0
    for r, u in zip(rows, users):
        url = u.get("profile_image_url_https") or "-"  # "-" = looked up, none found
        found += url != "-"
        con.execute("UPDATE tweets SET avatar=? WHERE account_id=? AND COALESCE(avatar,'')=''", (url, r["account_id"]))
    con.commit()
    print(f"avatars: found {found}/{len(rows)} missing avatars")
