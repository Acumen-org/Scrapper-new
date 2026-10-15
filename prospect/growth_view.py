"""The firm page's Growth tab: is the firm growing, and where will it need people?

One panel per question, most direct first (prospect/growth.py builds the
figures every day; prospect/openroles.py finds the openings):

  At a glance   the team, assets, clients and open roles in one row
  Open roles    what the firm is hiring for now, from its careers page, its
                applicant-tracking system and LinkedIn
  Team          registered people now and a year ago, who joined and who
                left, from where and to where, year by year, and teams hired
                from one firm
  Assets, clients and staff
                the latest filing against the one about a year before it,
                with both dates
  Hiring needs  by function: advisors, operations and support staff against
                similar firms, backfill, succession
  Expansion     new offices, new states, a recent registration, launches and
                custodian changes

Every comparison names the filings it compares. A change over more than
fifteen months is also given as a yearly rate, so growth over three years is
never read as one year's. Missing data is said to be missing, with what will
fill it.
"""

from __future__ import annotations

from datetime import date

from . import ui
from .growth import (ANNUALISE_AFTER_DAYS, SENIOR_INDUSTRY_YEARS, SENIOR_TENURE_YEARS,
                     VETERAN_YEARS)
from .webapp import esc, escn, money, nice_name

SOURCE_WORDS = {"website": "Careers page", "ats": "Tracking system", "job_board": "Job board"}
FUNCTION_ORDER = ("Advisory", "Operations and client service", "Compliance", "Finance and tax",
                  "Marketing and growth", "Technology", "People and HR", "Leadership", "Other")


# ------------------------------------------------------------- formatting

def _pct(v: float, signed: bool = True) -> str:
    r = round(v * 100)
    if r == 0:
        return "0%"
    return f"{'+' if signed and r > 0 else ''}{r:.0f}%"


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
        out += f'<div class="meta nowrap">{_pct(ch["annual"])} a year</div>'
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


def _not_measured_term(text: str = "Not measured yet") -> str:
    return '<span class="tterm"' + ui.tip_attr(
        "Not measured yet", "The Growth job compares each firm's Form ADV filings once a "
        "day; this firm has not had its turn yet. Team moves and open roles are already "
        "current.") + f'>{text}</span>'


def _team_now(gd: dict, stats: dict | None) -> dict:
    """The registered team now and a year ago, with the year's moves, from the
    Growth job's figures or, before it has run, from the roster stats."""
    t = dict(gd.get("team") or {})
    if stats:
        j, left = int(stats.get("hires_12m") or 0), int(stats.get("departures_12m") or 0)
        head = stats.get("headcount")
        if t.get("now") is None:
            t["now"] = head
        if t.get("prev") is None and head is not None:
            t["prev"] = head - (j - left)
        if t.get("joined") is None:
            t["joined"] = j
        if t.get("left") is None:
            t["left"] = left
    return t


def _team_cell(t: dict, label: str = "Team, past 12 months") -> str:
    """Who joined and who left as two labelled figures (ui.moves_html), with
    the headcount now and a year ago under them. Never one sentence."""
    if t.get("now") is None:
        return _fact(label, "-", "roster not loaded yet")
    under = f"{t['now']:,} registered now"
    if t.get("prev") is not None:
        under += f", {t['prev']:,} a year ago"
    return (f'<div><div class="l">{label}</div><div class="v">'
            f'{ui.moves_html(t.get("joined"), t.get("left"), "")}</div>'
            f'<div class="meta">{under}</div></div>')


def _not_measured(label: str) -> str:
    return _fact(label, "-", _not_measured_term("not measured yet"))


# ----------------------------------------------------------------- glance

def glance(d: dict, stats: dict | None) -> str:
    """The overview's Growth card: the team, assets and open roles."""
    g = d.get("growth") or {}
    gd = g.get("detail") or {}
    cells = [_team_cell(_team_now(gd, stats))]
    aum = gd.get("aum")
    period = gd.get("period") or {}
    if aum:
        rate = aum["annual"] if (period.get("days") or 0) > ANNUALISE_AFTER_DAYS else aum["change"]
        unit = " a year" if (period.get("days") or 0) > ANNUALISE_AFTER_DAYS else ""
        cells.append(_fact("Assets", f'<span class="{_tone(rate)}">{_pct(rate)}{unit}</span>',
                           f"since the {_when(period.get('from'))} filing"))
    elif not g:
        cells.append(_not_measured("Assets"))
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
    return f'<div class="facts three">{"".join(cells)}</div>'


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

def section(crd: str, f, d: dict, stats: dict | None, mv: dict, series: list) -> str:
    """The whole Growth tab: one panel per question, most direct first."""
    g = d.get("growth") or {}
    gd = g.get("detail") or {}
    return "".join((
        ui.panel("At a glance", _summary(d, gd, stats, bool(g)), sub="Against a year ago"),
        ui.panel("Open roles", _roles(d), anchor="growth-roles",
                 sub="From their careers page, applicant-tracking system and LinkedIn",
                 acts=(f'<button class="sm ghost" data-post="/api/firm/{esc(crd)}/roles" '
                       f'data-busy="Starting">Check for openings now</button>')),
        ui.panel("Team", _team(gd, stats, mv, series), anchor="hiring",
                 sub="Joins and departures, from SEC registration records"),
        ui.panel("Assets, clients and staff", _business(f, gd), anchor="growth-business",
                 sub=_business_sub(gd)),
        ui.panel("Hiring needs", _needs(gd), anchor="growth-needs",
                 sub="Where the firm is likely to need people next, by function"),
        ui.panel("Expansion", _expansion(gd), anchor="growth-expansion",
                 sub="New offices, new states and launches in the past year"),
    ))


def _summary(d: dict, gd: dict, stats, measured: bool = True) -> str:
    team = _team_cell(_team_now(gd, stats))
    period = gd.get("period") or {}
    long_gap = (period.get("days") or 0) > ANNUALISE_AFTER_DAYS

    def rate(key: str, label: str) -> str:
        ch = gd.get(key)
        if not ch and not measured:
            return _not_measured(label)
        if not ch:
            return _fact(label, "-", "no earlier filing to compare")
        v = ch["annual"] if long_gap else ch["change"]
        return _fact(label, f'<span class="{_tone(v)}">{_pct(v)}{" a year" if long_gap else ""}</span>',
                     f"since the {_when(period.get('from'))} filing")

    posts = d.get("postings") or []
    cr = d.get("careers") or {}
    if posts:
        roles = _fact("Open roles", f'<span class="ok">{len(posts)} open</span>',
                      esc(_top_functions(posts)))
    else:
        roles = _fact("Open roles", "None found" if cr else "-",
                      f"checked {esc(ui.ago(cr.get('checked_at')))}" if cr else "not checked yet")
    return (f'<div class="facts four">{team}{rate("aum", "Assets")}{rate("clients", "Clients")}'
            f'{roles}</div>')


# ------------------------------------------------------------------- team

LIST_MAX = 8


def _team(gd: dict, stats, mv: dict, series: list) -> str:
    if not stats and not mv.get("joined") and not mv.get("left"):
        return _missing("Joins and departures come from the SEC individual feed and appear "
                        "once it has loaded.")
    out = ""
    if stats:
        t = _team_now(gd, stats)
        head, prev = t.get("now"), t.get("prev")

        def moves(j, left, label):
            return (f'<div><div class="l">{label}</div><div class="v">'
                    f'{ui.moves_html(j, left, "")}</div></div>')
        out += ('<div class="facts three">'
                + f'<div><div class="l">Registered now</div><div class="v">'
                  f'{f"{head:,}" if head is not None else "-"}</div>'
                + (f'<div class="meta">{prev:,} a year ago</div>' if prev is not None else "")
                + '</div>'
                + moves(t.get("joined"), t.get("left"), "Past 12 months")
                + moves(stats.get("hires_prev_12m"), stats.get("departures_prev_12m"),
                        "The 12 months before")
                + '</div>')
    if series:
        out += (f'<div class="sub-sec"><h3>Joined and left, year by year</h3>'
                f'{ui.year_bars(series)}</div>')
    lifts = gd.get("lift_outs") or []
    if lifts:
        out += ('<div class="sub-sec"><h3>Teams hired together, past 12 months</h3>'
                '<ul class="evidence-list">'
                + "".join(f'<li>{int(x["n"])} people joined from '
                          f'{_firm_link(x.get("org"), x.get("org_crd"))}</li>'
                          for x in sorted(lifts, key=lambda x: -(x.get("n") or 0))[:3])
                + '</ul></div>')

    def lst(items, kind):
        if not items:
            return '<p class="muted">Nobody in the past two years.</p>'
        word = "from" if kind == "joined" else "now at"
        rows = []
        for m in items[:LIST_MAX]:
            other = _firm_link(m.get("other_org_name"), m.get("other_org_pk"))
            rows.append(
                f'<div class="it"><div class="ic {"in" if kind == "joined" else "out"}">'
                f'{"+" if kind == "joined" else "-"}</div><div><b>{escn(m["name"])}</b>'
                + (f'<div class="meta">{word} {other}</div>' if other else "")
                + f'</div><div class="tiny muted nowrap">{esc((m.get("date") or "")[:10])}</div></div>')
        more = (f'<p class="meta">and {len(items) - LIST_MAX} more</p>'
                if len(items) > LIST_MAX else "")
        return f'<div class="feed">{"".join(rows)}</div>{more}'

    # Where hires came from sits under the joiners, where leavers went under
    # the leavers: never one sentence about both.
    srcs = (stats or {}).get("top_sources") or []
    dests = (stats or {}).get("top_destinations") or []

    def fl(items, label):
        firms = ", ".join(f'{_firm_link(x.get("org_name"), x.get("org_pk"))} ({x.get("n")})'
                          for x in items[:5] if x.get("org_name"))
        return f'<p class="moves-from"><span class="l">{label}</span> {firms}</p>' if firms else ""
    moves = (f'<div class="cols-2"><div><h3>Joined, past two years</h3>{lst(mv.get("joined"), "joined")}'
             f'{fl(srcs, "Hires in the past three years came from")}</div>'
             f'<div><h3>Left, past two years</h3>{lst(mv.get("left"), "left")}'
             f'{fl(dests, "Leavers in the past three years went to")}</div></div>')
    out += f'<div class="sub-sec">{moves}</div>'
    return out


# --------------------------------------------------------------- business

ROWS = (("Assets under management", "aum", "money"), ("Clients", "clients", "int"),
        ("Total staff", "staff", "int"), ("Advisory staff", "advisors", "int"),
        ("Assets per advisor", "aum_per_advisor", "money"),
        ("Clients per advisor", "clients_per_advisor", "dec"))


def _business_sub(gd: dict) -> str:
    """Which two filings the table compares, said once in the panel's subtitle."""
    period = gd.get("period") or {}
    if period.get("from"):
        return (f"Form ADV filed {esc(_when(period.get('to')))} against "
                f"{esc(_when(period['from']))}, {_months(period.get('days'))} earlier")
    return "The latest Form ADV filing; no earlier one on record to compare"


def _business(f, gd: dict) -> str:
    period = gd.get("period") or {}
    days = period.get("days")
    compared = bool(period.get("from"))
    sm = gd.get("staff_mix") or {}
    current = {"aum": f["raum"], "clients": f["clients_total"],
               "staff": sm.get("total") if sm else f["total_employees"],
               "advisors": sm.get("advisory") if sm else f["iar_count"]}
    if current["aum"] and current["advisors"]:
        current["aum_per_advisor"] = current["aum"] / current["advisors"]
    if current["clients"] is not None and current["advisors"]:
        current["clients_per_advisor"] = current["clients"] / current["advisors"]
    rows = []
    for label, key, kind in ROWS:
        ch = gd.get(key)
        now = ch["now"] if ch else current.get(key)
        prev = ch["prev"] if ch else None
        rows.append(f'<tr><td>{label}</td><td class="num">{_num(now, kind)}</td>'
                    + (f'<td class="num">{_num(prev, kind)}</td><td class="num">{_change(ch, days)}</td>'
                       if compared else "") + '</tr>')
    head = '<th>Measure</th><th class="num">Latest filing</th>'
    if compared:
        change = "Change"
        if (days or 0) > ANNUALISE_AFTER_DAYS:
            change = ('<span class="tterm"' + ui.tip_attr(
                "Change, and per year", f"These filings are {_months(days)} apart, so each "
                "change is also shown as a yearly rate.") + '>Change</span>')
        head = (f'<th>Measure</th><th class="num">{esc(_when(period.get("to")))}</th>'
                f'<th class="num">{esc(_when(period["from"]))}</th><th class="num">{change}</th>')
    return ('<div class="table-scroll"><table class="tight"><thead><tr>' + head
            + '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')


# ------------------------------------------------------------------ needs

def _need(title: str, verdict: str, body: str) -> str:
    return f'<div class="need"><h3>{title}</h3><p class="verdict">{verdict}</p>{body}</div>'


def _needs(gd: dict) -> str:
    if not gd:
        return _missing(_not_measured_term())
    days = (gd.get("period") or {}).get("days")
    blocks = []

    # Advisors: is each advisor carrying more?
    pa, ca, adv = gd.get("aum_per_advisor"), gd.get("clients_per_advisor"), gd.get("advisors")
    if pa:
        r = pa["annual"]
        verdict = ("Each advisor is carrying more: assets are growing faster than the advisors."
                   if r >= 0.05 else
                   "Advisors are keeping pace with assets." if r > -0.05 else
                   "Advisors have grown faster than assets.")
        rows = [("Assets per advisor", pa, "money"), ("Clients per advisor", ca, "dec"),
                ("Advisory staff", adv, "int")]
        body = "".join(
            f'<div class="kvline"><span>{lbl}</span><span>{_num(ch["prev"], k)} to '
            f'{_num(ch["now"], k)} {_change(ch, days)}</span></div>' for lbl, ch, k in rows if ch)
    else:
        verdict = "Not compared yet: needs the advisor count on two filings a year apart."
        body = ""
    blocks.append(_need("Advisors", verdict, body))

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
        verdict = ("One advisor, so support staff is not compared with other firms." if n_adv < 2
                   else "Not enough similar firms to compare with.")
    else:
        verdict = "Total staff not reported."
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
                   + (f", {senior} of them senior." if senior else "."))
        rows = []
        for x in deps[:LIST_MAX]:
            bits = []
            if x.get("tenure") is not None:
                bits.append(f"{x['tenure']:.0f} years at the firm")
            if x.get("industry") is not None:
                bits.append(f"{x['industry']:.0f} in the industry")
            to = _firm_link(nice_name(x.get("to")) if x.get("to") else None, x.get("to_crd"))
            tag = (' <span class="chip warn"' + ui.tip_attr(
                "Senior departure", f"{SENIOR_TENURE_YEARS}+ years at the firm or "
                f"{SENIOR_INDUSTRY_YEARS}+ in the industry: likely to need a replacement.")
                   + '>Senior</span>') if x.get("senior") else ""
            rows.append(f'<div class="kvline"><span><b>{escn(x["name"])}</b>{tag}'
                        f'<div class="meta">{", ".join(bits) or "Tenure not on record"}'
                        f'{" &middot; now at " + to if to else ""}</div></span>'
                        f'<span class="tiny muted nowrap">{esc(x.get("date") or "")}</span></div>')
        body = "".join(rows)
    else:
        verdict = "Nobody left in the past year." if gd.get("team") else "Not known yet."
        body = ""
    blocks.append(_need("Backfill", verdict, body))

    # Succession: the people with 30 or more years in the industry.
    v = gd.get("veterans") or {}
    if v.get("of"):
        share = (v.get("n30") or 0) / v["of"]
        verdict = (f"{v.get('n30') or 0} of {v['of']} registered people have {VETERAN_YEARS}+ "
                   f"years in the industry ({share:.0%}).")
        body = "".join(f'<div class="kvline"><span>{escn(x["name"])}</span>'
                       f'<span class="meta">since {esc(x.get("since") or "?")}</span></div>'
                       for x in (v.get("names") or [])[:6])
    else:
        verdict, body = "Registered people not loaded yet.", ""
    blocks.append(_need("Succession", verdict, body))
    return f'<div class="needs">{"".join(blocks)}</div>'


# -------------------------------------------------------------- expansion

def _expansion(gd: dict) -> str:
    if not gd:
        return _missing(_not_measured_term())
    locs = gd.get("locations") or []
    st = gd.get("states") or {}
    new_states = st.get("new") or []
    reg = gd.get("registered") or {}
    events = gd.get("events") or []
    years = reg.get("years")
    out = ('<div class="facts three">'
           + _fact("New offices", str(len(locs)), "in the past year")
           + _fact("New states", str(len(new_states)),
                   f"in the past year, {st.get('total')} in all" if st.get("total") else "in the past year")
           + _fact("Registered", esc(_when(reg.get("date"))) if reg.get("date") else "-",
                   (f"{years:.1f} years ago" if years is not None else ""))
           + '</div>')
    lists = []
    if locs:
        lists.append('<div><h3>New offices</h3><div class="feed">' + "".join(
            f'<div class="it"><div class="ic in">+</div><div><b>{esc((x.get("city") or "").title())}, '
            f'{esc(x.get("state") or "")}</b><div class="meta">{x.get("people") or 0} '
            f'{"person" if x.get("people") == 1 else "people"} there</div></div>'
            f'<div class="tiny muted nowrap">since {esc(x.get("since") or "")}</div></div>'
            for x in locs[:LIST_MAX]) + '</div></div>')
    if new_states:
        lists.append('<div><h3>New states</h3><div class="feed">' + "".join(
            f'<div class="it"><div class="ic in">+</div><div><b>{esc(x["state"])}</b>'
            f'<div class="meta">{"notice filing" if x.get("kind") == "notice" else "state registration"}'
            f'</div></div><div class="tiny muted nowrap">{esc(x.get("date") or "")}</div></div>'
            for x in new_states[:10]) + '</div></div>')
    if lists:
        out += f'<div class="sub-sec"><div class="cols-2">{"".join(lists)}</div></div>'
    if events:
        out += ('<div class="sub-sec"><h3>Growth signals, past two years</h3><div class="timeline">'
                + "".join(
                    f'<div class="ev"><div><span class="nowrap">{esc(e.get("date") or "")}</span>'
                    f'<div class="meta">{esc(ui.ago(e.get("date")))}</div></div>'
                    f'<div><span class="chip lead">{esc(e.get("label") or "")}</span></div>'
                    f'<div class="why">{esc(e.get("text") or "")}</div></div>' for e in events[:8])
                + '</div></div>')
    if not (locs or new_states or events):
        out += ('<p class="sub-sec muted">No new offices, states or launches in the past year.</p>')
    return out


# ------------------------------------------------------------------ roles

def _roles(d: dict) -> str:
    cr = d.get("careers") or {}
    posts = d.get("postings") or []
    line = []
    if cr.get("careers_url"):
        line.append(f'<a href="{esc(cr["careers_url"])}" target="_blank" rel="noopener" '
                    f'data-noprefetch>Careers page</a>'
                    + (f' on {esc(cr["ats"])}' if cr.get("ats") else ""))
    if cr.get("checked_at"):
        line.append(f"website checked {esc(ui.ago(cr['checked_at']))}")
    if cr.get("searched_at"):
        line.append(f"LinkedIn searched {esc(ui.ago(cr['searched_at']))}")
    status_line = f'<p class="meta roles-status">{" &middot; ".join(line)}</p>' if line else ""
    if not posts:
        status = cr.get("status")
        msg = {
            "no_openings": "The careers page says there are no openings right now.",
            "page": "The firm has a careers page with no openings listed on it.",
            "no_page": "No careers page found on the firm's website.",
            "unreachable": "The firm's website did not answer; it is tried again in a few days.",
            "no_website": "No website on file.",
        }.get(status or "", '<span class="tterm"' + ui.tip_attr(
            "Not checked yet", "Every firm's website is read in turn, growing firms first, "
            "then LinkedIn is searched. Check for openings now reads this one straight away.")
            + '>Not checked yet.</span>')
        if status and cr.get("searched_at"):
            msg += " No LinkedIn posting names the firm either."
        elif status and status != "no_website":
            msg += " LinkedIn is searched next."
        return status_line + f'<p class="muted" style="margin:0">{msg}</p>'
    by = _by_function(posts)
    chips = " ".join(f'<span class="chip">{esc(k)} {len(v)}</span>' for k, v in by.items())
    rows = []
    for fn, items in by.items():
        for x in items:
            if x.get("source") == "job_board":
                where = x.get("host") or "Job board"
            elif x.get("source") == "ats":
                where = cr.get("ats") or "Tracking system"
            else:
                where = SOURCE_WORDS.get(x.get("source"), "")
            rows.append(f'<tr><td><a href="{esc(x.get("url") or "")}" target="_blank" rel="noopener" '
                        f'data-noprefetch>{esc(x["title"])}</a></td><td data-l="Function">{esc(fn)}</td>'
                        f'<td class="muted" data-l="Found on">{esc(where)}</td>'
                        f'<td class="muted" data-l="First seen">{esc(ui.ago(x.get("first_seen")))}</td></tr>')
    return (status_line + f'<div class="pills roles-chips">{chips}</div>'
            '<div class="table-scroll"><table class="tight roles-table"><thead><tr><th>Role</th><th>Function</th>'
            '<th>Found on</th><th>First seen</th></tr></thead><tbody>' + "".join(rows)
            + '</tbody></table></div>')
