"""The firm page's Growth tab: is the firm growing, and where will it need people?

Everything the ATS list scores, laid out to be read (prospect/growth.py builds
the figures every day; prospect/openroles.py finds the openings):

  At a glance   the team, assets, clients and open roles in one row
  Team          registered people now and a year ago, who joined and who
                left, from where and to where, year by year, and teams hired
                from one firm
  Business      assets, clients, total staff and advisory staff at the latest
                filing against the filing about a year before it, with both
                dates, and every filing on record with the same figures
  Hiring needs  by function: advisors, operations and support staff against
                similar firms, departures to backfill, succession
  Expansion     new offices, new states, a recent registration, launches and
                custodian changes
  Open roles    what the firm is hiring for now, from its careers page, its
                tracking system and the job boards

Every comparison names the filings it compares. A change over more than
fifteen months is also given as a yearly rate, so growth over three years is
never read as one year's. Missing data is said to be missing, with what will
fill it.
"""

from __future__ import annotations

from datetime import date

from . import ui
from .growth import ANNUALISE_AFTER_DAYS, VETERAN_YEARS
from .webapp import esc, escn, money, nice_name

SOURCE_WORDS = {"website": "Careers page", "ats": "Tracking system", "job_board": "Job board"}
FUNCTION_ORDER = ("Advisory", "Operations and client service", "Compliance", "Finance and tax",
                  "Marketing and growth", "Technology", "People and HR", "Leadership", "Other")


# ------------------------------------------------------------- formatting

def _pct(v: float, signed: bool = True) -> str:
    return f"{'+' if signed and v > 0 else ''}{v * 100:.0f}%"


def _tone(v: float | None) -> str:
    if v is None:
        return ""
    return "ok" if v > 0.005 else "bad" if v < -0.005 else ""


def _num(v, kind: str = "int") -> str:
    if v is None:
        return "-"
    if kind == "money":
        return money(v)
    if kind == "dec":
        return f"{float(v):,.1f}"
    return f"{float(v):,.0f}"


def _when(iso: str | None) -> str:
    try:
        return date.fromisoformat((iso or "")[:10]).strftime("%b %Y")
    except ValueError:
        return iso or "?"


def _months(days: int | None) -> str:
    if not days:
        return ""
    if days > ANNUALISE_AFTER_DAYS:
        return f"{days / 365:.1f} years"
    m = max(1, round(days / 30.4))
    return f"{m} month{'s' if m != 1 else ''}"


def _change(ch: dict | None, days: int | None) -> str:
    """The change between two filings, with its yearly rate when the filings
    are more than fifteen months apart."""
    if not ch:
        return '<span class="muted">-</span>'
    out = f'<span class="{_tone(ch["change"])}">{_pct(ch["change"])}</span>'
    if (days or 0) > ANNUALISE_AFTER_DAYS:
        out += f'<div class="meta">{_pct(ch["annual"])} a year</div>'
    return out


def _firm_link(name: str | None, pk) -> str:
    if not name:
        return ""
    if pk and str(pk).isdigit():
        return f'<a href="/firm/{esc(pk)}">{escn(name)}</a>'
    return escn(name)


def _fact(label: str, value: str, sub: str = "") -> str:
    return (f'<div><div class="l">{label}</div><div class="v">{value}'
            f'{(" <small>" + sub + "</small>") if sub else ""}</div></div>')


def _missing(text: str) -> str:
    return f'<p class="muted small">{text}</p>'


def _team_change(t: dict) -> tuple[str, str]:
    now, prev = t.get("now"), t.get("prev")
    if now is None or prev is None:
        return "-", ""
    net = now - prev
    if net > 0:
        word = f'<span class="ok">Grew by {net}</span>'
    elif net < 0:
        word = f'<span class="bad">Shrank by {-net}</span>'
    else:
        word = "No change"
    sub = f"{_pct(net / prev)} on {prev:,} a year ago" if prev else ""
    return word, sub


# ----------------------------------------------------------------- glance

def glance(d: dict, stats: dict | None) -> str:
    """The overview's Growth card: the team, assets and open roles."""
    g = d.get("growth") or {}
    gd = g.get("detail") or {}
    t = gd.get("team") or {}
    if not t and stats:
        j, left = int(stats.get("hires_12m") or 0), int(stats.get("departures_12m") or 0)
        t = {"now": stats.get("headcount"), "prev": (stats.get("headcount") or 0) - (j - left)}
    word, sub = _team_change(t)
    cells = [_fact("Team in the past year", word, sub)]
    aum = gd.get("aum")
    period = gd.get("period") or {}
    if aum:
        rate = aum["annual"] if (period.get("days") or 0) > ANNUALISE_AFTER_DAYS else aum["change"]
        unit = " a year" if (period.get("days") or 0) > ANNUALISE_AFTER_DAYS else ""
        cells.append(_fact("Assets", f'<span class="{_tone(rate)}">{_pct(rate)}{unit}</span>',
                           f"since the {_when(period.get('from'))} filing"))
    else:
        cells.append(_fact("Assets", "-", "no earlier filing to compare"))
    posts = d.get("postings") or []
    cr = d.get("careers") or {}
    if posts:
        cells.append(_fact("Open roles", f'<span class="ok">{len(posts)} open</span>',
                           esc(_top_functions(posts))))
    elif cr:
        cells.append(_fact("Open roles", "None listed", f"checked {esc(ui.ago(cr.get('checked_at')))}"))
    else:
        cells.append(_fact("Open roles", "-", "not checked yet"))
    return f'<div class="facts">{"".join(cells)}</div>'


def _top_functions(posts: list[dict]) -> str:
    by = _by_function(posts)
    return ", ".join(f"{k} {len(v)}" for k, v in list(by.items())[:2])


def _by_function(posts: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for x in posts:
        by.setdefault(x.get("function") or "Other", []).append(x)
    order = {k: i for i, k in enumerate(FUNCTION_ORDER)}
    return dict(sorted(by.items(), key=lambda kv: (-len(kv[1]), order.get(kv[0], 99))))


# ---------------------------------------------------------------- section

def section(crd: str, f, d: dict, stats: dict | None, mv: dict, series: list, history: list,
            ats=None) -> str:
    """The whole Growth tab."""
    g = d.get("growth") or {}
    gd = g.get("detail") or {}
    parts = [_summary(crd, d, gd, stats, ats)]
    if not g:
        parts.append('<div class="note plain small" style="margin-bottom:24px">The growth figures '
                     'for this firm are being built: the Growth job measures every firm once a day. '
                     'Team moves and open roles below are already current.</div>')
    parts.append(_block("hiring", "Team", "From the SEC record of every registered person",
                        _team(gd, stats, mv, series)))
    parts.append(_block("growth-business", "Assets, clients and staff",
                        "Form ADV: assets (Item 5.F), clients (5.D), total staff (5.A), "
                        "advisory staff (5.B)", _business(f, gd, history)))
    parts.append(_block("growth-needs", "Where they will need people",
                        "Hiring needs by function", _needs(gd, d)))
    parts.append(_block("growth-expansion", "Expansion",
                        "New offices, states, registrations and launches", _expansion(gd)))
    parts.append(_block("growth-roles", "Open roles",
                        "Careers page, tracking system and job boards", _roles(crd, d)))
    return "".join(parts)


def _block(anchor: str, title: str, sub: str, inner: str) -> str:
    return (f'<div class="card dsec growth-block" id="{anchor}"><div class="card-head"><h2>{title}</h2>'
            f'<span class="sub">{sub}</span></div>{inner}</div>')


def _summary(crd: str, d: dict, gd: dict, stats, ats) -> str:
    t = gd.get("team") or {}
    if not t and stats:
        j, left = int(stats.get("hires_12m") or 0), int(stats.get("departures_12m") or 0)
        t = {"now": stats.get("headcount"), "prev": (stats.get("headcount") or 0) - (j - left),
             "joined": j, "left": left}
    word, _ = _team_change(t)
    team_sub = (f"{t.get('joined') or 0} joined, {t.get('left') or 0} left"
                if t.get("now") is not None else "roster not loaded yet")
    period = gd.get("period") or {}
    long_gap = (period.get("days") or 0) > ANNUALISE_AFTER_DAYS

    def rate(key: str, label: str) -> str:
        ch = gd.get(key)
        if not ch:
            return _fact(label, "-", "no earlier filing to compare")
        v = ch["annual"] if long_gap else ch["change"]
        return _fact(label, f'<span class="{_tone(v)}">{_pct(v)}{" a year" if long_gap else ""}</span>',
                     f"since {_when(period.get('from'))}")

    posts = d.get("postings") or []
    cr = d.get("careers") or {}
    roles = (_fact("Open roles", f'<span class="ok">{len(posts)}</span>', esc(_top_functions(posts)))
             if posts else _fact("Open roles", "None found" if cr else "-",
                                 "" if cr else "not checked yet"))
    senior = (d.get("growth") or {}).get("departures_senior")
    cells = [_fact("Registered team", f"{t['now']:,}" if t.get("now") is not None else "-",
                   f"{word} in a year" if t.get("prev") is not None else team_sub),
             _fact("Joined and left", team_sub if t.get("now") is not None else "-"),
             rate("aum", "Assets"), rate("clients", "Clients"), roles,
             _fact("Senior departures", str(senior) if senior is not None else "-",
                   "in the past year")]
    score = ""
    if ats is not None and getattr(ats, "status", "") == "scored":
        score = (f'<a class="chip" href="#fit-ats" style="margin-left:auto">ATS score '
                 f'{ats.score:.0f}/100 &middot; {ats.coverage:.0f}% known data</a>')
    pitch = (f'<span class="small soft">{esc(ats.pitch)}</span>'
             if ats is not None and getattr(ats, "pitch", "") else "")
    head = (f'<div class="row growth-pitch" style="margin-bottom:12px;gap:12px;align-items:center">'
            f'{pitch}{score}</div>' if (score or pitch) else "")
    return f'<div class="dsec">{head}<div class="facts">{"".join(cells)}</div></div>'


# ------------------------------------------------------------------- team

def _team(gd: dict, stats, mv: dict, series: list) -> str:
    if not stats and not mv.get("joined") and not mv.get("left"):
        return _missing("Joins and departures come from the SEC individual feed and appear "
                        "once it has loaded.")
    out = ""
    if stats:
        t = gd.get("team") or {}
        j, left = int(stats.get("hires_12m") or 0), int(stats.get("departures_12m") or 0)
        head = stats.get("headcount")
        prev = t.get("prev") if t.get("prev") is not None else (head - (j - left) if head is not None else None)
        word, sub = _team_change({"now": head, "prev": prev})
        out += ('<div class="facts" style="margin-bottom:14px">'
                + _fact("Registered now", f"{head:,}" if head is not None else "-",
                        f"{prev:,} a year ago" if prev is not None else "")
                + _fact("Joined in the past year", f'<span class="{"ok" if j else ""}">{j}</span>')
                + _fact("Left in the past year", f'<span class="{"bad" if left else ""}">{left}</span>')
                + _fact("Team change", word, sub)
                + _fact("The year before", f"{int(stats.get('hires_prev_12m') or 0)} joined, "
                                           f"{int(stats.get('departures_prev_12m') or 0)} left")
                + '</div>')
    if series:
        out += f'<div style="margin:14px 0 6px">{ui.year_bars(series)}</div>'
    lifts = gd.get("lift_outs") or []
    if lifts:
        out += ('<div class="note plain small" style="margin:12px 0">'
                + "; ".join(f'<b>{int(x["n"])} people joined from {_firm_link(x.get("org"), x.get("org_crd"))}</b>'
                            for x in sorted(lifts, key=lambda x: -(x.get("n") or 0))[:3])
                + ' in the past year: a team hired together.</div>')

    def lst(items, kind):
        if not items:
            return '<p class="muted small">None in the last two years.</p>'
        word = "from" if kind == "joined" else "now at"
        rows = []
        for m in items[:12]:
            other = _firm_link(m.get("other_org_name"), m.get("other_org_pk"))
            rows.append(
                f'<div class="it"><div class="ic {"in" if kind == "joined" else "out"}">'
                f'{"+" if kind == "joined" else "-"}</div><div><b>{escn(m["name"])}</b>'
                + (f' <span class="muted">{word} {other}</span>' if other else "")
                + f'</div><div class="tiny muted nowrap">{esc((m.get("date") or "")[:10])}</div></div>')
        more = (f'<p class="meta">and {len(items) - 12} more</p>' if len(items) > 12 else "")
        return "".join(rows) + more
    out += (f'<div class="cols-2" style="margin-top:14px"><div><h3>Joined</h3><div class="feed">'
            f'{lst(mv.get("joined"), "joined")}</div></div><div><h3>Left</h3><div class="feed">'
            f'{lst(mv.get("left"), "left")}</div></div></div>')
    if stats:
        srcs, dests = stats.get("top_sources") or [], stats.get("top_destinations") or []

        def fl(items):
            return ", ".join(f'{_firm_link(x.get("org_name"), x.get("org_pk"))} ({x.get("n")})'
                             for x in items[:5] if x.get("org_name")) or "-"
        bits = []
        if srcs:
            bits.append(f"<b>Hires came from</b> {fl(srcs)}.")
        if dests:
            bits.append(f"<b>Leavers went to</b> {fl(dests)}.")
        if bits:
            out += f'<p class="small soft" style="margin-top:12px">{" ".join(bits)}</p>'
    return out


# --------------------------------------------------------------- business

ROWS = (("Assets under management", "aum", "money"), ("Clients", "clients", "int"),
        ("Total staff", "staff", "int"), ("Advisory staff", "advisors", "int"),
        ("Assets per advisor", "aum_per_advisor", "money"),
        ("Clients per advisor", "clients_per_advisor", "dec"))


def _business(f, gd: dict, history: list) -> str:
    period = gd.get("period") or {}
    days = period.get("days")
    sm = gd.get("staff_mix") or {}
    current = {"aum": f["raum"], "staff": sm.get("total", f["total_employees"]),
               "advisors": sm.get("advisory", f["iar_count"])}
    if current["aum"] and current["advisors"]:
        current["aum_per_advisor"] = current["aum"] / current["advisors"]
    out = ""
    if period.get("from"):
        note = (f"Latest filing, {esc(period.get('to') or '?')}, against {esc(period['from'])}: "
                f"{_months(days)} apart.")
        if (days or 0) > ANNUALISE_AFTER_DAYS:
            note += " More than fifteen months, so each change is also given as a yearly rate."
        out += f'<p class="small soft" style="margin-bottom:10px">{note}</p>'
    else:
        out += _missing("No filing a year or more before the latest one on record, so nothing "
                        "is compared yet. The figures below are the latest filing's.")
    rows = []
    for label, key, kind in ROWS:
        ch = gd.get(key)
        now = ch["now"] if ch else current.get(key)
        prev = ch["prev"] if ch else None
        rows.append(f'<tr><td>{label}</td><td class="num">{_num(now, kind)}</td>'
                    f'<td class="num">{_num(prev, kind)}</td><td class="num">{_change(ch, days)}</td></tr>')
    out += ('<div class="table-scroll"><table class="tight"><thead><tr><th>Measure</th>'
            f'<th class="num">Now</th><th class="num">{esc(_when(period.get("from"))) if period.get("from") else "Earlier"}</th>'
            '<th class="num">Change</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')
    hist = [h for h in history if any(h.get(k) is not None for k in ("raum", "iar_count", "employees", "clients"))]
    if len(hist) >= 2:
        hrows = "".join(
            f'<tr><td class="nowrap">{esc(h["filing_date"][:10])}</td><td class="num">{_num(h.get("raum"), "money")}</td>'
            f'<td class="num">{_num(h.get("clients"))}</td><td class="num">{_num(h.get("employees"))}</td>'
            f'<td class="num">{_num(h.get("iar_count"))}</td></tr>'
            for h in list(reversed(hist))[:10])
        out += (f'<details class="detail-section" style="margin-top:14px"><summary>Every filing on record '
                f'({len(hist)})</summary><div class="table-scroll"><table class="tight"><thead><tr><th>Filed</th>'
                f'<th class="num">Assets</th><th class="num">Clients</th><th class="num">Total staff</th>'
                f'<th class="num">Advisory staff</th></tr></thead><tbody>{hrows}</tbody></table></div>'
                + (f'<p class="meta">The ten most recent of {len(hist)} filings.</p>' if len(hist) > 10 else "")
                + '</details>')
    return out


# ------------------------------------------------------------------ needs

def _need(title: str, verdict: str, body: str) -> str:
    return (f'<div class="need"><h3>{title}</h3><p class="verdict">{verdict}</p>{body}</div>')


def _needs(gd: dict, d: dict) -> str:
    if not gd:
        return _missing("Measured once the Growth job has run for this firm.")
    days = (gd.get("period") or {}).get("days")
    blocks = []

    # Advisors: is each advisor carrying more?
    pa, ca, adv = gd.get("aum_per_advisor"), gd.get("clients_per_advisor"), gd.get("advisors")
    if pa:
        r = pa["annual"]
        verdict = ("Each advisor carries more: assets are growing faster than advisors."
                   if r >= 0.05 else
                   "Advisors are keeping pace with assets." if r > -0.05 else
                   "Advisors have grown faster than assets.")
        rows = [("Assets per advisor", pa, "money"), ("Clients per advisor", ca, "dec"),
                ("Advisory staff", adv, "int")]
        body = "".join(
            f'<div class="kvline"><span>{lbl}</span><span>{_num(ch["prev"], k)} to {_num(ch["now"], k)} '
            f'{_change(ch, days)}</span></div>' for lbl, ch, k in rows if ch)
        blocks.append(_need("Advisors", verdict, body))
    else:
        blocks.append(_need("Advisors", '<span class="muted">Not compared yet</span>',
                            _missing("Needs the number of advisors on two filings a year or "
                                     "more apart.")))

    # Operations, compliance and client service: support staff per advisor.
    sm = gd.get("staff_mix") or {}
    spa, peer, n_adv = sm.get("support_per_advisor"), sm.get("peer"), sm.get("advisory") or 0
    if spa is not None and peer and n_adv >= 2:
        gap = (peer - spa) * n_adv
        if spa < peer:
            verdict = (f"Lean: {spa:.1f} support staff per advisor, where firms with "
                       f"{esc(sm.get('band'))} usually have {peer:.1f}"
                       + (f", about {gap:.0f} fewer people than usual" if gap >= 1 else "") + ".")
        else:
            verdict = (f"Staffed as usual or better: {spa:.1f} support staff per advisor, "
                       f"against {peer:.1f} at similar firms.")
    elif sm.get("total") is not None and n_adv:
        verdict = ("One advisor: support staff not compared with other firms." if n_adv < 2 else
                   "Not enough similar firms to compare with.")
    else:
        verdict = '<span class="muted">Total staff not reported</span>'
    staff = gd.get("staff")
    body = "".join(
        f'<div class="kvline"><span>{lbl}</span><span>{val}</span></div>' for lbl, val in (
            ("Total staff", _num(sm.get("total")) + (f" {_change(staff, days)}" if staff else "")),
            ("Advisory staff", _num(sm.get("advisory"))),
            ("Support staff", _num(sm.get("support"))),
            ("Registered reps of a broker-dealer", _num(sm.get("reps"))),
            ("Licensed insurance agents", _num(sm.get("insurance")))) if val != "-")
    blocks.append(_need("Operations, compliance and client service", verdict, body))

    # Backfill: who left, and how senior.
    deps = gd.get("departures") or []
    left_n = (gd.get("team") or {}).get("left")
    if deps:
        senior = sum(1 for x in deps if x.get("senior"))
        verdict = (f"{left_n or len(deps)} left in the past year"
                   + (f", {senior} of them senior" if senior else "") + ".")
        rows = []
        for x in deps[:8]:
            bits = []
            if x.get("tenure") is not None:
                bits.append(f"{x['tenure']:.0f} years at the firm")
            if x.get("industry") is not None:
                bits.append(f"{x['industry']:.0f} in the industry")
            to = _firm_link(nice_name(x.get("to")) if x.get("to") else None, x.get("to_crd"))
            rows.append(f'<div class="kvline"><span><b>{escn(x["name"])}</b>'
                        f'{" <span class=" + chr(34) + "chip warn" + chr(34) + ">Senior</span>" if x.get("senior") else ""}'
                        f'<div class="meta">{", ".join(bits) or "Tenure not on record"}'
                        f'{" &middot; now at " + to if to else ""}</div></span>'
                        f'<span class="tiny muted nowrap">{esc(x.get("date") or "")}</span></div>')
        body = "".join(rows)
    else:
        verdict = "Nobody left in the past year." if gd.get("team") else '<span class="muted">Not known yet</span>'
        body = ""
    blocks.append(_need("Departures to backfill", verdict, body))

    # Succession: the people with 30 or more years in the industry.
    v = gd.get("veterans") or {}
    if v.get("of"):
        share = (v.get("n30") or 0) / v["of"]
        verdict = (f"{v.get('n30') or 0} of {v['of']} registered people have {VETERAN_YEARS}+ years in "
                   f"the industry ({share:.0%}).")
        body = "".join(f'<div class="kvline"><span>{escn(x["name"])}</span>'
                       f'<span class="meta">in the industry since {esc(x.get("since") or "?")}</span></div>'
                       for x in (v.get("names") or [])[:6])
    else:
        verdict, body = '<span class="muted">Registered people not loaded yet</span>', ""
    blocks.append(_need("Succession", verdict, body))
    return f'<div class="needs">{"".join(blocks)}</div>'


# -------------------------------------------------------------- expansion

def _expansion(gd: dict) -> str:
    if not gd:
        return _missing("Measured once the Growth job has run for this firm.")
    locs = gd.get("locations") or []
    st = gd.get("states") or {}
    new_states = st.get("new") or []
    reg = gd.get("registered") or {}
    events = gd.get("events") or []
    years = reg.get("years")
    out = ('<div class="facts" style="margin-bottom:14px">'
           + _fact("New offices, past year", str(len(locs)))
           + _fact("New states, past year", str(len(new_states)),
                   f"{st.get('total') or 0} in all" if st.get("total") else "")
           + _fact("Registered", esc(_when(reg.get("date"))) if reg.get("date") else "-",
                   (f"{years:.1f} years ago" if years is not None else ""))
           + _fact("Growth signals, 2 years", str(len(events)))
           + '</div>')
    lists = []
    if locs:
        lists.append('<div><h3>New offices</h3><div class="feed">' + "".join(
            f'<div class="it"><div class="ic in">+</div><div><b>{esc((x.get("city") or "").title())}, '
            f'{esc(x.get("state") or "")}</b> <span class="muted">{x.get("people") or 0} '
            f'{"person" if x.get("people") == 1 else "people"} there</span></div>'
            f'<div class="tiny muted nowrap">since {esc(x.get("since") or "")}</div></div>'
            for x in locs[:8]) + '</div></div>')
    if new_states:
        lists.append('<div><h3>New states</h3><div class="feed">' + "".join(
            f'<div class="it"><div class="ic in">+</div><div><b>{esc(x["state"])}</b> '
            f'<span class="muted">{"notice filing" if x.get("kind") == "notice" else "state registration"}'
            f'</span></div><div class="tiny muted nowrap">{esc(x.get("date") or "")}</div></div>'
            for x in new_states[:10]) + '</div></div>')
    if lists:
        out += f'<div class="cols-2">{"".join(lists)}</div>'
    if events:
        out += ('<h3 style="margin-top:16px">Growth signals</h3><div class="timeline">' + "".join(
            f'<div class="ev"><div><span class="nowrap">{esc(e.get("date") or "")}</span>'
            f'<div class="meta">{esc(ui.ago(e.get("date")))}</div></div>'
            f'<div><span class="chip lead">{esc(e.get("label") or "")}</span></div>'
            f'<div class="why">{esc(e.get("text") or "")}</div></div>' for e in events[:8]) + '</div>')
    if not (locs or new_states or events) and not (years is not None and years <= 3):
        out += _missing("No new offices, states or launches seen in the last year. New offices "
                        "come from where newly registered people work; new states from the "
                        "firm's state notice filings and registrations.")
    return out


# ------------------------------------------------------------------ roles

def _roles(crd: str, d: dict) -> str:
    cr = d.get("careers") or {}
    posts = d.get("postings") or []
    btn = (f'<button class="sm ghost" data-post="/api/firm/{esc(crd)}/roles" data-busy="Starting">'
           f'Check for openings now</button>')
    line = []
    if cr.get("careers_url"):
        u = cr["careers_url"]
        line.append(f'<a href="{esc(u)}" target="_blank" rel="noopener" data-noprefetch>Careers page</a>'
                    + (f' <span class="muted">via {esc(cr["ats"])}</span>' if cr.get("ats") else ""))
    if cr.get("checked_at"):
        line.append(f"website checked {esc(ui.ago(cr['checked_at']))}")
    if cr.get("searched_at"):
        line.append(f"job boards searched {esc(ui.ago(cr['searched_at']))}")
    head = (f'<div class="row" style="justify-content:space-between;gap:12px;margin-bottom:12px">'
            f'<span class="small soft">{" &middot; ".join(line) or "Not checked yet"}</span>{btn}</div>')
    if not posts:
        status = cr.get("status")
        msg = {
            "no_openings": "The careers page says there are no openings right now.",
            "page": "The firm has a careers page, with no openings listed on it.",
            "no_page": "No careers page found on the firm's website.",
            "unreachable": "The firm's website did not answer; it is tried again in a few days.",
            "no_website": "No website on file.",
        }.get(status or "", "Not checked yet. The Open roles job reads every firm's website, "
                             "growing firms first, and the Job boards job searches LinkedIn, "
                             "Indeed and the others.")
        if status and cr.get("searched_at"):
            msg += " Nothing on the job boards names the firm either."
        elif status and status != "no_website":
            msg += " The job boards are searched next."
        return head + _missing(msg)
    by = _by_function(posts)
    chips = " ".join(f'<span class="chip">{esc(k)} {len(v)}</span>' for k, v in by.items())
    rows = []
    for fn, items in by.items():
        for x in items:
            where = (x.get("host") or "") if x.get("source") == "job_board" else (
                SOURCE_WORDS.get(x.get("source"), "") if x.get("source") != "ats" else (cr.get("ats") or "Tracking system"))
            rows.append(f'<tr><td><a href="{esc(x.get("url") or "")}" target="_blank" rel="noopener" '
                        f'data-noprefetch>{esc(x["title"])}</a></td><td class="small">{esc(fn)}</td>'
                        f'<td class="small muted">{esc(where)}</td>'
                        f'<td class="tiny muted nowrap">{esc(ui.ago(x.get("first_seen")))}</td></tr>')
    return (head + f'<div class="pills" style="margin-bottom:12px">{chips}</div>'
            '<div class="table-scroll"><table class="tight"><thead><tr><th>Role</th><th>Function</th>'
            '<th>Found on</th><th>First seen</th></tr></thead><tbody>' + "".join(rows)
            + '</tbody></table></div>')
