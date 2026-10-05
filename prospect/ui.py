"""Pieces every screen shares: how reachable a firm is, the one-line reason a
firm scored where it did, filter controls, and small inline charts. Kept out
of webapp.py so the views can import them without pulling in each other."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from . import products
from .webapp import esc, money, nice_name

STATUS_OPTIONS = ["new", "working", "meeting set", "qualified", "disqualified",
                  "customer"]


def contact_flags(c, crds: list[str]) -> dict[str, dict]:
    """How reachable each firm is, in one pass over the unified contacts:
    named people with an address the firm published or a server confirmed,
    verified addresses, shared inboxes, pattern guesses, direct lines, and
    how many people we know of."""
    out = {k: {"personal": 0, "verified": 0, "inbox": 0, "guess": 0, "direct": 0,
               "phone": False, "people": 0} for k in crds}
    if not crds:
        return out
    ph = ",".join("?" * len(crds))
    a = tuple(crds)

    def each(sql, fn):
        try:
            for r in c.execute(sql, a):
                if r["crd"] in out:
                    fn(out[r["crd"]], r)
        except Exception:
            c.rollback()

    def put(d, r):
        d.update(personal=r["personal"] or 0, verified=r["verified"] or 0,
                 inbox=r["inbox"] or 0, guess=r["guess"] or 0, direct=r["direct"] or 0,
                 phone=bool(r["phones"]))

    each(f"""SELECT crd,
          COUNT(DISTINCT person_key) FILTER (WHERE kind='email' AND person_key != ''
                AND is_role=0 AND source != 'pattern'
                AND verify_status NOT IN ('invalid','no_mail_server')) AS personal,
          COUNT(*) FILTER (WHERE kind='email' AND verify_status='valid') AS verified,
          COUNT(*) FILTER (WHERE kind='email' AND is_role=1) AS inbox,
          COUNT(*) FILTER (WHERE kind='email' AND source='pattern'
                AND verify_status NOT IN ('valid','invalid','no_mail_server')) AS guess,
          COUNT(*) FILTER (WHERE kind='phone' AND person_key != '') AS direct,
          COUNT(*) FILTER (WHERE kind='phone') AS phones
        FROM usable_contact_point WHERE crd IN ({ph}) GROUP BY crd""", put)
    each(f"SELECT crd, headcount FROM firm_people_stats WHERE crd IN ({ph})",
         lambda d, r: d.__setitem__("people", r["headcount"] or 0))
    missing = [k for k, v in out.items() if not v["people"]]
    if missing:
        ph2 = ",".join("?" * len(missing))
        try:
            for r in c.execute(f"SELECT crd, COUNT(*) n FROM schedule_a WHERE is_individual=1"
                               f" AND crd IN ({ph2}) GROUP BY crd", tuple(missing)):
                out[r["crd"]]["people"] = r["n"]
        except Exception:
            c.rollback()
    return out


def contact_cell(f: dict) -> str:
    bits = []
    if f["verified"]:
        bits.append(f'<span class="chip lead" title="Addresses a mail server confirmed">'
                    f'{f["verified"]} verified</span>')
    if f["personal"]:
        bits.append(f'<span class="chip line" title="Named people with an address the firm '
                    f'published">{f["personal"]} people</span>')
    elif f["guess"]:
        bits.append(f'<span class="chip" title="Addresses built from the firm\'s pattern, '
                    f'not yet confirmed">{f["guess"]} guessed</span>')
    elif f["inbox"]:
        bits.append('<span class="chip" title="Only shared inboxes such as info@">inbox only'
                    '</span>')
    if f["direct"]:
        bits.append(f'<span class="meta" style="display:block">{f["direct"]} direct line'
                    f'{"s" if f["direct"] > 1 else ""}</span>')
    elif f["phone"]:
        bits.append('<span class="meta" style="display:block">main phone</span>')
    return " ".join(bits) or '<span class="muted small">none yet</span>'


def detail(detail_json: str | None) -> dict:
    try:
        return json.loads(detail_json or "{}") or {}
    except ValueError:
        return {}


def why_line(detail_json: str | None, n: int = 2) -> str:
    """The criteria that earned the most points, in the firm's own evidence."""
    d = detail(detail_json)
    comps = sorted(d.get("components", []), key=lambda c: -c["contrib"])[:n]
    parts = []
    for c in comps:
        if c["contrib"] <= 0:
            continue
        ev = c["evidence"] or c["level"]
        if len(ev) > 90:
            ev = ev[:87].rstrip() + "..."
        parts.append(f'<div class="evidence-item"><strong>{esc(c["label"])}</strong><p>{esc(ev)}</p></div>')
    if not parts:
        return '<span class="muted">Evidence pending</span>'
    labels = [esc(c['label']) for c in comps if c['contrib'] > 0]
    return f'<details class="evidence"><summary>{labels[0]}<span>{" + " + str(len(labels)-1) + " factors" if len(labels)>1 else "View evidence"}</span></summary><div>{"".join(parts)}</div></details>'


def pitch_of(detail_json: str | None) -> str:
    return detail(detail_json).get("pitch") or ""


def opt(value, current, label) -> str:
    sel = " selected" if str(value) == str(current) else ""
    return f'<option value="{esc(value)}"{sel}>{esc(label)}</option>'


def firm_meta(r) -> str:
    bits = [f"CRD {esc(r['crd'])}"]
    if r.get("city") or r.get("state"):
        bits.append(esc(" ".join(x for x in (nice_name(r.get("city")), r.get("state")) if x)))
    bits.append(f"{money(r.get('raum'))} AUM")
    return " &middot; ".join(bits)


def product_name(key: str | None) -> str:
    try:
        return products.product(key)["name"] if key else ""
    except KeyError:
        return key or ""


def ago(iso: str | None) -> str:
    """'3 days ago' from an ISO date or timestamp; blank when unknown."""
    if not iso:
        return ""
    try:
        if len(iso) <= 10:
            d = (date.today() - date.fromisoformat(iso[:10])).days
            if d <= 0:
                return "today"
            if d == 1:
                return "yesterday"
            if d < 45:
                return f"{d} days ago"
            if d < 540:
                m = d // 30
                return f"{m} month{'s' if m != 1 else ''} ago"
            y = d // 365
            return f"{y} year{'s' if y != 1 else ''} ago"
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        s = (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return iso[:10]
    if s < 0:
        s = -s
        if s < 3600:
            return f"in {max(1, int(s // 60))} min"
        if s < 86400:
            return f"in {int(s // 3600)} h"
        return f"in {int(s // 86400)} days"
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def spark(values: list[float], w: int = 120, h: int = 28, colour: str = "var(--ok)") -> str:
    """A tiny trend line, for a number that has a history."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.6"'
            f' stroke-linejoin="round" stroke-linecap="round"/></svg>')


def histogram(values: list[float], bins: int = 20, w: int = 160, h: int = 34) -> str:
    """Score distribution as small bars, 0 to 100."""
    counts = [0] * bins
    for v in values:
        if v is None:
            continue
        i = min(bins - 1, max(0, int(float(v) / 100 * bins)))
        counts[i] += 1
    top = max(counts) or 1
    bw = w / bins
    bars = []
    for i, n in enumerate(counts):
        bh = max(1.0, n / top * (h - 2)) if n else 0
        col = "var(--ok)" if i >= bins * 0.6 else "var(--soft)"
        op = "0.9" if i >= bins * 0.6 else "0.45"
        if bh:
            bars.append(f'<rect x="{i * bw + 0.5:.1f}" y="{h - bh:.1f}" width="{bw - 1.5:.1f}" '
                        f'height="{bh:.1f}" rx="1" fill="{col}" opacity="{op}"/>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'aria-label="Score distribution">{"".join(bars)}</svg>')


def year_bars(series: list[dict], w: int = 640, h: int = 140) -> str:
    """Headcount by year as bars, with joins above and departures below the
    axis, for the hiring section of a firm page."""
    if not series:
        return ""
    n = len(series)
    top = max(max((s.get("joined") or 0) for s in series),
              max((s.get("left") or 0) for s in series), 1)
    mid = h * 0.55
    bw = (w - 40) / n
    out = [f'<line class="grid" x1="30" y1="{mid:.1f}" x2="{w}" y2="{mid:.1f}" stroke-width="1"/>']
    for i, s in enumerate(series):
        x = 34 + i * bw
        j = (s.get("joined") or 0) / top * (mid - 14)
        l_ = (s.get("left") or 0) / top * (h - mid - 20)
        if j:
            out.append(f'<rect x="{x:.1f}" y="{mid - j:.1f}" width="{bw * 0.62:.1f}" height="{j:.1f}"'
                       f' rx="2" fill="var(--ok)" opacity=".85"><title>{s["year"]}: '
                       f'{s.get("joined")} joined</title></rect>')
        if l_:
            out.append(f'<rect x="{x:.1f}" y="{mid:.1f}" width="{bw * 0.62:.1f}" height="{l_:.1f}"'
                       f' rx="2" fill="var(--red-hi)" opacity=".75"><title>{s["year"]}: '
                       f'{s.get("left")} left</title></rect>')
        out.append(f'<text x="{x + bw * 0.31:.1f}" y="{h - 3}" text-anchor="middle">'
                   f'{str(s["year"])[-2:] if n > 8 else s["year"]}</text>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" style="width:100%;height:{h}px">'
            f'{"".join(out)}<text x="0" y="12">joined</text>'
            f'<text x="0" y="{h - 16}">left</text></svg>')
