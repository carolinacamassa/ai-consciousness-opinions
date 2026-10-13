"""The two small "signs of life" charts on the homepage, as inline SVG built with the site.

1. Tweets about the topic per complete week: one series, a line in ink.
2. Share of each stance among shown opinions per month: 100% stacked columns.

Two measures of different scale get two charts, never one chart with two axes. Every value
is also in the table under the charts; hover and keyboard focus show the same readouts."""

import json
from datetime import date, datetime, timedelta, timezone

from markupsafe import Markup, escape

from . import config, db

ON_TOPIC_KINDS = ("argument", "opinion", "question", "news", "joke")  # human tweets on the topic
INK, GRID, MUTED, SURFACE = "#1B2A4E", "#E3E8EE", "#5A667D", "#FFFFFF"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _nice(v: float) -> int:
    """Round up to a clean axis maximum (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8 or 10 times a power of ten)."""
    if v <= 0:
        return 1
    p = 10 ** (len(str(int(v))) - 1)
    return int(next(m * p for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * p >= v))


def _day(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).date()


def _label(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.day}"


def weekly(con, today: date) -> list[dict]:
    """Complete Monday-to-Sunday weeks from START_DATE: tweets about the topic in each."""
    start = date.fromisoformat(config.START_DATE)
    start -= timedelta(days=start.weekday())
    last = today - timedelta(days=today.weekday() + 7)  # Monday of the last complete week
    counts = {}
    for r in con.execute(f"SELECT created_at FROM reports WHERE unit IN {db.UNITS} AND kind IN {ON_TOPIC_KINDS}"):
        d = _day(r["created_at"])
        counts[d - timedelta(days=d.weekday())] = counts.get(d - timedelta(days=d.weekday()), 0) + 1
    out, w = [], start
    while w <= last:
        out.append({"week": w.isoformat(), "label": _label(w), "n": counts.get(w, 0)})
        w += timedelta(days=7)
    return out


def monthly_shares(con, today: date) -> list[dict]:
    """Per month from START_DATE: shown opinions per stance and their shares."""
    rows = con.execute(f"SELECT substr(created_at,1,7) m, stance, COUNT(*) n FROM reports r WHERE {db.shown()} "
                       "GROUP BY 1, 2", (config.MIN_SCORE,)).fetchall()
    by_month = {}
    for r in rows:
        by_month.setdefault(r["m"], {})[r["stance"]] = r["n"]
    out = []
    for m in sorted(by_month):
        y, mo = map(int, m.split("-"))
        counts = {s["slug"]: by_month[m].get(s["slug"], 0) for s in config.STANCES}
        total = sum(counts.values())
        partial = (y, mo) == (today.year, today.month)
        out.append({"month": m, "label": MONTHS[mo - 1], "partial": partial,
                    "long": f"{MONTHS[mo - 1]} {y}",
                    "total": total, "counts": counts,
                    "shares": {k: (100 * v / total if total else 0) for k, v in counts.items()}})
    return out


def line_svg(weeks: list[dict]) -> Markup:
    W, H, L, R, T, B = 340, 116, 30, 34, 10, 20
    pw, ph = W - L - R, H - T - B
    if len(weeks) < 2:
        return Markup("")
    top = _nice(max(w["n"] for w in weeks))
    xs = [L + i * pw / (len(weeks) - 1) for i in range(len(weeks))]
    ys = [T + ph - w["n"] / top * ph for w in weeks]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    area = f"{line} L{xs[-1]:.1f},{T + ph} L{xs[0]:.1f},{T + ph} Z"
    points = [{"x": round(x, 1), "y": round(y, 1), "v": w["n"], "t": f"week of {w['label']}"}
              for x, y, w in zip(xs, ys, weeks)]
    first, last = weeks[0], weeks[-1]
    parts = [
        f'<svg class="viz line" viewBox="0 0 {W} {H}" role="img" tabindex="0" '
        f'aria-label="Tweets per week, from {escape(first["label"])} to {escape(last["label"])}: '
        f'{last["n"]} in the last complete week. Values are in the table below." '
        f'data-points=\'{escape(json.dumps(points))}\' data-plot="{L},{T},{pw},{ph}">',
        f'<line x1="{L}" x2="{L + pw}" y1="{T}" y2="{T}" stroke="{GRID}" stroke-width="1"/>',
        f'<line x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}" stroke="{GRID}" stroke-width="1"/>',
        f'<text x="{L - 6}" y="{T + 4}" text-anchor="end" class="tick">{top}</text>',
        f'<text x="{L - 6}" y="{T + ph + 4}" text-anchor="end" class="tick">0</text>',
        f'<text x="{L}" y="{H - 4}" class="tick">{escape(first["label"])}</text>',
        f'<text x="{L + pw}" y="{H - 4}" text-anchor="end" class="tick">{escape(last["label"])}</text>',
        f'<path d="{area}" fill="{INK}" fill-opacity="0.08"/>',
        f'<path d="{line}" fill="none" stroke="{INK}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4" fill="{INK}" stroke="{SURFACE}" stroke-width="2"/>',
        f'<text x="{xs[-1] + 8:.1f}" y="{ys[-1] + 4:.1f}" class="end">{last["n"]}</text>',
        f'<line class="cross" x1="0" x2="0" y1="{T}" y2="{T + ph}" stroke="{MUTED}" stroke-width="1" visibility="hidden"/>',
        f'<circle class="hover-dot" r="4" fill="{INK}" stroke="{SURFACE}" stroke-width="2" visibility="hidden"/>',
        f'<rect class="hit" x="{L}" y="{T}" width="{pw}" height="{ph}" fill="transparent"/>',
        "</svg>",
    ]
    return Markup("".join(parts))


def stack_svg(months: list[dict]) -> Markup:
    W, H, L, R, T, B = 340, 132, 30, 96, 10, 20
    pw, ph = W - L - R, H - T - B
    if not months:
        return Markup("")
    band = pw / len(months)
    bw = min(24, band * 0.6)
    gap, radius = 2, 4
    colors = {s["slug"]: s["color"] for s in config.STANCES}
    names = {s["slug"]: s["name"] for s in config.STANCES}
    parts = [
        f'<svg class="viz stack" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Share of each stance among shown opinions by month. Values are in the table below.">',
        f'<line x1="{L}" x2="{L + pw}" y1="{T}" y2="{T}" stroke="{GRID}" stroke-width="1"/>',
        f'<line x1="{L}" x2="{L + pw}" y1="{T + ph / 2}" y2="{T + ph / 2}" stroke="{GRID}" stroke-width="1"/>',
        f'<text x="{L - 6}" y="{T + 4}" text-anchor="end" class="tick">100%</text>',
        f'<text x="{L - 6}" y="{T + ph / 2 + 4}" text-anchor="end" class="tick">50%</text>',
        f'<text x="{L - 6}" y="{T + ph + 4}" text-anchor="end" class="tick">0</text>',
    ]
    last_mids = {}
    for i, m in enumerate(months):
        x = L + i * band + (band - bw) / 2
        y0 = T + ph  # stack upward from the baseline
        order = [s["slug"] for s in config.STANCES if m["counts"][s["slug"]]]
        for j, slug in enumerate(order):
            h = m["shares"][slug] / 100 * ph
            top = y0 - h
            draw_top = top + (gap if j < len(order) - 1 else 0)  # 2px surface gap above all but the top segment
            seg_h = max(0.5, y0 - draw_top)
            tip = f'{m["long"]}: {names[slug]} {round(m["shares"][slug])}% ({m["counts"][slug]} of {m["total"]})'
            if j == len(order) - 1:  # rounded data end on the top segment only
                r = min(radius, seg_h, bw / 2)
                d = (f"M{x:.1f},{y0:.1f} L{x:.1f},{draw_top + r:.1f} Q{x:.1f},{draw_top:.1f} {x + r:.1f},{draw_top:.1f} "
                     f"L{x + bw - r:.1f},{draw_top:.1f} Q{x + bw:.1f},{draw_top:.1f} {x + bw:.1f},{draw_top + r:.1f} "
                     f"L{x + bw:.1f},{y0:.1f} Z")
                parts.append(f'<path d="{d}" fill="{colors[slug]}"/>')
            else:
                parts.append(f'<rect x="{x:.1f}" y="{draw_top:.1f}" width="{bw:.1f}" height="{seg_h:.1f}" '
                             f'fill="{colors[slug]}"/>')
            # Hit target: the whole band width, so the pointer doesn't have to land on a 24px bar.
            parts.append(f'<rect class="seg" x="{L + i * band:.1f}" y="{top:.1f}" width="{band:.1f}" height="{max(h, 1):.1f}" '
                         f'fill="transparent" tabindex="0" data-tip="{escape(tip)}"/>')
            if i == len(months) - 1:
                last_mids[slug] = (top + y0) / 2
            y0 = top
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{H - 4}" text-anchor="middle" class="tick">'
                     f'{escape(m["label"])}</text>')
    # Direct labels for the latest month, only where they don't collide (else legend and table carry it).
    mids = sorted(last_mids.items(), key=lambda kv: kv[1])
    if all(b[1] - a[1] >= 12 for a, b in zip(mids, mids[1:])):
        lx = L + (len(months) - 1) * band + (band + bw) / 2 + 8
        for slug, y in mids:
            parts.append(f'<text x="{lx:.1f}" y="{y + 4:.1f}" class="end">{round(months[-1]["shares"][slug])}% '
                         f'{escape(names[slug].lower())}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def build(con, today: date) -> dict:
    weeks, months = weekly(con, today), monthly_shares(con, today)
    return {"weeks": weeks, "months": months, "line": line_svg(weeks), "stack": stack_svg(months)}
