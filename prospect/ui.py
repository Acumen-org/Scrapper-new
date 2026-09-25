"""Pieces every screen shares: contact readiness, the one-line reason a firm
scored where it did, and filter controls. Kept out of webapp.py so the views
can import them without pulling in each other."""

from __future__ import annotations

import json

from . import products
from .webapp import esc, money, nice_name

STATUS_OPTIONS = ["new", "working", "meeting set", "qualified", "disqualified",
                  "customer"]


def contact_flags(c, crds: list[str]) -> dict[str, dict]:
    """How reachable each firm is, in one pass: real emails (filed in the
    brochure or on their own site), guessed ones on a live domain, people, and
    the main phone."""
    out = {k: {"filed": 0, "web": 0, "guess": 0, "people": 0, "phone": False}
           for k in crds}
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

    each(f"SELECT crd, COUNT(*) n FROM firm_contact_info WHERE kind='email'"
         f" AND crd IN ({ph}) GROUP BY crd", lambda d, r: d.__setitem__("filed", r["n"]))
    each(f"SELECT crd, COUNT(*) n FROM web_contact WHERE email IS NOT NULL"
         f" AND crd IN ({ph}) GROUP BY crd", lambda d, r: d.__setitem__("web", r["n"]))
    each(f"SELECT crd, COUNT(*) n FROM contact_email WHERE status='domain_accepts_mail'"
         f" AND crd IN ({ph}) GROUP BY crd", lambda d, r: d.__setitem__("guess", r["n"]))
    each(f"SELECT crd, COUNT(*) n FROM schedule_a WHERE is_individual=1"
         f" AND crd IN ({ph}) GROUP BY crd", lambda d, r: d.__setitem__("people", r["n"]))
    each(f"SELECT crd, phone FROM firm_current WHERE crd IN ({ph})",
         lambda d, r: d.__setitem__("phone", bool(r["phone"])))
    return out


def contact_cell(f: dict) -> str:
    bits = []
    real = f["filed"] + f["web"]
    if real:
        bits.append(f'<span class="chip lead" title="Addresses the firm itself '
                    f'published">{real} email{"s" if real > 1 else ""}</span>')
    elif f["guess"]:
        bits.append(f'<span class="chip" title="Pattern-guessed on a domain that '
                    f'accepts mail">{f["guess"]} guessed</span>')
    if f["phone"]:
        bits.append('<span class="chip line" title="Main office phone, Form ADV">'
                    'phone</span>')
    if f["people"]:
        bits.append(f'<span class="meta" style="display:block;white-space:nowrap">'
                    f'{f["people"]} officers</span>')
    return " ".join(bits) or '<span class="muted small">none yet</span>'


def why_line(detail_json: str | None, n: int = 2) -> str:
    """The criteria that earned the most points, in the firm's own evidence."""
    if not detail_json:
        return ""
    try:
        d = json.loads(detail_json)
    except ValueError:
        return ""
    comps = sorted(d.get("components", []), key=lambda c: -c["contrib"])[:n]
    parts = []
    for c in comps:
        if c["contrib"] <= 0:
            continue
        ev = c["evidence"] or c["level"]
        if len(ev) > 90:
            ev = ev[:87].rstrip() + "..."
        parts.append(f'<b>{esc(c["label"])}</b> {esc(ev)}')
    return "<br>".join(parts)


def pitch_of(detail_json: str | None) -> str:
    try:
        return (json.loads(detail_json or "{}") or {}).get("pitch") or ""
    except ValueError:
        return ""


def opt(value, current, label) -> str:
    sel = " selected" if str(value) == str(current) else ""
    return f'<option value="{esc(value)}"{sel}>{esc(label)}</option>'


def firm_meta(r) -> str:
    bits = [f"CRD {esc(r['crd'])}"]
    if r.get("city") or r.get("state"):
        bits.append(esc(" ".join(x for x in (nice_name(r.get("city")), r.get("state")) if x)))
    bits.append(f"{money(r.get('raum'))} AUM")
    return " &middot; ".join(bits)


def product_name(key: str) -> str:
    try:
        return products.product(key)["name"]
    except KeyError:
        return key
