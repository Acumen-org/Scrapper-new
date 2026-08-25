"""SQLite store.

Two rules shape this schema:

1. Snapshots are immutable. Nothing here updates a captured artefact in place.
   Ingested rows carry the snapshot they came from, so any past state can be
   reconstructed and any derived number traced back to the filing that produced it.

2. Every run is recorded whether it succeeds or fails. A silent partial pull that
   leaves stale rows looking fresh is the worst failure mode for this system, so
   run_log is written before the work starts and updated when it finishes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
-- ---------------------------------------------------------------- provenance

CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY,
    source_key      TEXT    NOT NULL,
    published_at    TEXT,                 -- date the SEC stamped on the artefact
    captured_at     TEXT    NOT NULL,
    filename        TEXT    NOT NULL,
    rel_path        TEXT    NOT NULL,     -- relative to data/snapshots
    bytes           INTEGER NOT NULL,
    sha256          TEXT    NOT NULL,
    config_stamp    TEXT    NOT NULL,
    UNIQUE (source_key, filename, sha256)
);

CREATE INDEX IF NOT EXISTS ix_snapshot_source ON snapshot (source_key, published_at);

CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY,
    source_key      TEXT    NOT NULL,
    stage           TEXT    NOT NULL,     -- snapshot | ingest
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,     -- running | ok | failed | skipped
    rows_in         INTEGER,
    rows_out        INTEGER,
    prev_rows_out   INTEGER,
    pct_change      REAL,
    flagged         INTEGER NOT NULL DEFAULT 0,
    message         TEXT,
    config_stamp    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_runlog_source ON run_log (source_key, started_at DESC);

-- Header fingerprints for every source file we parse. Compared on every pull so
-- an upstream rename fails the run instead of quietly nulling a column.
CREATE TABLE IF NOT EXISTS source_schema (
    id              INTEGER PRIMARY KEY,
    source_key      TEXT    NOT NULL,
    member          TEXT    NOT NULL,     -- file within the source (or '-')
    observed_at     TEXT    NOT NULL,
    columns_json    TEXT    NOT NULL,
    columns_sha256  TEXT    NOT NULL,
    snapshot_id     INTEGER REFERENCES snapshot(id),
    UNIQUE (source_key, member, columns_sha256)
);

-- --------------------------------------------------- historical Schedule D

-- Schedule D keys on Filing ID, not CRD. Without this crosswalk none of it can
-- be attached to a firm, so it is a hard prerequisite rather than an extra.
CREATE TABLE IF NOT EXISTS filing_crd (
    filing_id       TEXT    PRIMARY KEY,
    crd             TEXT    NOT NULL,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id)
);
CREATE INDEX IF NOT EXISTS ix_filing_crd ON filing_crd (crd);

-- Retained permanently. Thirteen years of Schedule D is the corpus for
-- backtesting trigger definitions, not a one time bootstrap for day one.

-- Custodians holding 10 percent or more of separately managed account assets.
-- crd is resolved through filing_crd at load time and left null where the
-- crosswalk has no entry, so unjoined rows stay visible rather than vanishing.
CREATE TABLE IF NOT EXISTS sched_d_5k3 (
    id                      INTEGER PRIMARY KEY,
    snapshot_id             INTEGER NOT NULL REFERENCES snapshot(id),
    filing_id               TEXT,
    crd                     TEXT,
    custodian_legal_name    TEXT,
    custodian_business_name TEXT,
    city                    TEXT,
    state                   TEXT,
    country                 TEXT,
    is_related_person       TEXT,
    assets_held             INTEGER,
    raw_json                TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_5k3_crd ON sched_d_5k3 (crd);
CREATE INDEX IF NOT EXISTS ix_5k3_cust ON sched_d_5k3 (custodian_business_name);

-- Private fund reporting. Fund type and gross asset value are columns here, not
-- separate tables, so this one table carries the whole PHH alternatives signal.
CREATE TABLE IF NOT EXISTS sched_d_7b1 (
    id                  INTEGER PRIMARY KEY,
    snapshot_id         INTEGER NOT NULL REFERENCES snapshot(id),
    filing_id           TEXT,
    crd                 TEXT,
    fund_id             TEXT,
    fund_name           TEXT,
    fund_type           TEXT,
    fund_type_other     TEXT,
    gross_asset_value   INTEGER,
    minimum_investment  INTEGER,
    state               TEXT,
    country             TEXT,
    fund_of_funds       TEXT,
    raw_json            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_7b1_crd ON sched_d_7b1 (crd);
CREATE INDEX IF NOT EXISTS ix_7b1_fund ON sched_d_7b1 (fund_id);
CREATE INDEX IF NOT EXISTS ix_7b1_type ON sched_d_7b1 (fund_type);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


FIRM_SCHEMA = """
-- One row per firm per feed snapshot. Immutable: snapshots accumulate rather
-- than update, so any past state is reconstructable and triggers are a diff
-- between two rows here.
CREATE TABLE IF NOT EXISTS firm (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id),
    crd             TEXT    NOT NULL,
    legal_name      TEXT,
    business_name   TEXT,
    firm_type       TEXT,       -- Registered | ERA
    is_era          INTEGER NOT NULL DEFAULT 0,
    sec_number      TEXT,
    city            TEXT,
    state           TEXT,
    country         TEXT,
    postal_code     TEXT,
    website         TEXT,       -- first NON social media web address from 1.I
    phone           TEXT,       -- main office phone, MainAddr PhNb
    regulator       TEXT,       -- SEC | STATE, which feed the row came from
    filing_date     TEXT,
    registered_date TEXT,
    total_employees INTEGER,
    iar_count       INTEGER,    -- Item 5.B(1)
    raum            INTEGER,    -- Item 5.F(2)(c) total
    raum_disc       INTEGER,    -- 5.F(2)(a)
    raum_nondisc    INTEGER,    -- 5.F(2)(b)
    clients_total   INTEGER,    -- summed from Item 5.D, not 5.C(1)
    hnw_clients     INTEGER,    -- 5.D category (b)
    hnw_aum         INTEGER,
    retail_clients  INTEGER,    -- 5.D category (a), individuals other than HNW
    retail_aum      INTEGER,
    q5k3            TEXT,
    q7b             TEXT,
    disciplinary    TEXT,       -- Item 11
    PRIMARY KEY (snapshot_id, crd)
);
CREATE INDEX IF NOT EXISTS ix_firm_crd ON firm (crd);
CREATE INDEX IF NOT EXISTS ix_firm_type ON firm (firm_type, raum);
-- The firm list, the inbox and every scored query select the current snapshot,
-- then filter is_era and the RAUM band, then order by RAUM. With two feeds the
-- firm table doubled, and without this the planner scans the whole snapshot.
CREATE INDEX IF NOT EXISTS ix_firm_band ON firm (snapshot_id, is_era, raum);
"""


# One row per firm per snapshot is what makes triggers a diff, but it also
# means a bare JOIN on crd duplicates rows the moment a second snapshot lands.
# Everything that wants "the firm as of now" reads this view instead. Two feeds
# now populate the table (SEC-registered and state-registered), each on its own
# weekly snapshot cadence, so "current" means the newest snapshot per source,
# not one global MAX. State rows for CRDs that also appear in the SEC feed are
# skipped at ingest, so the union here is disjoint by construction.
FIRM_SCHEMA_VIEW = """
CREATE VIEW IF NOT EXISTS firm_current AS
  SELECT * FROM firm WHERE snapshot_id IN (
    SELECT MAX(s.id) FROM snapshot s
    WHERE EXISTS (SELECT 1 FROM firm f WHERE f.snapshot_id = s.id)
    GROUP BY s.source_key);
"""


def init_firm(conn) -> None:
    conn.executescript(FIRM_SCHEMA)
    # Columns added after the table first shipped. ALTER is the only way to
    # grow a SQLite table, and firm_current is a SELECT * view, so it has to be
    # dropped and recreated or it keeps serving the old column list forever.
    have = {r[1] for r in conn.execute("PRAGMA table_info(firm)")}
    if "phone" not in have:
        conn.execute("ALTER TABLE firm ADD COLUMN phone TEXT")
        conn.execute("DROP VIEW IF EXISTS firm_current")
    if "regulator" not in have:
        conn.execute("ALTER TABLE firm ADD COLUMN regulator TEXT")
        conn.execute("UPDATE firm SET regulator='SEC' WHERE regulator IS NULL")
        conn.execute("DROP VIEW IF EXISTS firm_current")
    # The view definition changed when the state feed arrived; recreate it if it
    # still carries the old single-source MAX().
    vsql = conn.execute("SELECT sql FROM sqlite_master WHERE name='firm_current'"
                        ).fetchone()
    if vsql and "GROUP BY s.source_key" not in (vsql["sql"] or ""):
        conn.execute("DROP VIEW IF EXISTS firm_current")
    conn.executescript(FIRM_SCHEMA_VIEW)
    conn.commit()
