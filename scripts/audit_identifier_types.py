"""Audit every digit-string identifier at every format boundary.

The CUSIP bug was undetectable by design: every filing parsed, every row
validated, every guard passed, and the output was plausible. YAML silently turned
an all-digit CUSIP into an integer and the lookup could never match. ADC and DIVO
survived only because leading zeros forced YAML to keep them as strings, which
means the system was one formatting accident away from looking correct while
missing the second most held security in the dataset.

Any identifier that is a string of digits is at risk wherever it crosses a YAML,
JSON or CSV boundary. This project keys on several: CRD, CIK, SEC file number,
Filing ID, CUSIP, accession, fund ID. This audit checks all of them:

  1. every identifier column is declared TEXT, not INTEGER
  2. no stored value is actually of integer type (SQLite honours what you insert)
  3. no identifier in a config file is unquoted and all-digit
  4. cross-table joins compare like types

    python -m scripts.audit_identifier_types
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db  # noqa: E402

# table -> identifier columns that must behave as opaque strings
IDENTIFIER_COLUMNS = {
    "firm": ["crd", "sec_number"],
    "filing_crd": ["filing_id", "crd"],
    "sched_d_5k3": ["filing_id", "crd"],
    "sched_d_7b1": ["filing_id", "crd", "fund_id"],
    "tier_a_rank": ["crd"],
    "tier_c_score": ["crd"],
    "re_segment": ["crd"],
    "firm_custodian_profile": ["crd"],
    "trigger_event": ["crd"],
    "edgar_13f_filer": ["cik", "accession"],
    "edgar_filer_meta": ["cik"],
    "adv_13f_match": ["crd", "cik"],
    "holding_13f": ["cik", "crd", "accession", "cusip"],
    "filing_13f": ["cik", "crd", "accession"],
    "firm_overlay": ["crd"],
}

# an unquoted all-digit scalar in YAML becomes an int
YAML_UNQUOTED_DIGITS = re.compile(
    r"^\s*(?P<key>[\w.-]*(?:cusip|cik|crd|filing_id|sec_number|fund_id|accession)"
    r"[\w.-]*)\s*:\s*(?P<val>\d+)\s*(?:#.*)?$", re.IGNORECASE | re.MULTILINE)


def main() -> int:
    conn = db.connect()
    problems: list[str] = []
    checked = 0

    print("1. DECLARED COLUMN TYPES")
    print("-" * 78)
    for table, cols in IDENTIFIER_COLUMNS.items():
        try:
            info = {r["name"]: (r["type"] or "").upper()
                    for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            print(f"  {table:<26} (absent, skipped)")
            continue
        if not info:
            print(f"  {table:<26} (absent, skipped)")
            continue
        for c in cols:
            if c not in info:
                continue
            checked += 1
            declared = info[c]
            ok = declared.startswith("TEXT")
            if not ok:
                problems.append(f"{table}.{c} declared {declared or '(none)'}, expected TEXT")
            print(f"  {table+'.'+c:<40} {declared or '(none)':<10} {'ok' if ok else 'PROBLEM'}")

    print("\n2. STORED VALUE TYPES (SQLite stores what you insert, not what you declare)")
    print("-" * 78)
    for table, cols in IDENTIFIER_COLUMNS.items():
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        except Exception:
            continue
        for c in cols:
            try:
                r = conn.execute(
                    f"SELECT COUNT(*) n, SUM(typeof({c})='integer') i,"
                    f" SUM(typeof({c})='real') f FROM {table}").fetchone()
            except Exception:
                continue
            if not r["n"]:
                continue
            bad = (r["i"] or 0) + (r["f"] or 0)
            if bad:
                problems.append(f"{table}.{c}: {bad:,} of {r['n']:,} values stored as numbers")
                print(f"  {table+'.'+c:<40} {bad:,}/{r['n']:,} numeric   PROBLEM")

    print("  (no numeric-typed identifier values found)" if not any(
        "stored as numbers" in p for p in problems) else "")

    print("\n3. CONFIG FILES: unquoted all-digit identifiers")
    print("-" * 78)
    for path in sorted(config.CONFIG_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        hits = list(YAML_UNQUOTED_DIGITS.finditer(text))
        if hits:
            for m in hits:
                problems.append(f"{path.name}: '{m.group('key')}: {m.group('val')}' "
                                f"is unquoted and will parse as an integer")
                print(f"  {path.name:<26} {m.group('key')}: {m.group('val')}   PROBLEM")
        else:
            print(f"  {path.name:<26} clean")

    print("\n4. JOIN TYPE AGREEMENT (a join across mismatched types silently returns nothing)")
    print("-" * 78)
    joins = [("filing_crd", "crd", "firm", "crd"),
             ("sched_d_7b1", "crd", "firm", "crd"),
             ("sched_d_5k3", "filing_id", "filing_crd", "filing_id"),
             ("adv_13f_match", "cik", "edgar_13f_filer", "cik"),
             ("holding_13f", "crd", "firm", "crd"),
             ("holding_13f", "accession", "filing_13f", "accession")]
    for lt, lc, rt, rc in joins:
        try:
            l = conn.execute(f"SELECT typeof({lc}) t FROM {lt} WHERE {lc} IS NOT NULL LIMIT 1").fetchone()
            r = conn.execute(f"SELECT typeof({rc}) t FROM {rt} WHERE {rc} IS NOT NULL LIMIT 1").fetchone()
        except Exception:
            continue
        if not l or not r:
            continue
        ok = l["t"] == r["t"]
        if not ok:
            problems.append(f"{lt}.{lc} ({l['t']}) joins {rt}.{rc} ({r['t']}): type mismatch")
        print(f"  {lt+'.'+lc:<28} -> {rt+'.'+rc:<26} {l['t']}/{r['t']:<8} "
              f"{'ok' if ok else 'PROBLEM'}")

    print("\n" + "=" * 78)
    if problems:
        print(f"{len(problems)} PROBLEM(S) FOUND")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"clean: {checked} identifier columns checked across "
          f"{len(IDENTIFIER_COLUMNS)} tables, all config files, all joins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
