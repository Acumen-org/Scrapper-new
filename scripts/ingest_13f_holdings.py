"""Fetch and parse 13F information tables for matched advisers.

Four quarters, not one. "Position opened" and "position increased" are stronger
signals than "position held", and neither is computable from a single snapshot: a
firm that bought JEPI last quarter is in market now, one that has held it for
three years is not.

Only auto-accepted and human-confirmed matches are ingested. A review-queue match
is not merged until someone confirms it, because a wrong link puts a holding the
firm does not own into a call opener.

Every filing that parses to zero rows raises rather than recording an empty
result. That guard exists because a namespace bug once made two thirds of
information tables parse to nothing, which reads downstream as "holds none of
these" rather than as a parse failure.

    python -m scripts.ingest_13f_holdings [--limit N]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, net, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS holding_13f (
    cik           TEXT NOT NULL,
    crd           TEXT,
    quarter       TEXT NOT NULL,
    accession     TEXT NOT NULL,
    cusip         TEXT NOT NULL,
    ticker        TEXT,
    issuer        TEXT,
    value_usd     INTEGER,
    shares        INTEGER,
    PRIMARY KEY (accession, cusip)
);
CREATE INDEX IF NOT EXISTS ix_h13f_crd ON holding_13f (crd, ticker);
CREATE INDEX IF NOT EXISTS ix_h13f_q ON holding_13f (quarter);

-- Per-filing outcome, including totals used for the concentration proxy and the
-- cross-check against ADV reported AUM.
CREATE TABLE IF NOT EXISTS filing_13f (
    accession       TEXT PRIMARY KEY,
    cik             TEXT NOT NULL,
    crd             TEXT,
    quarter         TEXT NOT NULL,
    date_filed      TEXT,
    rows_parsed     INTEGER,
    total_value_usd INTEGER,
    largest_pct     REAL,      -- largest single position as % of reported value
    status          TEXT NOT NULL,   -- ok | parse_failed | fetch_failed
    message         TEXT
);
CREATE INDEX IF NOT EXISTS ix_f13f_status ON filing_13f (status);
"""

# Namespaced tags are common (<ns1:infoTable>). Matching without the optional
# prefix silently yields zero rows and looks like a firm holding nothing.
T = r"(?:\w+:)?"
INFO_BLOCK = re.compile(rf"<{T}infoTable\b.*?</{T}infoTable>", re.I | re.S)
FIELD = {
    "issuer": re.compile(rf"<{T}nameOfIssuer>(.*?)</{T}nameOfIssuer>", re.I | re.S),
    "cusip": re.compile(rf"<{T}cusip>(.*?)</{T}cusip>", re.I | re.S),
    "value": re.compile(rf"<{T}value>(.*?)</{T}value>", re.I | re.S),
    "shares": re.compile(rf"<{T}sshPrnamt>(.*?)</{T}sshPrnamt>", re.I | re.S),
}


def parse_info_table(body: str) -> list[dict]:
    rows = []
    for block in INFO_BLOCK.findall(body):
        get = lambda k: (FIELD[k].search(block).group(1).strip()  # noqa: E731
                         if FIELD[k].search(block) else "")
        cusip = get("cusip").upper()
        if len(cusip) != 9:
            continue
        def num(s):
            s = re.sub(r"[^0-9]", "", s or "")
            return int(s) if s else None
        rows.append({"cusip": cusip, "issuer": " ".join(get("issuer").split()),
                     "value": num(get("value")), "shares": num(get("shares"))})
    return rows


def document_urls(cik: str, accession: str) -> list[str]:
    """The full submission text always exists; it embeds the information table."""
    acc = accession.replace("-", "")
    return [f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}.txt",
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}.txt"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = config.load()
    sec = yaml.safe_load(
        (config.CONFIG_DIR / "target_securities.yml").read_text(encoding="utf-8"))
    # str() defensively: YAML turns an all-digit CUSIP into an int, and the
    # resulting lookup silently never matches a parsed (string) CUSIP.
    targets = {str(v["cusip"]).strip(): t for t, v in sec["securities"].items()
               if v.get("verified") and v.get("cusip")}
    print(f"target CUSIPs in play: {len(targets)} "
          f"({sum(1 for t,v in sec['securities'].items() if not v.get('verified'))} excluded as unverified)")

    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    fetch = net.Fetcher(cfg.http)

    todo = conn.execute("""
        SELECT DISTINCT e.cik, e.accession, e.quarter, e.date_filed, m.crd
        FROM edgar_13f_filer e
        JOIN adv_13f_match m ON m.cik = e.cik
        WHERE m.status IN ('auto','confirmed')
          AND e.accession IS NOT NULL
          AND e.accession NOT IN (SELECT accession FROM filing_13f)
        ORDER BY e.quarter DESC""").fetchall()
    if args.limit:
        todo = todo[:args.limit]
    print(f"filings to ingest: {len(todo):,}")

    ok = parse_fail = fetch_fail = hits = 0
    with runlog.Run(conn, "holding_13f", "ingest", cfg.stamp) as run:
        for i, f in enumerate(todo, 1):
            body = None
            for url in document_urls(f["cik"], f["accession"]):
                try:
                    resp = fetch._request(url, stream=False)
                    body = resp.text
                    resp.close()
                    break
                except Exception:
                    continue
            if body is None:
                fetch_fail += 1
                conn.execute("INSERT OR REPLACE INTO filing_13f VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f["accession"], f["cik"], f["crd"], f["quarter"],
                              f["date_filed"], 0, None, None, "fetch_failed",
                              "all document URLs failed"))
                continue

            rows = parse_info_table(body)
            try:
                guard.require_rows(len(rows), f"13F {f['accession']}",
                                   "information table present but no rows recognised")
            except guard.EmptyParse as exc:
                parse_fail += 1
                conn.execute("INSERT OR REPLACE INTO filing_13f VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f["accession"], f["cik"], f["crd"], f["quarter"],
                              f["date_filed"], 0, None, None, "parse_failed",
                              str(exc)[:300]))
                conn.commit()
                continue

            total = sum(r["value"] or 0 for r in rows)
            largest = (max((r["value"] or 0) for r in rows) / total * 100) if total else None
            conn.execute("INSERT OR REPLACE INTO filing_13f VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (f["accession"], f["cik"], f["crd"], f["quarter"],
                          f["date_filed"], len(rows), total, largest, "ok", None))
            keep = [r for r in rows if r["cusip"] in targets]
            if keep:
                conn.executemany(
                    "INSERT OR REPLACE INTO holding_13f VALUES (?,?,?,?,?,?,?,?,?)",
                    [(f["cik"], f["crd"], f["quarter"], f["accession"], r["cusip"],
                      targets[r["cusip"]], r["issuer"], r["value"], r["shares"])
                     for r in keep])
                hits += len(keep)
            ok += 1
            if i % 50 == 0:
                conn.commit()
                print(f"  {i:,}/{len(todo):,}  ok={ok} parse_fail={parse_fail} "
                      f"fetch_fail={fetch_fail} target_hits={hits}")
        conn.commit()
        run.rows_out = ok
        rate = parse_fail / max(ok + parse_fail, 1) * 100
        run.note(f"ok={ok} parse_failed={parse_fail} fetch_failed={fetch_fail} "
                 f"parse_failure_rate={rate:.1f}% target_hits={hits}")
        if rate > 5:
            run.flag(f"13F parse failure rate {rate:.1f}% exceeds 5%")

    print(f"\nok {ok:,} | parse failed {parse_fail:,} | fetch failed {fetch_fail:,} "
          f"| target holdings {hits:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
