"""Generate internal candidates, one untried pattern per person after rejection.

Only verified addresses become usable contacts. Pending and inconclusive checks
wait for verification; confirmed rejection advances to the next known pattern.
All firms with websites are eligible, with a seven-day discovery revisit.
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


def generate_for_firm(conn, crd, firm_pat, fallback):
    domain = emailguess.domain_for(conn, crd)
    conn.commit()
    if not domain or mailcheck.has_mx(domain) is False:
        return 0
    records = conn.execute("SELECT person_key, value, verify_status FROM contact_point WHERE crd=? AND kind='email'", (crd,)).fetchall()
    by_person = {}
    owners = {}
    for row in records:
        by_person.setdefault(row['person_key'], []).append(row)
        owners.setdefault(row['value'], set()).add(row['person_key'])
    preferred = firm_pat.get(crd, (domain, fallback))[1]
    pending = {'valid', 'unverified', 'queued', 'unknown', 'catch_all', 'risky', 'no_mail_server'}
    candidates = []
    seen = set()
    for full, title, key in people_at(conn, crd):
        if not key or key in seen:
            continue
        seen.add(key)
        prior = by_person.get(key, [])
        if any(row['verify_status'] in pending for row in prior):
            continue
        tried = {row['value'] for row in prior}
        tried.update(address for address, people in owners.items() if people - {key, ''})
        guess = emailguess.next_candidate(full, domain, preferred, tried)
        if guess and mailcheck.valid_syntax(guess[0]):
            candidates.append((full, title, key, *guess))
    counts = {}
    for row in candidates:
        counts[row[3]] = counts.get(row[3], 0) + 1
    made = 0
    for full, title, key, address, pattern in candidates:
        if counts[address] != 1:
            continue
        made += contacts.upsert(conn, crd, 'email', address, 'pattern', person_key=key,
            person_name=full, title=title, source_ref=pattern, confidence=35, is_role=False)
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=150)
    ap.add_argument('--crd')
    args = ap.parse_args()
    conn = db.connect()
    contacts.init(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    from prospect import jobs
    jobs.init(conn)
    firm_pat, fallback = emailguess.observed(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    firms = ([args.crd] if args.crd else [r['crd'] for r in conn.execute("""
        SELECT f.crd FROM firm_current f
        LEFT JOIN firm_scope s ON s.crd=f.crd
        LEFT JOIN email_guess_state g ON g.crd=f.crd
        LEFT JOIN firm_refresh_request r ON r.crd=f.crd
        WHERE f.website IS NOT NULL AND f.website != '' AND
          (g.crd IS NULL OR g.checked_at < ? OR r.requested_at > g.checked_at OR EXISTS
           (SELECT 1 FROM contact_point cp WHERE cp.crd=f.crd AND cp.kind='email'
            AND cp.verify_status='invalid' AND cp.verified_at > g.checked_at))
        ORDER BY g.checked_at ASC NULLS FIRST, s.priority DESC NULLS LAST LIMIT ?
        """, (cutoff, args.limit))])
    conn.commit()
    made = 0
    for crd in firms:
        n = generate_for_firm(conn, crd, firm_pat, fallback)
        conn.execute("""INSERT INTO email_guess_state (crd,checked_at,domain,pattern,made)
            VALUES (?,?,?,?,?) ON CONFLICT(crd) DO UPDATE SET checked_at=excluded.checked_at,
            domain=excluded.domain, pattern=excluded.pattern, made=email_guess_state.made+excluded.made""",
            (crd,datetime.now(timezone.utc).isoformat(),emailguess.domain_for(conn,crd),firm_pat.get(crd,(None,None))[1],n))
        conn.commit()
        made += n
    if made:
        jobs.request_run(conn, 'email_verify')
    conn.close()
    print(f'{made} internal candidates queued for verification across {len(firms)} firms')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
