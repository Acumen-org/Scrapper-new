"""Match 13F filers (CIK) to advisers (CRD), with confidence.

There is no official crosswalk. Matching is on normalised firm name plus state,
and nothing below the auto-accept threshold is ever merged silently: uncertain
matches go to the review queue for a human to confirm or reject.

The asymmetry that sets the thresholds: a wrong link puts a holding the firm does
not own into a call opener, which costs credibility. A missed link costs one
prospect a data point. So the bar for auto-accept is high and the bar for
discarding outright is low.

    python -m scripts.match_13f [--scope tier_a|tier_c|all] [--limit N]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS adv_13f_match (
    crd             TEXT NOT NULL,
    cik             TEXT NOT NULL,
    confidence      REAL NOT NULL,
    state_adv       TEXT,
    state_edgar     TEXT,
    name_adv        TEXT,
    name_edgar      TEXT,
    match_basis     TEXT NOT NULL,     -- how the names matched
    status          TEXT NOT NULL,     -- auto | review | rejected | confirmed | denied
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    config_stamp    TEXT NOT NULL,
    PRIMARY KEY (crd, cik)
);
CREATE INDEX IF NOT EXISTS ix_m13f_status ON adv_13f_match (status, confidence DESC);
CREATE INDEX IF NOT EXISTS ix_m13f_crd ON adv_13f_match (crd);

CREATE TABLE IF NOT EXISTS edgar_filer_meta (
    cik             TEXT PRIMARY KEY,
    name            TEXT,
    state           TEXT,
    former_names    TEXT,
    fetched_at      TEXT
);
"""

LEGAL = {"LLC", "LLP", "LP", "INC", "INCORPORATED", "LTD", "CO", "CORP",
         "CORPORATION", "COMPANY", "PLC", "SA", "AG", "NA", "THE", "PC", "PLLC"}
GENERIC = LEGAL | {"TRUST", "GROUP", "PARTNERS", "PARTNERSHIP", "ADVISORS",
                   "ADVISERS", "MANAGEMENT", "CAPITAL", "ASSOCIATES", "HOLDINGS"}
_DROP = re.compile(r"[.'’,]")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def norm(raw: str | None, strip: set) -> str:
    if not raw:
        return ""
    p = _WS.sub(" ", _PUNCT.sub(" ", _DROP.sub("", raw.upper()))).strip().split(" ")
    while len(p) > 1 and p[-1] in strip:
        p.pop()
    if len(p) > 1 and p[0] == "THE":
        p.pop(0)
    return " ".join(p)


def fetch_meta(fetch: net.Fetcher, cik: str, ua: str) -> dict | None:
    """EDGAR submissions record: current name, business state, former names."""
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "application/json"})
    try:
        fetch._throttle()
        data = json.loads(urllib.request.urlopen(req, timeout=45).read())
    except Exception:
        return None
    addr = (data.get("addresses") or {}).get("business") or {}
    former = [f.get("name") for f in (data.get("formerNames") or []) if f.get("name")]
    return {"name": data.get("name"), "state": addr.get("stateOrCountry"),
            "former": former}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="all", choices=["tier_a", "tier_c", "all"])
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = config.load()
    sc = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
    th = sc.get("match_13f", {"auto_accept": 0.85, "review_floor": 0.50})
    stamp = cfg.stamp
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    fetch = net.Fetcher(cfg.http)
    ua = cfg.http["user_agent"]

    # Candidate advisers
    if args.scope == "tier_a":
        where = "t.crd IN (SELECT crd FROM tier_a_rank)"
    elif args.scope == "tier_c":
        where = "t.crd IN (SELECT crd FROM tier_c_score WHERE total_score>=70)"
    else:
        where = ("t.crd IN (SELECT crd FROM tier_a_rank) OR "
                 "t.crd IN (SELECT crd FROM tier_c_score WHERE total_score>=70)")
    advisers = conn.execute(f"""
        SELECT f.crd, f.legal_name, f.business_name, f.state
        FROM firm_current f JOIN (SELECT crd FROM tier_a_rank
                          UNION SELECT crd FROM tier_c_score WHERE total_score>=70) t
          ON t.crd=f.crd
        WHERE {where}""").fetchall()
    print(f"candidate advisers in scope: {len(advisers):,}")

    # Index EDGAR filers by both normalisations
    by_cons: dict[str, list] = collections.defaultdict(list)
    by_aggr: dict[str, list] = collections.defaultdict(list)
    for r in conn.execute("SELECT DISTINCT cik, company_name FROM edgar_13f_filer"):
        by_cons[norm(r["company_name"], LEGAL)].append(r)
        by_aggr[norm(r["company_name"], GENERIC)].append(r)
    by_cons.pop("", None); by_aggr.pop("", None)

    # Pair advisers to filers on name, then verify with state
    pairs = []
    for a in advisers:
        for nm in (a["legal_name"], a["business_name"]):
            key_c, key_a = norm(nm, LEGAL), norm(nm, GENERIC)
            if key_c in by_cons:
                for f in by_cons[key_c]:
                    pairs.append((a, f, "exact_legal_suffix_stripped"))
                break
            if key_a in by_aggr:
                for f in by_aggr[key_a]:
                    pairs.append((a, f, "generic_tail_stripped"))
                break
    seen = set(); uniq = []
    for a, f, basis in pairs:
        k = (a["crd"], f["cik"])
        if k not in seen:
            seen.add(k); uniq.append((a, f, basis))
    print(f"name-level candidate pairs: {len(uniq):,}")
    if args.limit:
        uniq = uniq[:args.limit]

    # Ambiguity: a CIK claimed by several CRDs, or vice versa, is weaker evidence
    crd_per_cik = collections.Counter(f["cik"] for _, f, _ in uniq)
    cik_per_crd = collections.Counter(a["crd"] for a, _, _ in uniq)

    have = {r["cik"] for r in conn.execute("SELECT cik FROM edgar_filer_meta")}
    need = [f["cik"] for _, f, _ in uniq if f["cik"] not in have]
    print(f"fetching EDGAR metadata for {len(set(need)):,} CIKs ...")
    got = 0
    for cik in sorted(set(need)):
        m = fetch_meta(fetch, cik, ua)
        if m is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO edgar_filer_meta VALUES (?,?,?,?,datetime('now'))",
            (cik, m["name"], m["state"], json.dumps(m["former"])))
        got += 1
        if got % 200 == 0:
            conn.commit(); print(f"  {got:,}/{len(set(need)):,}")
    conn.commit()
    meta = {r["cik"]: r for r in conn.execute("SELECT * FROM edgar_filer_meta")}

    out = []
    with runlog.Run(conn, "adv_13f_match", "match", stamp) as run:
        for a, f, basis in uniq:
            m = meta.get(f["cik"])
            st_e = (m["state"] if m else None) or None
            st_a = a["state"]
            exact = basis == "exact_legal_suffix_stripped"
            if st_a and st_e:
                agree = (st_a == st_e)
                conf = (0.95 if exact else 0.72) if agree else (0.45 if exact else 0.25)
                basis += "+state_match" if agree else "+state_conflict"
            else:
                conf = 0.78 if exact else 0.55
                basis += "+state_unknown"
            # Ambiguity penalty: one filer claimed by many advisers is weak evidence
            if crd_per_cik[f["cik"]] > 1 or cik_per_crd[a["crd"]] > 1:
                conf -= 0.20
                basis += "+ambiguous"
            conf = round(max(0.0, min(1.0, conf)), 3)
            status = ("auto" if conf >= th["auto_accept"]
                      else "review" if conf >= th["review_floor"] else "rejected")
            out.append((a["crd"], f["cik"], conf, st_a, st_e, a["legal_name"],
                        f["company_name"], basis, status, None, None, stamp))
        conn.executemany(
            "INSERT OR REPLACE INTO adv_13f_match VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
        conn.commit()
        run.rows_out = len(out)

    print("\nmatch outcomes:")
    for r in conn.execute("""SELECT status, COUNT(*) n, ROUND(AVG(confidence),3) c
                             FROM adv_13f_match GROUP BY 1 ORDER BY n DESC"""):
        print(f"  {r['status']:<10} {r['n']:>5,}  mean confidence {r['c']}")
    print("\nby basis:")
    for r in conn.execute("""SELECT match_basis, COUNT(*) n FROM adv_13f_match
                             GROUP BY 1 ORDER BY n DESC LIMIT 8"""):
        print(f"  {r['n']:>5,}  {r['match_basis']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
