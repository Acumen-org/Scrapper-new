"""Does the Postgres adapter still behave like the SQLite it replaced?

Eight checks over prospect.pg, run against a real database rather than a mock,
because every one of them is about a difference between two engines and a mock
would only ever confirm what this file already believes.

Point it at a scratch database, never the live one: it writes rows and it
expects to be the only writer.

    BELLWETHER_DSN=postgresql://... python -m scripts.qa_pg

Exits nonzero on any failure, so it can gate a merge alongside scripts.qa_smoke.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import db, guard, runlog  # noqa: E402

conn = db.connect()
db.init(conn); db.init_firm(conn)
print("1. db.connect() + init() + init_firm() OK")

db.init_firm(conn)
print("2. init_firm idempotent on second call OK")

with runlog.Run(conn, "adv_feed", "ingest", "stamp") as run:
    pass
assert isinstance(run.id, int) and run.id > 0, run.id
r = conn.execute("SELECT status FROM run_log WHERE id = ?", (run.id,)).fetchone()
print(f"3. runlog.Run round-trip OK (id={run.id}, status={r['status']})")

guard.record_columns(conn, "adv_feed", "-", ["a", "b", "c"], None)
conn.commit()
assert guard.known_layouts(conn, "adv_feed", "-") == [["a", "b", "c"]]
print("4. guard.record_columns / known_layouts OK")

cols = {x["name"] for x in conn.execute("PRAGMA table_info(firm)")}
assert {"phone", "regulator", "raum", "crd"} <= cols
conn.execute("SELECT COUNT(*) FROM firm_current").fetchone()
print(f"5. firm schema + firm_current view OK ({len(cols)} cols)")

# INSERT OR IGNORE must not duplicate on a repeat
guard.record_columns(conn, "adv_feed", "-", ["a", "b", "c"], None)
conn.commit()
n = conn.execute("SELECT COUNT(*) AS n FROM source_schema").fetchone()["n"]
assert n == 1, f"OR IGNORE duplicated: {n} rows"
print("6. INSERT OR IGNORE -> ON CONFLICT DO NOTHING is idempotent OK")

sid = conn.execute(
    "INSERT INTO snapshot (source_key, published_at, captured_at, filename,"
    " rel_path, bytes, sha256, config_stamp) VALUES (?,?,?,?,?,?,?,?)"
    " RETURNING id",
    ("adv_feed", "2026-08-01", "2026-08-27", "f.zip", "a/f.zip", 10, "abc", "st")
).lastrowid

# INSERT OR REPLACE must replace on the real key, not duplicate
conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd, snapshot_id)"
             " VALUES (?,?,?)", ("f1", "111", sid))
conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd, snapshot_id)"
             " VALUES (?,?,?)", ("f1", "999", sid))
conn.commit()
row = conn.execute("SELECT crd FROM filing_crd WHERE filing_id='f1'").fetchone()
n = conn.execute("SELECT COUNT(*) AS n FROM filing_crd").fetchone()["n"]
assert n == 1 and row["crd"] == "999", (n, dict(row))
print("7. INSERT OR REPLACE upserts on the real key OK (replaced, not duplicated)")

# A partial INSERT OR REPLACE must refuse, because SQLite and Postgres would
# store different things and neither answer is safe to guess at.
try:
    conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd) VALUES (?,?)",
                 ("f2", "222"))
    raise AssertionError("partial INSERT OR REPLACE was allowed through")
except RuntimeError as e:
    assert "snapshot_id" in str(e), e
    print("8. partial INSERT OR REPLACE refused, naming the unset column OK")

print("\nINTEGRATION CHECKS PASS")
