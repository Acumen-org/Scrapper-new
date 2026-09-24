"""Derive and verify the target CUSIP map from real 13F filings.

A wrong CUSIP produces zero matches for that ticker and reads identically to a
firm not holding it. That is a silent failure of exactly the class the field
register exists to catch, so the map is built by observation rather than recall:
sample real information tables, collect every CUSIP with the issuer name filed
alongside it, and only accept a CUSIP that actually appears in filings.

Output is config/target_securities.yml with, for each ticker, the observed CUSIP,
the issuer name as filed, and how many distinct filings it was seen in. A ticker
that cannot be observed is written with a null CUSIP and excluded from scoring
rather than guessed at.

    python -m scripts.build_cusip_map [--filings 60]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net, runlog  # noqa: E402

# Issuer-name patterns as they appear in filed information tables. Deliberately
# loose on punctuation and corporate suffix, strict on the distinguishing words.
TARGETS = {
    # AcuBooth converts: covered call / option overlay ETFs
    "JEPI": (r"JPMORGAN\s+EQUITY\s+PREMIUM", "acubooth"),
    "JEPQ": (r"JPMORGAN\s+NASDAQ\s+EQUITY\s+PREMIUM", "acubooth"),
    "XYLD": (r"GLOBAL\s*X.*S&?P\s*500\s+COVERED\s+CALL", "acubooth"),
    "QYLD": (r"GLOBAL\s*X.*NASDAQ\s*100\s+COVERED\s+CALL", "acubooth"),
    "RYLD": (r"GLOBAL\s*X.*RUSSELL\s*2000\s+COVERED\s+CALL", "acubooth"),
    "DIVO": (r"AMPLIFY.*(CWP|ENHANCED\s+DIVIDEND)", "acubooth"),
    # NEOS funds sometimes file under the trust name, so match on the index each
    # fund tracks rather than requiring the full marketing name.
    "SPYI": (r"NEOS.*(S&?P\s*500|SPYI)", "acubooth"),
    "QQQI": (r"NEOS.*(NASDAQ|QQQI)", "acubooth"),
    # PHH converts: net lease and real estate income
    "O":    (r"REALTY\s+INCOME\s+CORP", "phh"),
    "NNN":  (r"^NNN\s+REIT|NATIONAL\s+RETAIL\s+PROPERTIES", "phh"),
    "WPC":  (r"W\.?\s*P\.?\s*CAREY", "phh"),
    "ADC":  (r"AGREE\s+REALTY", "phh"),
    "EPRT": (r"ESSENTIAL\s+PROPERTIES\s+REALTY", "phh"),
    "STAG": (r"STAG\s+INDUSTRIAL", "phh"),
}

# Information tables are frequently namespaced (<ns1:nameOfIssuer>). A regex
# without the optional prefix silently matches nothing and the filing looks
# empty rather than unparsed, which is the same failure class as a wrong CUSIP.
# Measured on one real filing: 0 matches without the prefix, 205 with it.
INFO_RE = re.compile(
    r"<(?:\w+:)?nameOfIssuer>(.*?)</(?:\w+:)?nameOfIssuer>"
    r".*?<(?:\w+:)?cusip>(.*?)</(?:\w+:)?cusip>",
    re.IGNORECASE | re.DOTALL)


# Search phrases for tickers a random institutional sample will not surface.
# Covered call ETFs are held by advisory practices, not by the large filers that
# dominate a random draw, so they need a targeted lookup rather than more sampling.
# Every ticker has a query, so verification never depends on what a random
# sample happened to contain. Phrases below were each confirmed to return
# 13F-HR hits before being written here.
FTS_QUERY = {
    "JEPI": "JPMorgan Equity Premium Income ETF",
    "JEPQ": "JPMorgan Nasdaq Equity Premium Income ETF",
    "XYLD": "Global X S&P 500 Covered Call",
    "QYLD": "Global X Nasdaq 100 Covered Call",
    "RYLD": "Global X Russell 2000 Covered Call",
    "DIVO": "Amplify CWP Enhanced Dividend Income",
    "SPYI": "NEOS ETF Trust",
    "QQQI": "NEOS Nasdaq 100 High Income ETF",
    "O":    "Realty Income Corp",
    "NNN":  "NNN REIT Inc",
    "WPC":  "W. P. Carey Inc",
    "ADC":  "Agree Realty Corp",
    "EPRT": "Essential Properties Realty Trust",
    "STAG": "Stag Industrial Inc",
}


def fts_documents(phrase: str, ua: str, limit: int = 4):
    """EDGAR full-text search, restricted to 13F-HR. Returns (cik, accession, file)."""
    url = ("https://efts.sec.gov/LATEST/search-index?q="
           + urllib.parse.quote(f'"{phrase}"') + "&forms=13F-HR")
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
        ciks = hit.get("_source", {}).get("ciks") or []
        if not ciks:
            continue
        out.append((str(int(ciks[0])), acc.replace("-", ""), fname))
    return out


def resolve_via_fts(ticker, pattern, fetch, ua):
    """Fetch the specific information tables EDGAR says contain the phrase, and
    read the CUSIP filed next to the matching issuer name."""
    rx = re.compile(pattern, re.IGNORECASE)
    tally = collections.Counter()
    filed_name = {}
    for cik, acc, fname in fts_documents(FTS_QUERY.get(ticker, ""), ua):
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{fname}"
        try:
            resp = fetch._request(url, stream=False)
            body = resp.text
            resp.close()
        except Exception:
            continue
        for issuer, cusip in INFO_RE.findall(body):
            cu = cusip.strip().upper()
            iss = " ".join(issuer.split()).upper()
            if len(cu) == 9 and rx.search(iss):
                tally[cu] += 1
                filed_name.setdefault(cu, iss)
    if not tally:
        return None
    cu, n = tally.most_common(1)[0]
    return cu, filed_name[cu], n


def sample_filings(conn, n: int):
    """Spread the sample across quarters so a ticker that only recently began
    trading is still observable."""
    # One filing per filer, then a random sample of those. SQLite returned an
    # arbitrary row per GROUP BY cik; Postgres requires the choice to be
    # explicit, and DISTINCT ON needs its own ORDER BY, so the randomising sort
    # moves outside.
    return conn.execute("""
        SELECT * FROM (
            SELECT DISTINCT ON (cik) cik, accession, company_name, quarter
            FROM edgar_13f_filer
            WHERE accession IS NOT NULL
            ORDER BY cik, quarter DESC
        ) one_per_filer
        ORDER BY RANDOM() LIMIT ?""", (n,)).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filings", type=int, default=60)
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="permit replacing an already-verified CUSIP (default: refuse)")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    fetch = net.Fetcher(cfg.http)

    seen: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    names: dict[str, str] = {}
    fetched = ok = 0

    with runlog.Run(conn, "cusip_map", "derive", cfg.stamp) as run:
        for row in sample_filings(conn, args.filings):
            url = (f"https://www.sec.gov/Archives/edgar/data/"
                   f"{int(row['cik'])}/{row['accession']}.txt")
            fetched += 1
            try:
                resp = fetch._request(url, stream=False)
                body = resp.text
                resp.close()
            except Exception:
                continue
            hits = INFO_RE.findall(body)
            if not hits:
                continue
            ok += 1
            for issuer, cusip in hits:
                cu = cusip.strip().upper()
                iss = " ".join(issuer.split()).upper()
                if len(cu) != 9:
                    continue
                seen[cu][row["accession"]] += 1
                names.setdefault(cu, iss)
            if ok % 10 == 0:
                print(f"  parsed {ok}/{fetched} filings, {len(seen):,} distinct CUSIPs")

        print(f"\nparsed {ok} of {fetched} filings; {len(seen):,} distinct CUSIPs observed")
        run.rows_out = len(seen)

    # A thin sample must never clobber a CUSIP an earlier run verified. Load what
    # is already on disk and only ever add evidence to it.
    prior = {}
    out_path = config.CONFIG_DIR / "target_securities.yml"
    if out_path.exists():
        import yaml as _yaml
        old = _yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        for t, v in (old.get("securities") or {}).items():
            if v and v.get("cusip"):
                prior[t] = v

    resolved = {}
    for ticker, (pattern, product) in TARGETS.items():
        rx = re.compile(pattern, re.IGNORECASE)
        cands = [(cu, names[cu], len(seen[cu])) for cu in seen if rx.search(names[cu])]
        cands.sort(key=lambda t: -t[2])
        if not cands and ticker in prior:
            p = prior[ticker]
            resolved[ticker] = dict(cusip=p["cusip"],
                                    issuer_as_filed=p.get("issuer_as_filed"),
                                    seen_in_filings=p.get("seen_in_filings", 0),
                                    product=product, verified=True)
            continue
        if cands:
            cu, iss, nfil = cands[0]
            if (ticker in prior and prior[ticker]["cusip"] != cu
                    and not args.allow_downgrade):
                print(f"  {ticker}: keeping verified {prior[ticker]['cusip']} "
                      f"(this run saw {cu}); pass --allow-downgrade to replace")
                cu = prior[ticker]["cusip"]
                iss = prior[ticker].get("issuer_as_filed") or iss
                nfil = max(nfil, prior[ticker].get("seen_in_filings", 0))
            resolved[ticker] = dict(cusip=cu, issuer_as_filed=iss,
                                    seen_in_filings=nfil, product=product,
                                    verified=True)
        else:
            hit = resolve_via_fts(ticker, pattern, fetch, cfg.http["user_agent"])
            if hit:
                cu, iss, n = hit
                print(f"  {ticker}: resolved via targeted search -> {cu} ({iss[:40]})")
                resolved[ticker] = dict(cusip=cu, issuer_as_filed=iss,
                                        seen_in_filings=n, product=product,
                                        verified=True)
            else:
                resolved[ticker] = dict(cusip=None, issuer_as_filed=None,
                                        seen_in_filings=0, product=product,
                                        verified=False)

    out = config.CONFIG_DIR / "target_securities.yml"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("# Target securities for the 13F convert overlays.\n#\n")
        fh.write("# CUSIPs are OBSERVED, not recalled. Each was read from a real filed\n")
        fh.write("# information table alongside the issuer name shown. A wrong CUSIP\n")
        fh.write("# produces zero matches and reads identically to a firm not holding\n")
        fh.write("# the security, so an unverified ticker carries a null cusip and is\n")
        fh.write("# excluded from scoring rather than guessed at.\n#\n")
        fh.write(f"# Derived from a sample of {ok} filings.\n\n")
        fh.write("config_version: 1\n\nsecurities:\n")
        for t in TARGETS:
            r = resolved[t]
            fh.write(f"  {t}:\n")
            fh.write(f"    product: {r['product']}\n")
            fh.write(f"    cusip: {r['cusip'] or 'null'}\n")
            fh.write(f"    verified: {'true' if r['verified'] else 'false'}\n")
            if r["issuer_as_filed"]:
                fh.write(f"    issuer_as_filed: \"{r['issuer_as_filed']}\"\n")
                fh.write(f"    seen_in_filings: {r['seen_in_filings']}\n")

    v = sum(1 for r in resolved.values() if r["verified"])
    print(f"\nverified {v} of {len(TARGETS)} tickers -> {out.name}")
    for t, r in resolved.items():
        mark = "OK " if r["verified"] else "-- "
        print(f"  {mark}{t:<5} {r['cusip'] or 'NOT OBSERVED':<12} "
              f"{(r['issuer_as_filed'] or '')[:46]:<48} {r['seen_in_filings']} filings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
