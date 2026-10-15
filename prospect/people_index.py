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

import re
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


def score_keys() -> list[str]:
    """Products whose score rides on every row as sc_<key>, so the People list
    filters and sorts by one product's score without a join."""
    from . import products
    return [k for k in products.product_keys() if re.fullmatch(r"[a-z0-9_]+", k)]


TEMP = ("pi_em", "pi_ph", "pi_li", "pi_sc", "pi_extra", "pi_rkeys", "pi_title")


def _contacts_and_scores(conn, keys: list[str]) -> None:
    """Each person's best email, phone and LinkedIn, and each firm's score on
    every product, worked out once for both kinds of row."""
    for t in TEMP:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("""CREATE TEMP TABLE pi_em AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value, verify_status, source, title
        FROM usable_contact_point WHERE kind = 'email' AND person_key != ''
        ORDER BY crd, person_key, (verify_status = 'valid') DESC, confidence DESC""")
    conn.execute("""CREATE TEMP TABLE pi_ph AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value, label, title
        FROM usable_contact_point WHERE kind = 'phone' AND person_key != ''
        ORDER BY crd, person_key, CASE label WHEN 'direct' THEN 0 WHEN 'mobile' THEN 1
                 WHEN 'office' THEN 2 WHEN 'main' THEN 3 ELSE 4 END, confidence DESC""")
    conn.execute("""CREATE TEMP TABLE pi_li AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value
        FROM usable_contact_point WHERE kind = 'linkedin' AND person_key != ''
        ORDER BY crd, person_key, (verify_status = 'matched') DESC, confidence DESC""")
    pivot = "".join(f", MAX(score) FILTER (WHERE product = '{k}') AS sc_{k}" for k in keys)
    if keys and _has(conn, "product_score"):
        conn.execute(f"CREATE TEMP TABLE pi_sc AS SELECT crd{pivot} FROM product_score"
                     f" WHERE status = 'scored' GROUP BY crd")
    else:
        keys.clear()
        conn.execute("CREATE TEMP TABLE pi_sc AS SELECT NULL::text AS crd WHERE false")
    for t in ("pi_em", "pi_ph", "pi_li"):
        conn.execute(f"CREATE INDEX ON {t} (crd, person_key)")
    conn.execute("CREATE INDEX ON pi_sc (crd)")


def _extra_people(conn) -> list[tuple]:
    """People at a firm who hold no registration there: Schedule A officers
    (often the COO, CFO or compliance chief) and people the firm's website or
    a search named (often HR, talent, operations). The firm page and the email
    hunt already treat them as staff; the People screen does too.
    (crd, person_key, src, name, first, last, title, is_officer) per person."""
    from .contacts import name_key
    from .emailguess import pretty
    from .people import schedule_a_display
    out: dict[tuple, list] = {}
    if _has(conn, "schedule_a"):
        for r in conn.execute("SELECT crd, name, title FROM schedule_a WHERE is_individual = 1"
                              " AND crd IN (SELECT crd FROM firm_current)"):
            key = name_key(pretty(r["name"]))
            if not key:
                continue
            title = " ".join((r["title"] or "").split()) or None
            have = out.get((r["crd"], key))
            if have:
                if title and title.lower() not in (have[6] or "").lower():
                    have[6] = f"{have[6]}; {title}" if have[6] else title
                continue
            out[(r["crd"], key)] = [r["crd"], key, "adv", schedule_a_display(r["name"]),
                                    None, None, title, 1]
    for r in conn.execute("""SELECT crd, person_key, MAX(person_name) AS name,
            MAX(NULLIF(TRIM(title), '')) AS title FROM contact_point
            WHERE person_key LIKE 'n:%' AND person_name IS NOT NULL AND person_name != ''
              AND crd IN (SELECT crd FROM firm_current)
            GROUP BY crd, person_key"""):
        have = out.get((r["crd"], r["person_key"]))
        if have:
            have[6] = have[6] or r["title"]
            continue
        out[(r["crd"], r["person_key"])] = [r["crd"], r["person_key"], "web",
                                            " ".join(r["name"].split()), None, None,
                                            r["title"], 0]
    rows = []
    for v in out.values():
        parts = (v[3] or "").split()
        v[4], v[5] = (parts[0] if parts else None), (parts[-1] if len(parts) > 1 else None)
        v[6] = (v[6] or None) and v[6][:200]
        rows.append(tuple(v))
    return rows


def _tag_titles(conn) -> None:
    """Each distinct title, read once: its normalised form (what a typed title
    is matched against) and the designations it fills, for every product."""
    from . import designations
    compiled = designations.matcher()
    titles = [r["title"] for r in conn.execute(
        "SELECT DISTINCT title FROM people_index_new WHERE title IS NOT NULL AND title != ''")]
    conn.execute("CREATE TEMP TABLE pi_title (title TEXT PRIMARY KEY, title_norm TEXT,"
                 " desig TEXT[])")
    batch = []
    for t in titles:
        tags = designations.tags_for(t, compiled)
        batch.append((t, designations.norm(t), "{" + ",".join(tags) + "}" if tags else None))
    for i in range(0, len(batch), 5000):
        conn.executemany("INSERT INTO pi_title VALUES (?, ?, ?::text[])", batch[i:i + 5000])
    conn.execute("""UPDATE people_index_new p SET title_norm = t.title_norm, desig = t.desig
        FROM pi_title t WHERE p.title = t.title""")


def _build(conn, t0: float) -> tuple[int, float]:
    cls = _has(conn, "firm_class")
    cls_col = "fc.category" if cls else "NULL::text"
    sa_ok = _has(conn, "schedule_a")
    sa_cte = ("""sa AS (
          SELECT DISTINCT ON (crd, k) crd, k, title FROM (
            SELECT crd, title, UPPER(TRIM(SPLIT_PART(name, ',', 1))) || '|' ||
                   UPPER(LEFT(TRIM(SPLIT_PART(name, ',', 2)), 3)) AS k
            FROM schedule_a WHERE is_individual = 1 AND title IS NOT NULL AND title != '') z
          ORDER BY crd, k),""" if sa_ok else "sa AS (SELECT NULL::text crd, NULL::text k, NULL::text title),")
    keys = score_keys()
    _contacts_and_scores(conn, keys)
    sc_cols = "".join(f", psx.sc_{k}" for k in keys)
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
        {sa_cte.rstrip(',')}
        SELECT p.indvl_pk, 'i:' || p.indvl_pk AS person_key, 'sec'::text AS src,
               cur.crd, p.name, LOWER(p.name) AS name_lc, p.first_name, p.last_name,
               COALESCE(sa.title, em.title, ph.title) AS title,
               NULL::text AS title_norm, NULL::text[] AS desig,
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
               COALESCE(ph.label IN ('direct', 'mobile'), false)::int AS has_direct,
               (ph.value IS NOT NULL)::int AS has_phone,
               (li.value IS NOT NULL)::int AS has_linkedin,
               p.iapd_link{sc_cols}
        FROM cur
        JOIN person p ON p.indvl_pk = cur.indvl_pk
        JOIN firm_current f ON f.crd = cur.crd
        LEFT JOIN firm_scope sc ON sc.crd = cur.crd
        LEFT JOIN prior ON prior.indvl_pk = p.indvl_pk
        LEFT JOIN sa ON sa.crd = cur.crd
             AND sa.k = UPPER(COALESCE(p.last_name, '')) || '|' || UPPER(LEFT(COALESCE(p.first_name, ''), 3))
        LEFT JOIN pi_em em ON em.crd = cur.crd AND em.person_key = 'i:' || p.indvl_pk
        LEFT JOIN pi_ph ph ON ph.crd = cur.crd AND ph.person_key = 'i:' || p.indvl_pk
        LEFT JOIN pi_li li ON li.crd = cur.crd AND li.person_key = 'i:' || p.indvl_pk
        LEFT JOIN pi_sc psx ON psx.crd = cur.crd
        {"LEFT JOIN firm_class fc ON fc.crd = cur.crd" if cls else ""}""")
    conn.execute("CREATE INDEX ON people_index_new (crd)")

    # Officers and website people not on the roster. A roster person's name
    # key (first and last name) leaves out anyone already listed.
    conn.execute("""CREATE TEMP TABLE pi_extra (crd TEXT, person_key TEXT, src TEXT, name TEXT,
        first_name TEXT, last_name TEXT, title TEXT, is_officer INT)""")
    extra = _extra_people(conn)
    for i in range(0, len(extra), 5000):
        conn.executemany("INSERT INTO pi_extra VALUES (?,?,?,?,?,?,?,?)", extra[i:i + 5000])
    conn.execute(r"""CREATE TEMP TABLE pi_rkeys AS SELECT DISTINCT crd,
        'n:' || SPLIT_PART(TRIM(REGEXP_REPLACE(LOWER(COALESCE(first_name, '')), '[^a-z]+', ' ', 'g')), ' ', 1)
        || ' ' || REGEXP_REPLACE(TRIM(REGEXP_REPLACE(LOWER(COALESCE(last_name, '')), '[^a-z]+', ' ', 'g')),
                                 '^.* ', '') AS k
        FROM people_index_new""")
    conn.execute("CREATE INDEX ON pi_rkeys (crd, k)")
    conn.execute(f"""
        INSERT INTO people_index_new
        SELECT NULL, x.person_key, x.src, x.crd, x.name, LOWER(x.name), x.first_name, x.last_name,
               COALESCE(x.title, em.title, ph.title), NULL, NULL,
               f.legal_name, LOWER(COALESCE(f.legal_name, '')), f.state, f.city, NULL, NULL, f.raum,
               sc.priority, sc.products, sc.best_score, {cls_col},
               NULL, NULL, NULL, 0, 0, 0, x.is_officer,
               (COALESCE(x.title, em.title, ph.title, '') ~* '{LEADER_RE}')::int,
               em.value, em.verify_status, em.source,
               COALESCE(ph.value, NULLIF(f.phone, '')),
               CASE WHEN ph.value IS NOT NULL THEN ph.label
                    WHEN COALESCE(f.phone, '') != '' THEN 'main' END,
               li.value,
               (em.value IS NOT NULL)::int,
               COALESCE(ph.label IN ('direct', 'mobile'), false)::int,
               (COALESCE(ph.value, NULLIF(f.phone, '')) IS NOT NULL)::int,
               (li.value IS NOT NULL)::int,
               NULL{sc_cols}
        FROM pi_extra x
        JOIN firm_current f ON f.crd = x.crd
        LEFT JOIN firm_scope sc ON sc.crd = x.crd
        LEFT JOIN pi_em em ON em.crd = x.crd AND em.person_key = x.person_key
        LEFT JOIN pi_ph ph ON ph.crd = x.crd AND ph.person_key = x.person_key
        LEFT JOIN pi_li li ON li.crd = x.crd AND li.person_key = x.person_key
        LEFT JOIN pi_sc psx ON psx.crd = x.crd
        {"LEFT JOIN firm_class fc ON fc.crd = x.crd" if cls else ""}
        WHERE NOT EXISTS (SELECT 1 FROM pi_rkeys r WHERE r.crd = x.crd AND r.k = x.person_key)""")
    _tag_titles(conn)
    ddls = [
        "CREATE INDEX ON people_index_new (state)",
        "CREATE INDEX ON people_index_new (priority DESC NULLS LAST, raum DESC NULLS LAST)",
        "CREATE INDEX ON people_index_new (start_date DESC NULLS LAST)",
        "CREATE INDEX ON people_index_new (last_name, first_name)",
        "CREATE INDEX ON people_index_new (name_lc text_pattern_ops)",
        "CREATE INDEX ON people_index_new (has_email, priority DESC NULLS LAST)",
        "CREATE INDEX ON people_index_new (indvl_pk)",
        "CREATE INDEX ON people_index_new USING gin (desig)"]
    ddls += [f"CREATE INDEX ON people_index_new (sc_{k} DESC NULLS LAST, raum DESC NULLS LAST)"
             for k in keys]
    for ddl in ddls:
        conn.execute(ddl)
    for t in TEMP:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    # Committed before the optional extension: a failed CREATE EXTENSION rolls
    # its transaction back, and must not take the new table with it.
    conn.commit()
    if _trgm(conn):
        try:
            conn.execute("CREATE INDEX ON people_index_new USING gin (name_lc gin_trgm_ops)")
            conn.execute("CREATE INDEX ON people_index_new USING gin (firm_lc gin_trgm_ops)")
            conn.execute("CREATE INDEX ON people_index_new USING gin (title_norm gin_trgm_ops)")
            conn.commit()
        except Exception:
            conn.rollback()
    conn.execute("ANALYZE people_index_new")
    conn.execute("DROP TABLE IF EXISTS people_index")
    conn.execute("ALTER TABLE people_index_new RENAME TO people_index")
    conn.commit()
    _COLS.clear()
    n = conn.execute("SELECT COUNT(*) n FROM people_index").fetchone()["n"]
    return n, time.monotonic() - t0


_COLS: dict = {}


def columns(conn) -> set[str]:
    """The index's columns, asked once a minute: a table built before an
    upgrade lacks the newer ones until its next rebuild."""
    hit = _COLS.get("v")
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='people_index'")}
    except Exception:
        conn.rollback()
        cols = set()
    _COLS["v"] = (time.monotonic(), cols)
    return cols


def coverage(conn) -> dict:
    """How reachable the people Bellwether knows are, for Home and People."""
    if not exists(conn):
        return {}
    try:
        r = conn.execute("""SELECT COUNT(*) total, SUM(has_email) email, SUM(has_direct) direct,
                   COUNT(*) FILTER (WHERE indvl_pk IS NOT NULL) registered,
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
