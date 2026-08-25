"""Infer decision-maker emails at scale, from patterns the firm itself uses.

The problem: Schedule A names who runs each firm, but no filing carries their
email. The insight: we now hold thousands of REAL person-to-email pairs (from
brochures and firm websites), and firms almost always use one pattern for
everyone. If jsmith@firm.com is real for Jane Smith, then the CEO's address is
knowable with high confidence.

Three tiers of inference, each labelled so the export can say how a guess was
made:

  observed_at_firm   A real person email at this same firm matches a known
                     pattern; apply that pattern to the firm's other people.
  common_pattern     No observed pair at this firm; use the most common pattern
                     across all firms where we HAVE observed one, at lower
                     confidence.
  (none)             No usable mail domain at all; generate nothing.

Every generated address gets the free local check (syntax + does the domain
publish a mail server). Nothing external, nothing paid, and no claim that a
mailbox exists: the tiers say how the guess was made, the check says whether the
domain can receive mail at all.

Targets decision makers first: Schedule A officers of firms on the scored lists,
then reps. Deduped against real addresses we already hold, which need no guess.

    python -m scripts.infer_emails [--limit N] [--all-band]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, mailcheck, runlog  # noqa: E402
from prospect.mailcheck import BAD_EMAIL_DOMAINS  # noqa: E402

PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "flast":      lambda f, l: f"{f[0]}{l}",
    "first":      lambda f, l: f,
    "firstlast":  lambda f, l: f"{f}{l}",
    "first_l":    lambda f, l: f"{f}{l[0]}",
    "f.last":     lambda f, l: f"{f[0]}.{l}",
    "last":       lambda f, l: l,
}


def name_parts(full: str) -> tuple[str, str] | None:
    parts = [x for x in re.sub(r"[^a-z ]", "", (full or "").lower()).split() if x]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def detect_pattern(first: str, last: str, local: str) -> str | None:
    for pat, fn in PATTERNS.items():
        if fn(first, last) == local:
            return pat
    return None


def pretty(filed: str) -> str:
    parts = [p.strip() for p in filed.split(",") if p.strip()]
    return (" ".join(parts[1:] + parts[:1]) if len(parts) >= 2 else filed).title()


def observed_pairs(conn):
    """(crd, domain, pattern) for every real person-email we hold, plus the
    global pattern popularity for the fallback tier."""
    firm_pattern: dict[str, tuple[str, str]] = {}
    popularity: Counter = Counter()
    rows = list(conn.execute(
        "SELECT crd, person, email FROM web_contact"
        " WHERE person IS NOT NULL AND email IS NOT NULL"))
    rows += [(r["crd"], r["name"], r["value"]) for r in conn.execute(
        """SELECT f.crd, s.name, f.value FROM firm_contact_info f
           JOIN schedule_a s ON s.crd = f.crd AND s.is_individual = 1
           WHERE f.kind='email'""")]
    for crd, person, email in rows:
        np = name_parts(pretty(person) if "," in person else person)
        if not np:
            continue
        local, _, dom = email.lower().partition("@")
        if not dom or any(b in dom for b in BAD_EMAIL_DOMAINS):
            continue
        pat = detect_pattern(np[0], np[1], local)
        if pat:
            firm_pattern.setdefault(crd, (dom, pat))
            popularity[pat] += 1
    return firm_pattern, popularity


def firm_domain(conn, crd: str) -> str | None:
    r = conn.execute("""SELECT value FROM firm_contact_info
        WHERE crd=? AND kind='email' ORDER BY id LIMIT 1""", (crd,)).fetchone()
    if r:
        dom = r["value"].rsplit("@", 1)[-1].lower()
        if not any(b in dom for b in BAD_EMAIL_DOMAINS):
            return dom
    r = conn.execute("SELECT website FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if r and r["website"]:
        m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})",
                      r["website"].lower())
        if m and not any(b in m.group(1) for b in BAD_EMAIL_DOMAINS):
            return m.group(1)
    return None


def targets(conn, limit: int, all_band: bool):
    """Decision makers lacking any known email, scored firms first."""
    scope = "" if all_band else """
          AND (f.crd IN (SELECT crd FROM tier_a_rank)
               OR f.crd IN (SELECT crd FROM tier_c_score WHERE rank<=1000)
               OR f.crd IN (SELECT crd FROM firm_overlay WHERE phh_13f=1))"""
    return conn.execute(f"""
        SELECT s.crd, s.name FROM schedule_a s
        JOIN firm_current f ON f.crd = s.crd
        WHERE s.is_individual = 1
          AND f.is_era = 0 AND f.raum >= 25e6 AND f.raum < 500e6{scope}
        ORDER BY (SELECT MIN(rank) FROM tier_a_rank t WHERE t.crd=s.crd) IS NULL,
                 (SELECT MIN(rank) FROM tier_a_rank t WHERE t.crd=s.crd)
        LIMIT {int(limit)}""").fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--all-band", action="store_true")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    with runlog.Run(conn, "infer_emails", "infer", cfg.stamp) as run:
        firm_pat, popularity = observed_pairs(conn)
        fallback = popularity.most_common(1)[0][0] if popularity else "first.last"
        print(f"firms with an observed pattern: {len(firm_pat):,}; "
              f"pattern popularity: {dict(popularity.most_common(5))}; "
              f"fallback: {fallback}")

        have = {(r["crd"], (r["name"] or "").lower()) for r in conn.execute(
            "SELECT crd, name FROM contact_email")}
        real = {(r["crd"], (r["person"] or "").lower()) for r in conn.execute(
            "SELECT crd, person FROM web_contact WHERE email IS NOT NULL")}

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mx_cache: dict[str, bool | None] = {}
        made = skipped = 0
        for t in targets(conn, args.limit, args.all_band):
            person = pretty(t["name"])
            key = (t["crd"], person.lower())
            if key in have or key in real:
                skipped += 1
                continue
            np = name_parts(person)
            if not np:
                continue
            if t["crd"] in firm_pat:
                dom, pat = firm_pat[t["crd"]]
                label = f"{pat} (observed at firm)"
            else:
                dom = firm_domain(conn, t["crd"])
                pat, label = fallback, f"{fallback} (common pattern)"
            if not dom:
                continue
            addr = f"{PATTERNS[pat](np[0], np[1])}@{dom}"
            if not mailcheck.valid_syntax(addr):
                continue
            if dom not in mx_cache:
                mx_cache[dom] = mailcheck.has_mx(dom)
            mx = mx_cache[dom]
            status = ("domain_accepts_mail" if mx else
                      "no_mail_server" if mx is False else "queued")
            conn.execute("""INSERT OR IGNORE INTO contact_email
                (crd, name, email, pattern, status, checked_at)
                VALUES (?,?,?,?,?,?)""",
                         (t["crd"], person, addr, label, status, now))
            made += 1
            if made % 500 == 0:
                conn.commit()
        conn.commit()
        run.rows_out = made
        ok = conn.execute("""SELECT COUNT(*) n FROM contact_email
            WHERE status='domain_accepts_mail'""").fetchone()["n"]
        print(f"generated {made:,} decision-maker guesses "
              f"({skipped:,} already had an address); "
              f"{ok:,} total guesses now on live mail domains")
        run.note(f"made={made} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
