"""Which email platform each firm runs on: Microsoft 365, Google, or other.

Glynac's marketing compliance module connects to Microsoft 365 only, so the
Glynac score needs to know. Every mail server publishes where its mail goes
(the MX record) and who may send for it (the SPF record); both are public DNS
answers any mail client asks for. No account, no API, no cost.

The domain is the firm's own: an address it printed in its brochure first,
then its filed website, never a social or freemail host. One row per firm,
re-checked after `--max-age` days because firms do migrate.

    python -m scripts.mail_platform [--limit N] [--max-age 90] [--workers 16]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, emailguess, mailcheck, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_mail_platform (
    crd         TEXT PRIMARY KEY,
    domain      TEXT,
    platform    TEXT NOT NULL,    -- m365 | google | other | unknown | none | no_domain
    evidence    TEXT,
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mailplat ON firm_mail_platform (platform);
"""


DNS_RETRY_DAYS = 7      # a domain whose own DNS does not answer is asked again after this


def todo(conn, limit: int, max_age_days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)
              ).isoformat(timespec="seconds")
    week = (datetime.now(timezone.utc) - timedelta(days=DNS_RETRY_DAYS)).isoformat(timespec="seconds")
    rows = conn.execute("""
        SELECT f.crd FROM firm_current f
        LEFT JOIN firm_scope s ON s.crd=f.crd
        LEFT JOIN firm_mail_platform m ON m.crd=f.crd
        LEFT JOIN firm_refresh_request r ON r.crd=f.crd
        WHERE m.crd IS NULL OR m.checked_at < ? OR r.requested_at > m.checked_at
           OR (m.platform='unknown' AND m.evidence LIKE 'DNS for this domain%' AND m.checked_at < ?)
        ORDER BY COALESCE(m.checked_at, '1970-01-01'), s.priority DESC NULLS LAST
        LIMIT ?""", (cutoff, week, limit)).fetchall()
    return [r['crd'] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--max-age", type=int, default=90)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()

    from prospect import jobs
    jobs.init(conn)
    crds = todo(conn, args.limit, args.max_age)
    domains = {crd: emailguess.domain_for(conn, crd) for crd in crds}
    # Firms sharing a domain (affiliated advisers) need one lookup, not several.
    uniq = sorted({d for d in domains.values() if d})
    print(f"{len(crds):,} firms to check, {len(uniq):,} distinct domains")

    answers: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for dom, res in zip(uniq, ex.map(mailcheck.mail_platform, uniq)):
            answers[dom] = res

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    # A resolver outage is not a finding, but a domain whose own DNS never
    # answers is one. Asking about a domain that always resolves tells the two
    # apart; without this, those firms were "retried" on every slice for ever.
    resolver_ok = mailcheck.records("gmail.com", mailcheck.QTYPE_MX) is not None
    with runlog.Run(conn, "mail_platform", "derive", cfg.stamp) as run:
        for crd in crds:
            dom = domains[crd]
            if not dom:
                plat, why = "no_domain", "no usable domain on file"
            else:
                plat, why = answers.get(dom, ("unknown", "not checked"))
            if plat == "unknown" and why == "DNS unreachable":
                if not resolver_ok:
                    continue            # our resolver is down: leave the firm for next time
                why = f"DNS for this domain did not answer; asked again in {DNS_RETRY_DAYS} days"
            counts[plat] = counts.get(plat, 0) + 1
            conn.execute("INSERT INTO firm_mail_platform (crd, domain, platform,"
                         " evidence, checked_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(crd) DO UPDATE SET domain=excluded.domain,"
                         " platform=excluded.platform, evidence=excluded.evidence,"
                         " checked_at=excluded.checked_at",
                         (crd, dom, plat, why, now))
        conn.commit()
        jobs.request_run(conn, 'rescore')
        run.rows_out = sum(counts.values())
        note = "  ".join(f"{k}={v:,}" for k, v in sorted(counts.items()))
        run.note(note)
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
