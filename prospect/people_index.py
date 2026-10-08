"""People index: one narrow row per person at each firm they work for now.

The People screen used to join the SEC roster (2.8 million employment rows),
the person table, Schedule A titles and every contact at request time, and on
the live server a page took two to six seconds. Here all of that is worked
out once, in the background, into a table built for the screen: the person's
best usable email, best phone (direct before office before main line) and
LinkedIn are already chosen, the officer title is already matched, and the
firm's facts ride along, so any filter is one indexed read.

The table is rebuilt as a whole into a new name and swapped in, so readers
never wait on a half-built index. The people_index job keeps it fresh as
the contact jobs find addresses; a rebuild takes well under a minute.
"""

from __future__ import annotations

import time

LEADER_RE = (r"(chief|president|founder|partner|principal|managing|director|owner|"
             r"\mceo\M|\mcio\M|\mcoo\M|\mcfo\M|\mcco\M|chair|head of|vice president|\mvp\M)")


def _has(conn, table: str) -> bool:
    try:
        r = conn.execute("SELECT to_regclass(?) t", (table,)).fetchone()
        return bool(r and r["t"])
    except Exception:
        conn.rollback()
        return False


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
            (table, column)).fetchone())
    except Exception:
        conn.rollback()
        return False


def exists(conn) -> bool:
    return _has(conn, "people_index")


def _trgm(conn) -> bool:
    """Trigram indexes make 'smith' anywhere in a name instant; the extension
    is not on every server, and the screen works without it."""
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


LOCK_ID = 424243   # one builder at a time, across the app and the job worker


def build(conn) -> tuple[int, float]:
    """Rebuild the index; returns (rows, seconds). When another process is
    already building it, returns (0, 0.0) at once: two builders would both
    create people_index_new and the second would fail."""
    t0 = time.monotonic()
    if not (_has(conn, "person") and _has(conn, "person_employment")):
        return 0, 0.0
    got = conn.execute("SELECT pg_try_advisory_lock(?) ok", (LOCK_ID,)).fetchone()["ok"]
    conn.commit()
    if not got:
        return 0, 0.0
    try:
        return _build(conn, t0)
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(?)", (LOCK_ID,))
            conn.commit()
        except Exception:
            pass


def _build(conn, t0: float) -> tuple[int, float]:
    cls = _has(conn, "firm_class")
    cls_join = "LEFT JOIN firm_class fc ON fc.crd = cur.crd" if cls else ""
    cls_col = "fc.category" if cls else "NULL::text"
    sa_ok = _has(conn, "schedule_a")
    sa_cte = ("""sa AS (
          SELECT DISTINCT ON (crd, k) crd, k, title FROM (
            SELECT crd, title, UPPER(TRIM(SPLIT_PART(name, ',', 1))) || '|' ||
                   UPPER(LEFT(TRIM(SPLIT_PART(name, ',', 2)), 3)) AS k
            FROM schedule_a WHERE is_individual = 1 AND title IS NOT NULL AND title != '') z
          ORDER BY crd, k),""" if sa_ok else "sa AS (SELECT NULL::text crd, NULL::text k, NULL::text title),")
    conn.execute("DROP TABLE IF EXISTS people_index_new")
    conn.execute(f"""
        CREATE TABLE people_index_new AS
        WITH cur AS (
          SELECT DISTINCT ON (e.indvl_pk, e.org_pk) e.indvl_pk, e.org_pk AS crd, e.start_date,
                 e.city AS bcity, e.state AS bstate
          FROM person_employment e WHERE e.kind = 'current'
          ORDER BY e.indvl_pk, e.org_pk, e.start_date DESC NULLS LAST),
        prior AS (
          SELECT DISTINCT ON (x.indvl_pk) x.indvl_pk, x.org_name
          FROM person_employment x WHERE x.kind = 'previous'
          ORDER BY x.indvl_pk, x.end_date DESC NULLS LAST),
        {sa_cte}
        em AS (
          SELECT DISTINCT ON (crd, person_key) crd, person_key, value, verify_status, source, title
          FROM usable_contact_point WHERE kind = 'email' AND person_key LIKE 'i:%'
          ORDER BY crd, person_key, (verify_status = 'valid') DESC, confidence DESC),
        ph AS (
          SELECT DISTINCT ON (crd, person_key) crd, person_key, value, label, title
          FROM usable_contact_point WHERE kind = 'phone' AND person_key LIKE 'i:%'
          ORDER BY crd, person_key, CASE label WHEN 'direct' THEN 0 WHEN 'mobile' THEN 1
                   WHEN 'office' THEN 2 WHEN 'main' THEN 3 ELSE 4 END, confidence DESC),
        li AS (
          SELECT DISTINCT ON (crd, person_key) crd, person_key, value
          FROM usable_contact_point WHERE kind = 'linkedin' AND person_key LIKE 'i:%'
          ORDER BY crd, person_key, (verify_status = 'matched') DESC, confidence DESC)
        SELECT p.indvl_pk, cur.crd, p.name, LOWER(p.name) AS name_lc, p.first_name, p.last_name,
               COALESCE(sa.title, em.title, ph.title) AS title,
               f.legal_name AS firm_name, LOWER(COALESCE(f.legal_name, '')) AS firm_lc,
               f.state, f.city AS firm_city, cur.bcity, cur.bstate, f.raum,
               sc.priority, sc.products, sc.best_score, {cls_col} AS category,
               cur.start_date, prior.org_name AS prior_firm,
               p.designations,
               (COALESCE(p.designations, '') ILIKE '%CFP%')::int AS cfp,
               (COALESCE(p.designations, '') ILIKE '%CFA%')::int AS cfa,
               COALESCE(p.has_disclosure, 0)::int AS has_disclosure,
               (sa.title IS NOT NULL)::int AS is_officer,
               (COALESCE(sa.title, em.title, ph.title, '') ~* '{LEADER_RE}')::int AS is_leader,
               em.value AS email, em.verify_status AS email_status, em.source AS email_source,
               ph.value AS phone, ph.label AS phone_label, li.value AS linkedin,
               (em.value IS NOT NULL)::int AS has_email,
               (ph.label IN ('direct', 'mobile'))::int AS has_direct,
               (ph.value IS NOT NULL)::int AS has_phone,
               (li.value IS NOT NULL)::int AS has_linkedin,
               p.iapd_link
        FROM cur
        JOIN person p ON p.indvl_pk = cur.indvl_pk
        JOIN firm_current f ON f.crd = cur.crd
        LEFT JOIN firm_scope sc ON sc.crd = cur.crd
        LEFT JOIN prior ON prior.indvl_pk = p.indvl_pk
        LEFT JOIN sa ON sa.crd = cur.crd
             AND sa.k = UPPER(COALESCE(p.last_name, '')) || '|' || UPPER(LEFT(COALESCE(p.first_name, ''), 3))
        LEFT JOIN em ON em.crd = cur.crd AND em.person_key = 'i:' || p.indvl_pk
        LEFT JOIN ph ON ph.crd = cur.crd AND ph.person_key = 'i:' || p.indvl_pk
        LEFT JOIN li ON li.crd = cur.crd AND li.person_key = 'i:' || p.indvl_pk
        {cls_join}""")
    for ddl in (
            "CREATE INDEX ON people_index_new (crd)",
            "CREATE INDEX ON people_index_new (state)",
            "CREATE INDEX ON people_index_new (priority DESC NULLS LAST, raum DESC NULLS LAST)",
            "CREATE INDEX ON people_index_new (start_date DESC NULLS LAST)",
            "CREATE INDEX ON people_index_new (last_name, first_name)",
            "CREATE INDEX ON people_index_new (name_lc text_pattern_ops)",
            "CREATE INDEX ON people_index_new (has_email, priority DESC NULLS LAST)",
            "CREATE INDEX ON people_index_new (indvl_pk)"):
        conn.execute(ddl)
    # Committed before the optional extension: a failed CREATE EXTENSION rolls
    # its transaction back, and must not take the new table with it.
    conn.commit()
    if _trgm(conn):
        try:
            conn.execute("CREATE INDEX ON people_index_new USING gin (name_lc gin_trgm_ops)")
            conn.execute("CREATE INDEX ON people_index_new USING gin (firm_lc gin_trgm_ops)")
            conn.commit()
        except Exception:
            conn.rollback()
    conn.execute("ANALYZE people_index_new")
    conn.execute("DROP TABLE IF EXISTS people_index")
    conn.execute("ALTER TABLE people_index_new RENAME TO people_index")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) n FROM people_index").fetchone()["n"]
    return n, time.monotonic() - t0


def coverage(conn) -> dict:
    """How reachable the people Bellwether knows are, for Home and People."""
    if not exists(conn):
        return {}
    try:
        r = conn.execute("""SELECT COUNT(*) total, SUM(has_email) email, SUM(has_direct) direct,
                   COUNT(*) FILTER (WHERE email_status='valid') verified,
                   SUM(has_phone) phone, SUM(has_linkedin) linkedin,
                   SUM(CASE WHEN priority IS NOT NULL THEN 1 ELSE 0 END) listed,
                   SUM(CASE WHEN priority IS NOT NULL THEN has_email ELSE 0 END) listed_email,
                   SUM(CASE WHEN has_email = 1 OR has_direct = 1 OR has_linkedin = 1
                       THEN 1 ELSE 0 END) reachable
                   FROM people_index""").fetchone()
        return {k: int(r[k] or 0) for k in r.keys()}
    except Exception:
        conn.rollback()
        return {}
