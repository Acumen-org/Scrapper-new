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

from prospect import config, db, emailguess, mailcheck, runlog  # noqa: E402

# One best guess per person, using the pattern the firm actually uses. The
# generation logic is shared with the firm-page button (prospect.emailguess) so
# the two never diverge.
name_parts = emailguess.name_parts
pretty = emailguess.pretty


def targets(conn, limit: int, all_band: bool):
    """Decision makers lacking any known email, scored firms first."""
    scope = "" if all_band else """
          AND (f.crd IN (SELECT crd FROM tier_a_rank)
               OR f.crd IN (SELECT crd FROM tier_c_score WHERE rank<=1000)
               OR f.crd IN (SELECT crd FROM firm_overlay WHERE phh_13f=1))"""
    return conn.execute(f"""
        SELECT s.crd, s.name, s.title FROM schedule_a s
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
        firm_pat, fallback = emailguess.observed(conn)
        print(f"firms with an observed pattern: {len(firm_pat):,}; "
              f"fallback pattern: {fallback}")

        have = {(r["crd"], (r["name"] or "").lower()) for r in conn.execute(
            "SELECT crd, name FROM contact_email")}
        real = {(r["crd"], (r["person"] or "").lower()) for r in conn.execute(
            "SELECT crd, person FROM web_contact WHERE email IS NOT NULL")}

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mx_cache: dict[str, bool | None] = {}
        made = skipped = 0
        # One address per person: best_email picks a single pattern, so a person
        # never appears three times the way the old firm-page button produced.
        dom_by_crd: dict[str, str | None] = {}
        for t in targets(conn, args.limit, args.all_band):
            person = pretty(t["name"])
            key = (t["crd"], person.lower())
            if key in have or key in real:
                skipped += 1
                continue
            if t["crd"] not in dom_by_crd:
                dom_by_crd[t["crd"]] = emailguess.domain_for(conn, t["crd"])
            guess = emailguess.best_email(person, t["crd"], firm_pat, fallback,
                                          dom_by_crd[t["crd"]])
            if not guess:
                continue
            addr, label = guess
            if not mailcheck.valid_syntax(addr):
                continue
            have.add(key)         # never emit the same person twice this run
            dom = addr.rsplit("@", 1)[-1]
            if dom not in mx_cache:
                mx_cache[dom] = mailcheck.has_mx(dom)
            mx = mx_cache[dom]
            status = ("domain_accepts_mail" if mx else
                      "no_mail_server" if mx is False else "queued")
            conn.execute("""INSERT OR IGNORE INTO contact_email
                (crd, name, title, email, pattern, status, checked_at)
                VALUES (?,?,?,?,?,?,?)""",
                         (t["crd"], person, t["title"], addr, label, status, now))
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
