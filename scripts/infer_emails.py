"""Candidate emails for the people at each firm, from the pattern the firm uses.

No filing carries an adviser's email, but firms almost always use one pattern
for everyone. Once Bellwether holds one real person-to-address pair at a firm
(from its website, a vCard, its brochure or a directory), every colleague's
address follows the same shape: if jsmith@firm.com is Jane Smith's, the CEO's
is knowable.

Two kinds of candidate, labelled so nobody mistakes one for the other:

  pattern seen at firm   the firm's own pattern, learned from an address it
                         published. Confidence 60.
  common pattern         no pair observed at this firm; the most common
                         pattern across firms where one was. Confidence 35.

A candidate is never presented as real. The verification job then asks the
firm's mail server about each one; only a server that accepts that exact
mailbox (and is not an accept-all domain) turns a candidate into a verified
address. Domains that publish no mail server get no candidates at all.

Works firm by firm, best-scored first: every person on the firm's IAPD roster
and Schedule A who has no address yet. Each firm is marked when done and
looked at again after 30 days (or sooner once its pattern is learned), so
every slice moves on to new firms instead of re-reading the same ones.

    python -m scripts.infer_emails [--limit FIRMS]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, contacts, db, emailguess, mailcheck, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_guess_state (
    crd        TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    domain     TEXT,
    pattern    TEXT,              -- the firm's own pattern, if one was seen
    made       INTEGER NOT NULL DEFAULT 0
);
"""

RECHECK_DAYS = 30

# Firms due: never looked at, looked at long ago, or looked at before their
# own pattern was known while one is known now.
DUE_SQL = """
    SELECT s.crd FROM firm_scope s
    LEFT JOIN email_guess_state g ON g.crd = s.crd
    WHERE g.crd IS NULL OR g.checked_at < ?
    ORDER BY (g.crd IS NOT NULL), s.priority DESC
    LIMIT ?"""


def people_at(conn, crd: str) -> list[tuple[str, str | None, str]]:
    """(display name, title, person key) for everyone at the firm."""
    out: list[tuple[str, str | None, str]] = []
    try:
        for r in conn.execute("""SELECT p.indvl_pk, p.name FROM person_employment e
                JOIN person p ON p.indvl_pk = e.indvl_pk
                WHERE e.org_pk = ? AND e.kind = 'current'""", (crd,)):
            out.append((r["name"], None, f"i:{r['indvl_pk']}"))
    except Exception:
        conn.rollback()
    for r in conn.execute("SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1",
                          (crd,)):
        full = emailguess.pretty(r["name"])
        out.append((full, r["title"], contacts.name_key(full)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150, help="firms per run")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    contacts.init(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    with runlog.Run(conn, "infer_emails", "infer", cfg.stamp) as run:
        firm_pat, fallback = emailguess.observed(conn)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).isoformat()
        firms = [r["crd"] for r in conn.execute(DUE_SQL, (cutoff, args.limit))]
        # A firm whose own pattern became known since it was last looked at is
        # worth another pass now: its candidates move from 35 to 60.
        learned = [r["crd"] for r in conn.execute(
            "SELECT crd FROM email_guess_state WHERE pattern IS NULL")
                   if r["crd"] in firm_pat][:args.limit]
        conn.commit()
        mx_cache: dict[str, bool | None] = {}
        made = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for crd in list(dict.fromkeys(firms + learned)):
            domain = emailguess.domain_for(conn, crd)
            here = 0
            if domain:
                if domain not in mx_cache:
                    mx_cache[domain] = mailcheck.has_mx(domain)
                if mx_cache[domain] is not False:
                    have = {r["person_key"] for r in conn.execute(
                        "SELECT DISTINCT person_key FROM contact_point WHERE crd=?"
                        " AND kind='email' AND person_key != '' AND source != 'pattern'", (crd,))}
                    observed = crd in firm_pat
                    if observed:
                        # The firm's own pattern replaces earlier guesses made
                        # from the common one, unless a server confirmed them.
                        conn.execute("DELETE FROM contact_point WHERE crd=? AND source='pattern'"
                                     " AND source_ref LIKE 'best-guess%' AND verify_status != 'valid'",
                                     (crd,))
                    cands: list[tuple[str, str | None, str, str, str]] = []
                    seen_keys: set[str] = set()
                    for full, title, key in people_at(conn, crd):
                        if not key or key in have or key in seen_keys:
                            continue
                        seen_keys.add(key)
                        guess = emailguess.best_email(full, crd, firm_pat, fallback, domain)
                        if guess and mailcheck.valid_syntax(guess[0]):
                            cands.append((full, title, key, guess[0], guess[1]))
                    # Two people the pattern gives the same address (three
                    # Michaels and first@) cannot all be right, and nothing
                    # says which one is: such an address is not guessed at all.
                    taken = {r["value"] for r in conn.execute(
                        "SELECT value FROM contact_point WHERE crd=? AND kind='email'"
                        " AND source != 'pattern'", (crd,))}
                    counts: dict[str, int] = {}
                    for c_ in cands:
                        counts[c_[3]] = counts.get(c_[3], 0) + 1
                    for full, title, key, addr, label in cands:
                        if counts[addr] > 1 or addr in taken:
                            continue
                        here += contacts.upsert(
                            conn, crd, "email", addr, "pattern", person_key=key,
                            person_name=full, title=title, source_ref=label,
                            confidence=60 if observed else 35, is_role=False)
                    # Earlier runs did not check this; clear any such pairs.
                    conn.execute("""DELETE FROM contact_point c WHERE c.crd=? AND c.source='pattern'
                        AND c.verify_status != 'valid' AND EXISTS (SELECT 1 FROM contact_point d
                        WHERE d.crd=c.crd AND d.kind='email' AND d.value=c.value AND d.id != c.id)""",
                                 (crd,))
            conn.execute(
                "INSERT INTO email_guess_state (crd, checked_at, domain, pattern, made)"
                " VALUES (?,?,?,?,?) ON CONFLICT (crd) DO UPDATE SET checked_at=excluded.checked_at,"
                " domain=excluded.domain, pattern=excluded.pattern,"
                " made=email_guess_state.made + excluded.made",
                (crd, now, domain, firm_pat.get(crd, (None, None))[1], here))
            conn.commit()
            made += here
        run.rows_out = made
        print(f"generated {made:,} candidate addresses at {len(firms):,} firms")
        run.note(f"made={made} firms={len(firms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
