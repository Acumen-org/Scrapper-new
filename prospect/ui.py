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

# Where a contact detail came from, in words.
SOURCE_LABEL = {"adv": "Form ADV", "adv_office": "Form ADV office list", "adv_social": "Form ADV",
                "brochure": "their brochure", "website": "their website",
                "vcard": "a vCard on their site", "directory": "a directory",
                "web_search": "web search", "pattern": "verified with their mail server",
                "ai": "AI reading of their site", "ai_web": "AI research", "manual": "added by hand"}


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


def fbars(components: list[dict], hi: bool = False) -> str:
    """Why a firm scores, at a glance: one segment per factor, as wide as its
    weight. The solid part is what the firm earned, a hatched segment is a
    factor Bellwether has no data for yet, an empty one is known and not met."""
    segs = []
    for c in components:
        w = float(c.get("weight") or 0)
        if w <= 0:
            continue
        known = c.get("known", True)
        pts = max(0.0, min(100.0, float(c.get("points") or 0)))
        tip = f'{c.get("label", "")}: ' + (f'{pts:.0f}/100' if known else "no data yet")
        inner = f'<u style="width:{pts:.0f}%"></u>' if known and pts else ""
        segs.append(f'<i class="{"" if known else "unk"}" style="flex:{w:g}" title="{esc(tip)}">{inner}</i>')
    return f'<div class="fbars{" hi" if hi else ""}">{"".join(segs)}</div>' if segs else ""


def why_cell(detail_json: str | None, score: float | None = None) -> str:
    """The list column that explains a score without a wall of text: the
    factor bar, then the single strongest reason in the firm's own evidence."""
    d = detail(detail_json)
    comps = d.get("components", [])
    if not comps:
        return '<span class="muted small">Evidence pending</span>'
    best = sorted((c for c in comps if c.get("contrib", 0) > 0), key=lambda c: -c["contrib"])
    top = ""
    if best:
        c = best[0]
        ev = (c.get("evidence") or c.get("level") or "").strip()
        if len(ev) > 70:
            ev = ev[:67].rstrip() + "..."
        top = f'<div class="whytop"><b>{esc(c["label"])}</b>{": " + esc(ev) if ev else ""}</div>'
    return fbars(comps, hi=bool(score is not None and score >= 60)) + top


def ring(score, coverage=None, size: int = 58) -> str:
    """A score as a ring, for cards; amber caption when data is incomplete."""
    if score is None:
        return f'<div class="ring" style="width:{size}px;height:{size}px"><b class="muted">-</b></div>'
    s = float(score)
    hi = " hi" if s >= 60 and (coverage is None or float(coverage) >= 80) else ""
    return (f'<div class="ring{hi}" style="--p:{s:.0f};width:{size}px;height:{size}px">'
            f'<b>{s:.0f}</b></div>')


def mono(name: str | None, cls: str = "") -> str:
    """Initials on a tile: firms get a rounded square, people (cls 'p') a circle."""
    words = [w for w in (name or "").replace(",", " ").split()
             if w[:1].isalnum() and w.lower() not in ("llc", "inc", "lp", "l.p.", "ltd", "the", "and", "of", "&")]
    ini = "".join(w[0] for w in words[:2]).upper() or "?"
    return f'<span class="mono-av {esc(cls)}" aria-hidden="true">{esc(ini)}</span>'


def _icon(name: str) -> str:
    from .webapp import ICONS
    return ICONS.get(name, "")


def contact_pills(email: str | None = None, phone: str | None = None,
                  phone_label: str | None = None, linkedin: str | None = None,
                  email_ok: bool = True, compact: bool = True) -> str:
    """How to reach one person, as pills. Only usable data ever reaches here:
    verified or published addresses, real phone lines, profile links."""
    out = []
    if email:
        out.append(f'<a class="pill{" ok" if email_ok else ""}" href="mailto:{esc(email)}" '
                   f'title="{esc(email)}">{_icon("mail")}{esc(email if not compact else "Email")}</a>')
    if phone:
        lbl = {"direct": "Direct", "mobile": "Mobile", "office": "Office", "main": "Main line",
               "toll_free": "Toll free"}.get(phone_label or "", "Phone")
        out.append(f'<a class="pill" href="tel:{esc(phone)}" title="{esc(lbl)}: {esc(phone)}">'
                   f'{_icon("phone")}{esc(phone if not compact else lbl)}</a>')
    if linkedin:
        out.append(f'<a class="pill li" href="{esc(linkedin)}" target="_blank" rel="noopener" '
                   f'data-noprefetch title="LinkedIn profile">{_icon("linkedin")}LinkedIn</a>')
    return f'<div class="pills">{"".join(out)}</div>' if out else ""


# ---------------------------------------------------------------- filter bar

def _qs(values: dict, drop: str | None = None, **extra) -> str:
    from urllib.parse import urlencode
    v = {k: x for k, x in values.items() if x not in (None, "", False) and k != drop}
    v.update({k: x for k, x in extra.items() if x not in (None, "")})
    return urlencode(v)


def filter_bar(action: str, values: dict, *, search: tuple[str, str] | None = ("q", "Search"),
               quick: list | None = None, more: list | None = None, hidden: dict | None = None,
               defaults: dict | None = None) -> str:
    """Search, a few filters as pills, every other filter one click away in
    More filters, and each active filter shown as a chip that removes it.

    quick and more hold fields as (name, label, options) for a select, where
    options are (value, text) pairs and the first is the 'any' choice, or
    (name, label, "check") for a checkbox. values holds the current request.
    defaults names values that count as 'not filtering' (a default sort)."""
    quick, more, hidden, defaults = quick or [], more or [], hidden or {}, defaults or {}

    def is_set(name):
        v = values.get(name)
        return v not in (None, "", False) and str(v) != str(defaults.get(name, ""))

    def select(name, label, options, pill):
        cur = values.get(name) or ""
        cls = ' class="set"' if pill and is_set(name) else ""
        opts = "".join(opt(v, cur, t) for v, t in options)
        sel = f'<select name="{esc(name)}" aria-label="{esc(label)}"{cls}>{opts}</select>'
        return sel if pill else f'<label>{esc(label)}{sel}</label>'

    def field(f, pill):
        name, label, options = f
        if options == "check":
            on = bool(values.get(name))
            cls = "inline" + (" set" if on and pill else "")
            return (f'<label class="{cls}"><input type="checkbox" name="{esc(name)}" value="1"'
                    f'{" checked" if on else ""}>{esc(label)}</label>')
        return f'<label>{select(name, label, options, pill)}</label>' if pill else select(name, label, options, pill)

    parts = []
    if search:
        parts.append(f'<input type="search" name="{esc(search[0])}" value="{esc(values.get(search[0]) or "")}"'
                     f' placeholder="{esc(search[1])}" aria-label="{esc(search[1])}">')
    parts += [field(f, True) for f in quick]
    n_more = sum(1 for f in more if is_set(f[0]))
    if more:
        panel = "".join(field(f, False) for f in more)
        parts.append(f'<details class="fmore"><summary>{_icon("filter")}More filters'
                     f'{f"<b>{n_more}</b>" if n_more else ""}</summary>'
                     f'<div class="fpanel">{panel}<div class="fact">'
                     f'<a class="btn ghost" href="{esc(action)}?{_qs(hidden)}">Reset all</a>'
                     f'<button class="primary" type="submit">Apply</button></div></div></details>')
    parts += [f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in hidden.items() if v]
    parts.append('<button class="ghost go sr-only" type="submit">Search</button>')
    form = f'<form class="fbar" method="get" action="{esc(action)}">{"".join(parts)}</form>'

    # Active filters as chips: what is narrowing the list, and one click to undo.
    labels = {}
    for name, label, options in quick + more:
        if options == "check":
            labels[name] = (label, {"1": label})
        else:
            labels[name] = (label, {str(v): t for v, t in options})
    chips = []
    if search and values.get(search[0]):
        chips.append((search[0], f'"{values[search[0]]}"'))
    for name, (label, opts) in labels.items():
        if is_set(name):
            val = str(values.get(name))
            text = opts.get(val, val)
            chips.append((name, text if options_is_self_describing(text, label) else f"{label}: {text}"))
    if not chips:
        return form
    allv = dict(values, **hidden)
    links = "".join(f'<span class="fchip">{esc(t)}<a href="{esc(action)}?{_qs(allv, drop=n)}" '
                    f'aria-label="Remove {esc(t)}">&times;</a></span>' for n, t in chips)
    return (form + f'<div class="fchips">{links}<a class="clear" href="{esc(action)}?{_qs(hidden)}">'
            f'Clear all</a></div>')


def options_is_self_describing(text: str, label: str) -> bool:
    """'Verified email' needs no 'Reach:' in front; '25' does."""
    return any(ch.isalpha() for ch in text) and len(text) > 3


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
