"""Ingest Form D filings for funds we track, and test the Owners field.

Two jobs in one pass:

1. Owners verification. Form D reports "total number of investors who already
   have invested" for the same funds Schedule D 7B1 reports Owners for. This is
   the external second source for the last load-bearing entry in the field
   register. Timing differs (Form D is filed at the offering and amended
   irregularly; ADV is amended annually), so the test is correlation and
   plausibility, not equality.

2. Competitor intel seed. Form Ds for the sponsor-segment funds (including the
   three zero-investor firms) show raise progress, minimums, and industry
   classification, which is the competitive tracking the plan wants.

Matching is by fund name through EDGAR full-text search, restricted to Form D,
and a candidate is accepted only when the normalised issuer name agrees with the
normalised fund name. Everything is stored with provenance either way.

    python -m scripts.ingest_form_d [--max-funds N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS form_d (
    accession        TEXT PRIMARY KEY,
    cik              TEXT,
    issuer_name      TEXT,
    date_filed       TEXT,
    form_type        TEXT,             -- D or D/A
    investors        INTEGER,          -- totalNumberAlreadyInvested
    total_sold       INTEGER,
    total_offering   INTEGER,
    min_investment   INTEGER,
    industry         TEXT,
    first_sale_date  TEXT,
    fund_id          TEXT,             -- matched 7B1 fund, when confidently matched
    crd              TEXT,
    match_quality    TEXT,             -- exact | prefix | none
    query_fund_name  TEXT
);
CREATE INDEX IF NOT EXISTS ix_fd_crd ON form_d (crd);
CREATE INDEX IF NOT EXISTS ix_fd_fund ON form_d (fund_id);
"""

_LEGAL = {"LLC", "LP", "LLP", "LTD", "INC", "CO", "CORP", "COMPANY", "THE",
          "L L C", "L P", "FUND", "PARTNERS", "PARTNERSHIP", "TRUST"}
_DROP = re.compile(r"[.'’,]")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def norm(raw: str | None) -> str:
    if not raw:
        return ""
    p = _WS.sub(" ", _PUNCT.sub(" ", _DROP.sub("", raw.upper()))).strip().split(" ")
    while len(p) > 1 and p[-1] in _LEGAL:
        p.pop()
    if len(p) > 1 and p[0] == "THE":
        p.pop(0)
    return " ".join(p)


# Form D primary_doc.xml fields; optional namespace prefix, same lesson as 13F.
T = r"(?:\w+:)?"
FD = {
    "issuer": re.compile(rf"<{T}entityName>(.*?)</{T}entityName>", re.I | re.S),
    "investors": re.compile(
        rf"<{T}totalNumberAlreadyInvested>(.*?)</{T}totalNumberAlreadyInvested>", re.I | re.S),
    "sold": re.compile(rf"<{T}totalAmountSold>(.*?)</{T}totalAmountSold>", re.I | re.S),
    "offering": re.compile(
        rf"<{T}totalOfferingAmount>(.*?)</{T}totalOfferingAmount>", re.I | re.S),
    "minimum": re.compile(
        rf"<{T}minimumInvestmentAccepted>(.*?)</{T}minimumInvestmentAccepted>", re.I | re.S),
    "industry": re.compile(
        rf"<{T}industryGroupType>(.*?)</{T}industryGroupType>", re.I | re.S),
    "first_sale": re.compile(
        rf"<{T}dateOfFirstSale>\s*<{T}value>(.*?)</{T}value>", re.I | re.S),
}


def _num(s: str | None) -> int | None:
    s = re.sub(r"[^0-9]", "", s or "")
    return int(s) if s else None


def fts_form_d(phrase: str, ua: str, limit: int = 3):
    url = ("https://efts.sec.gov/LATEST/search-index?q="
           + urllib.parse.quote(f'"{phrase}"') + "&forms=D")
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=45).read())
    except Exception:
        return []
    out = []
    for hit in data.get("hits", {}).get("hits", [])[:limit]:
        ident = hit.get("_id", "")
        if ":" not in ident:
            continue
        acc, fname = ident.split(":", 1)
        src = hit.get("_source", {})
        ciks = src.get("ciks") or []
        if not ciks:
            continue
        out.append({"cik": str(int(ciks[0])), "accession": acc,
                    "fname": fname, "date": src.get("file_date"),
                    "form": src.get("file_type") or "D"})
    return out


def candidate_funds(conn, max_funds: int):
    """RE-segment firms' real estate funds first (the Owners test set the user
    specified), then the three named sponsor funds by CRD."""
    rows = conn.execute("""
        WITH latest AS (SELECT s.crd AS crd, MAX(fc.filing_date) d FROM sched_d_7b1 s
          JOIN filing_crd fc ON fc.filing_id=s.filing_id
          WHERE s.fund_type='Real Estate Fund' GROUP BY s.crd)
        SELECT DISTINCT s.crd, s.fund_id, s.fund_name, s.owners,
               s.minimum_investment
        FROM sched_d_7b1 s
        JOIN filing_crd fc ON fc.filing_id=s.filing_id
        JOIN latest l ON l.crd=s.crd AND l.d=fc.filing_date
        WHERE s.fund_type='Real Estate Fund'
          AND s.crd IN (SELECT crd FROM re_segment)
          AND s.fund_name IS NOT NULL AND LENGTH(s.fund_name) >= 10
        ORDER BY s.gross_asset_value DESC""").fetchall()
    return rows[:max_funds]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-funds", type=int, default=220)
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    fetch = net.Fetcher(cfg.http)
    ua = cfg.http["user_agent"]

    funds = candidate_funds(conn, args.max_funds)
    done = {r["fund_id"] for r in conn.execute(
        "SELECT DISTINCT fund_id FROM form_d WHERE fund_id IS NOT NULL")}
    funds = [f for f in funds if f["fund_id"] not in done]
    print(f"fund names to search: {len(funds)} (skipping {len(done)} already matched)")

    searched = matched = stored = 0
    with runlog.Run(conn, "form_d", "ingest", cfg.stamp) as run:
        for fd in funds:
            qname = fd["fund_name"]
            searched += 1
            hits = fts_form_d(qname, ua)
            best = None
            for h in hits:
                url = (f"https://www.sec.gov/Archives/edgar/data/{h['cik']}/"
                       f"{h['accession'].replace('-', '')}/{h['fname']}")
                try:
                    resp = fetch._request(url, stream=False)
                    body = resp.text
                    resp.close()
                except Exception:
                    continue
                m = FD["issuer"].search(body)
                issuer = " ".join(m.group(1).split()) if m else ""
                nq, ni = norm(qname), norm(issuer)
                if not ni:
                    continue
                if nq == ni:
                    quality = "exact"
                elif ni.startswith(nq) or nq.startswith(ni):
                    quality = "prefix"
                else:
                    continue
                def field(key: str) -> str | None:
                    m2 = FD[key].search(body)
                    return m2.group(1).strip() if m2 else None

                rec = {
                    "accession": h["accession"], "cik": h["cik"], "issuer": issuer,
                    "date": h["date"], "form": h["form"],
                    "investors": _num(field("investors")),
                    "sold": _num(field("sold")),
                    "offering": _num(field("offering")),
                    "minimum": _num(field("minimum")),
                    "industry": field("industry"),
                    "first_sale": field("first_sale"),
                    "quality": quality,
                }
                # prefer exact match, then most recent filing (D/A carries the
                # freshest investor count)
                key = (quality == "exact", rec["date"] or "")
                if best is None or key > best[0]:
                    best = (key, rec)
            if best is None:
                continue
            r = best[1]
            matched += 1
            conn.execute(
                "INSERT OR REPLACE INTO form_d VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["accession"], r["cik"], r["issuer"], r["date"], r["form"],
                 r["investors"], r["sold"], r["offering"], r["minimum"],
                 r["industry"], r["first_sale"], fd["fund_id"], fd["crd"],
                 r["quality"], qname))
            stored += 1
            if stored % 20 == 0:
                conn.commit()
                print(f"  searched {searched}, matched {matched}")
        conn.commit()
        run.rows_out = stored
        run.note(f"searched={searched} matched={matched}")

    print(f"\nsearched {searched} fund names, matched {matched} Form D filings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
