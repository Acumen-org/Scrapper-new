"""PostgreSQL behind the SQLite-shaped API the rest of the app already speaks.

Bellwether was written against sqlite3 and moved to Postgres. The direct route
was to rewrite 327 call sites -- 301 `?` placeholders, 42 schema blocks in 17
files -- which is a very large diff over the exact code that decides what every
firm scores, reviewed by three people, at the moment the storage engine changes
underneath it. A diff that big is not reviewable, and an unreviewable diff over
scoring logic is the thing this repository is most careful to avoid.

So the translation lives here instead, in one module that can be read in full
and tested on its own, and the call sites are left alone. What that buys is a
migration where a `git diff` of the ingest and scoring code shows only the
places where Postgres genuinely behaves differently, and nothing else.

What is deliberately NOT hidden: the differences that change results. Those are
fixed at the call site, in the open, because burying them here is how a scoring
change ships disguised as a port.

Translated (mechanical, no behaviour change):
  - `?` placeholders to `%s`, respecting quoted literals, with literal `%`
    escaped so a LIKE pattern still means what it says.
  - `INTEGER PRIMARY KEY` to an identity column. In SQLite that declaration is
    the rowid alias and self-assigns; Postgres needs to be told.
  - `CREATE VIEW IF NOT EXISTS` to `CREATE OR REPLACE VIEW`.
  - `julianday(a) - julianday(b)` to date subtraction, which is already an
    integer count of days in Postgres.
  - `PRAGMA table_info(t)` to the catalogue query returning the same shape, so
    both `r[1]` and `r["name"]` keep working.
  - `PRAGMA journal_mode` / `foreign_keys` / `busy_timeout`, which have no
    Postgres meaning, to no-ops rather than errors.
  - `executescript`, a sqlite3 API rather than SQL, to a plain multi-statement
    execute.

Rows are the other compatibility surface. `sqlite3.Row` supports `r["col"]`,
`r[0]` and `.keys()`, and this code uses all three; a psycopg dict row supports
only the first. `Row` below restores the other two.

Upserts get more care, because this is where a port can quietly change stored
data rather than merely fail:

  - `INSERT OR IGNORE` becomes `ON CONFLICT DO NOTHING`. Those mean the same
    thing, so it is translated without further thought.
  - `INSERT OR REPLACE` does NOT mean `ON CONFLICT DO UPDATE`. SQLite deletes
    the conflicting row and inserts a new one, so a column the statement leaves
    out is reset to its default; `DO UPDATE` leaves that column at its existing
    value. The two agree only when the statement supplies every column, which
    is the shape all 26 of ours happen to have -- they are bulk ingest writes
    that rewrite a whole row.

    So this checks rather than assumes. It reads the table's real columns and
    key from the catalogue, and translates only when the statement covers every
    column; anything narrower raises and asks for the upsert to be written out
    by hand. A port is allowed to fail loudly. It is not allowed to change what
    a firm scores.

Not translated, on purpose:
  - Text ordering. SQLite compares TEXT byte by byte; Postgres uses the
    database collation, so `ORDER BY` on a name column can come back in a
    different order. Ranked output depends on it, so the database is created
    with a deterministic collation rather than papered over at query time.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql as _sql
from psycopg.rows import dict_row


# --------------------------------------------------------------- connection

def dsn() -> str:
    """Where Postgres is. No default host: a wrong guess here silently writes
    the wrong database, which is worse than refusing to start."""
    url = os.environ.get("BELLWETHER_DSN") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database configured. Set BELLWETHER_DSN, for example:\n"
            "  postgresql://bellwether:PASSWORD@db.internal:5432/bellwether")
    return url


# ---------------------------------------------------------------- SQL rewrite

_PRAGMA_NOOP = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|foreign_keys|busy_timeout|synchronous)\b",
    re.I)
_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*['\"]?(?P<t>[A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\)\s*;?\s*$",
    re.I)
_SQLITE_MASTER_VIEW = re.compile(
    r"SELECT\s+sql\s+FROM\s+sqlite_master\s+WHERE\s+name\s*=\s*'(?P<n>[^']+)'",
    re.I)
_JULIANDAY = re.compile(r"julianday\s*\(\s*([^()]*?)\s*\)", re.I)
_INT_PK = re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\b(?!\s*\()", re.I)
_VIEW_IF_NOT_EXISTS = re.compile(r"CREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\s+", re.I)
_AUTOINCREMENT = re.compile(r"\s+AUTOINCREMENT\b", re.I)

# PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk). Callers
# here read r[1] and r["name"], so both the order and the names have to match.
_TABLE_INFO_SQL = """
SELECT (a.attnum - 1)::int                                   AS cid,
       a.attname::text                                       AS name,
       format_type(a.atttypid, a.atttypmod)                  AS type,
       (a.attnotnull)::int                                   AS notnull,
       pg_get_expr(d.adbin, d.adrelid)                       AS dflt_value,
       COALESCE((SELECT 1 FROM pg_index i
                  WHERE i.indrelid = a.attrelid
                    AND i.indisprimary
                    AND a.attnum = ANY(i.indkey)), 0)::int   AS pk
  FROM pg_attribute a
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE a.attrelid = to_regclass(%s)
   AND a.attnum > 0
   AND NOT a.attisdropped
 ORDER BY a.attnum
"""


def _convert_placeholders(sql: str, has_params: bool) -> str:
    """`?` to `%s`, leaving anything inside a quoted literal alone.

    The `%` escaping is not optional. psycopg reads `%` as the start of a
    placeholder whenever parameters are passed, so an untouched `LIKE '%x%'`
    stops being a LIKE pattern and becomes a syntax error, or worse, binds
    something unintended.
    """
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                # '' and "" are escaped quotes inside a literal, not the end.
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            elif ch == "%" and has_params:
                out.append("%")
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%" and has_params:
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _convert_ddl(sql: str) -> str:
    """SQLite schema text to Postgres schema text.

    Only the declarations that differ. TEXT, INTEGER and REAL are all real
    Postgres types, `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT
    EXISTS` both exist, and `UNIQUE (...)` and `REFERENCES` are unchanged, so
    the great majority of these schemas port as they stand.
    """
    sql = _AUTOINCREMENT.sub("", sql)
    # In SQLite this aliases the rowid and fills itself in. Postgres will not,
    # and every one of these tables relies on the id arriving unasked.
    sql = _INT_PK.sub("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", sql)
    sql = _VIEW_IF_NOT_EXISTS.sub("CREATE OR REPLACE VIEW ", sql)
    return sql


def translate(sql: str, has_params: bool = False) -> str | tuple[str, tuple]:
    """One SQLite statement in, one Postgres statement out.

    Returns a plain string, except for `PRAGMA table_info`, which becomes a
    parameterised catalogue query and so comes back with its parameters.
    """
    m = _PRAGMA_TABLE_INFO.match(sql)
    if m:
        return _TABLE_INFO_SQL, (m.group("t"),)
    if _PRAGMA_NOOP.match(sql):
        return "SELECT 1 WHERE false", ()
    sql = _SQLITE_MASTER_VIEW.sub(
        lambda mo: ("SELECT pg_get_viewdef(to_regclass('%s'), true) AS sql "
                    "WHERE to_regclass('%s') IS NOT NULL"
                    % (mo.group("n"), mo.group("n"))), sql)
    # Postgres subtracts dates directly into an integer number of days, which is
    # what both callers were reaching for julianday to get.
    sql = _JULIANDAY.sub(r"(\1)::date", sql)
    if _OR_IGNORE.match(sql):
        sql = _OR_IGNORE.sub(lambda m: m.group(1) + "INSERT INTO", sql, count=1)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = _convert_ddl(sql)
    return _convert_placeholders(sql, has_params)


# -------------------------------------------------------------------- upserts

_OR_IGNORE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b", re.I)
_OR_REPLACE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(?P<t>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\(\s*(?P<cols>[^)]*?)\s*\))?\s*(?P<rest>VALUES|SELECT)",
    re.I | re.S)

_COLUMNS_SQL = """
SELECT a.attname::text AS name,
       COALESCE((SELECT true FROM pg_index i
                  WHERE i.indrelid = a.attrelid AND i.indisprimary
                    AND a.attnum = ANY(i.indkey)), false) AS is_pk,
       a.attidentity <> '' AS is_identity
  FROM pg_attribute a
 WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""

# Falls back to the narrowest unique index when a table has no primary key.
_UNIQUE_SQL = """
SELECT a.attname::text AS name
  FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
 WHERE i.indrelid = to_regclass(%s) AND i.indisunique AND NOT i.indisprimary
 ORDER BY i.indnatts, a.attnum
"""


def _table_shape(cur: psycopg.Cursor, table: str) -> tuple[list[str], list[str]]:
    cur.execute(_COLUMNS_SQL, (table,))
    rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"INSERT OR REPLACE into unknown table {table!r}")
    cols = [r["name"] for r in rows]
    key = [r["name"] for r in rows if r["is_pk"]]
    if not key:
        cur.execute(_UNIQUE_SQL, (table,))
        seen: list[str] = []
        for r in cur.fetchall():
            if r["name"] not in seen:
                seen.append(r["name"])
        key = seen
    if not key:
        raise RuntimeError(
            f"INSERT OR REPLACE into {table!r}, which has no primary key and no "
            "unique index. Postgres needs a conflict target; give the table a "
            "key, or write the statement out.")
    return cols, key


def _rewrite_or_replace(cur: psycopg.Cursor, sql: str,
                        cache: dict) -> str:
    m = _OR_REPLACE.match(sql)
    if not m:
        raise RuntimeError("unrecognised INSERT OR REPLACE: " + sql[:120])
    table = m.group("t")
    if table not in cache:
        cache[table] = _table_shape(cur, table)
    all_cols, key = cache[table]

    listed = ([c.strip().strip('"') for c in m.group("cols").split(",")]
              if m.group("cols") else
              [c for c in all_cols])  # no column list means every column, in order

    writable = [c for c in all_cols if c in listed]
    missing = [c for c in all_cols if c not in listed]
    if missing:
        raise RuntimeError(
            f"INSERT OR REPLACE into {table!r} does not set {missing}. In SQLite "
            "those columns would be reset to their defaults; ON CONFLICT DO "
            "UPDATE would leave them as they are. The two are not the same, so "
            "write this upsert out explicitly rather than letting it be guessed.")

    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in writable if c not in key)
    head = sql[:m.start("rest")]
    head = re.sub(r"^(\s*)INSERT\s+OR\s+REPLACE\s+INTO", r"\1INSERT INTO", head,
                  count=1, flags=re.I)
    body = sql[m.start("rest"):].rstrip().rstrip(";")
    target = ", ".join(key)
    if not updates:  # every column is part of the key; nothing left to update
        return f"{head}{body} ON CONFLICT ({target}) DO NOTHING"
    return f"{head}{body} ON CONFLICT ({target}) DO UPDATE SET {updates}"


# ---------------------------------------------------------------------- rows

class Row(dict):
    """A dict that also answers to `r[0]` and `.keys()`, the way sqlite3.Row does.

    Subclassing dict rather than wrapping one keeps `dict(r)`, `in`, `.get()`
    and iteration working without reimplementing any of them.
    """

    __slots__ = ()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        if isinstance(key, slice):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    make = dict_row(cursor)

    def go(values):
        return Row(make(values))

    return go


# ------------------------------------------------------------------ wrappers

class Cursor:
    """sqlite3's cursor surface over a psycopg one."""

    def __init__(self, cur: psycopg.Cursor, shapes: dict | None = None):
        self._cur = cur
        self._lastrowid: int | None = None
        self._shapes = shapes if shapes is not None else {}

    # -- the parts the app uses
    def execute(self, sql: str, params: Sequence | None = None) -> "Cursor":
        if _OR_REPLACE.match(sql):
            sql = _rewrite_or_replace(self._cur, sql, self._shapes)
        out = translate(sql, params is not None)
        if isinstance(out, tuple):
            sql, params = out[0], out[1]
        else:
            sql = out
        self._cur.execute(sql, tuple(params) if params is not None else None)
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence]) -> "Cursor":
        if _OR_REPLACE.match(sql):
            sql = _rewrite_or_replace(self._cur, sql, self._shapes)
        out = translate(sql, True)
        sql = out[0] if isinstance(out, tuple) else out
        self._cur.executemany(sql, [tuple(p) for p in seq])
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size: int = 1):
        return self._cur.fetchmany(size)

    def __iter__(self):
        return iter(self._cur)

    def close(self) -> None:
        self._cur.close()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    @property
    def lastrowid(self) -> int | None:
        """Postgres has no lastrowid, so the three callers that need a generated
        id use `RETURNING id` and this reads it back. It is a property rather
        than an attribute so that a statement without RETURNING fails loudly
        here instead of returning a plausible wrong number."""
        if self._lastrowid is not None:
            return self._lastrowid
        try:
            row = self._cur.fetchone()
        except psycopg.ProgrammingError:
            raise RuntimeError(
                "lastrowid needs the INSERT to end in RETURNING id: Postgres "
                "does not track an implicit last row.") from None
        if not row:
            return None
        self._lastrowid = int(row[0] if isinstance(row, (list, tuple))
                              else list(row.values())[0])
        return self._lastrowid


class Connection:
    """sqlite3's connection surface over a psycopg one.

    `conn.execute(...)` returning a cursor is a sqlite3 convenience that psycopg
    does not offer, and this code leans on it everywhere, so it is reproduced
    here rather than edited out of 300 call sites.
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        # Table shapes are read once per connection. They do not change while a
        # run is in flight, and the ingest loops upsert thousands of rows.
        self._shapes: dict = {}

    def cursor(self) -> Cursor:
        return Cursor(self._conn.cursor(), self._shapes)

    def execute(self, sql: str, params: Sequence | None = None) -> Cursor:
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence]) -> Cursor:
        return self.cursor().executemany(sql, seq)

    def executescript(self, script: str) -> Cursor:
        """sqlite3's multi-statement helper. psycopg will run a multi-statement
        string directly, so this is only here to keep the name callers use."""
        cur = self._conn.cursor()
        cur.execute(translate(script, False))  # type: ignore[arg-type]
        self._shapes.clear()  # DDL just ran; any cached shape may be stale
        return Cursor(cur, self._shapes)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def closed(self) -> bool:
        return self._conn.closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect(url: str | None = None, *, autocommit: bool = False) -> Connection:
    conn = psycopg.connect(url or dsn(), row_factory=_row_factory,
                           autocommit=autocommit)
    return Connection(conn)
