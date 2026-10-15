"""People index: one narrow row per person at each firm they work for now.

The People screen used to join the SEC roster (2.8 million employment rows),
the person table, Schedule A titles and every contact at request time, and on
the live server a page took two to six seconds. Here all of that is worked
out once, in the background, into a table built for the screen: the person's
best usable email, best phone (direct before office before main line) and
LinkedIn are already chosen, the officer title is already matched, and the
firm's facts ride along, so any filter is one indexed read.

The people_index job keeps it fresh as the contact jobs find addresses, and
most of its runs find little or nothing new. So each firm's inputs (its
contacts, Schedule A, scores, scope, type and filed facts) are fingerprinted:
a run where none changed stops there, and a run where a few firms changed
rebuilds just those firms' rows in place, in one transaction, with the same
query a full build uses. A full build makes the whole table under a new name
and swaps it in, so readers never wait on a half-built index; it runs at least
daily, and whenever the roster, the designations, the product lists or this
code change, or too many firms changed for a refresh to be the quicker way.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

LEADER_RE = (r"(chief|president|founder|partner|principal|managing|director|owner|"
             r"\mceo\M|\mcio\M|\mcoo\M|\mcfo\M|\mcco\M|chair|head of|vice president|\mvp\M)")

# Bump when the SQL here changes what a row holds: the next run rebuilds all.
BUILD_VERSION = "2"
FULL_EVERY_H = 24
# A refresh in place pays for every index on every row it rewrites; past this
# share of the table a full build (bulk load, then indexes) is quicker.
REFRESH_MAX_SHARE = 0.2

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people_index_state (
    k   TEXT PRIMARY KEY,      -- inputs | full_at | refreshed_at
    v   TEXT
);
CREATE TABLE IF NOT EXISTS people_index_firm (
    crd TEXT PRIMARY KEY,
    fp  TEXT NOT NULL          -- what the firm's rows were last built from
);
"""


def init(conn) -> None:
    """The two small tables that say what the index was built from. Created
    by the job and at startup, never in a request."""
    conn.executescript(STATE_SCHEMA)
    conn.commit()


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


def build(conn, full: bool = False, info: dict | None = None) -> tuple[int, float]:
    """Bring the index up to date: nothing when nothing it reads changed, the
    changed firms' rows in place, or a full build (always with `full`).
    Returns (rows in the index, seconds); `info`, when given, receives what
    was done (mode, why, firms, written). When another process is already
    building it, returns (0, 0.0) at once: two builders would both create
    people_index_new and the second would fail."""
    t0 = time.monotonic()
    info = info if info is not None else {}
    if not (_has(conn, "person") and _has(conn, "person_employment")):
        info.update(mode="no roster", written=0)
        return 0, 0.0
    got = conn.execute("SELECT pg_try_advisory_lock(?) ok", (LOCK_ID,)).fetchone()["ok"]
    conn.commit()
    if not got:
        info.update(mode="busy")
        return 0, 0.0
    try:
        n = _update(conn, full, info)
        return n, time.monotonic() - t0
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
# Only the firms a refresh rebuilds (pi_crds) and their new rows (pi_stage).
REFRESH_TEMP = ("pi_crds", "pi_stage")
# Added to every input query of a refresh, so it reads only those firms.
ONLY = " AND crd IN (SELECT crd FROM pi_crds)"


def _contacts_and_scores(conn, keys: list[str], only: str = "") -> None:
    """Each person's best email, phone and LinkedIn, and each firm's score on
    every product, worked out once for both kinds of row. Ties go to the
    oldest contact, so a refresh and a full build pick the same one."""
    for t in TEMP:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute(f"""CREATE TEMP TABLE pi_em AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value, verify_status, source, title
        FROM usable_contact_point WHERE kind = 'email' AND person_key != ''{only}
        ORDER BY crd, person_key, (verify_status = 'valid') DESC, confidence DESC, id""")
    conn.execute(f"""CREATE TEMP TABLE pi_ph AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value, label, title
        FROM usable_contact_point WHERE kind = 'phone' AND person_key != ''{only}
        ORDER BY crd, person_key, CASE label WHEN 'direct' THEN 0 WHEN 'mobile' THEN 1
                 WHEN 'office' THEN 2 WHEN 'main' THEN 3 ELSE 4 END, confidence DESC, id""")
    conn.execute(f"""CREATE TEMP TABLE pi_li AS
        SELECT DISTINCT ON (crd, person_key) crd, person_key, value
        FROM usable_contact_point WHERE kind = 'linkedin' AND person_key != ''{only}
        ORDER BY crd, person_key, (verify_status = 'matched') DESC, confidence DESC, id""")
    pivot = "".join(f", MAX(score) FILTER (WHERE product = '{k}') AS sc_{k}" for k in keys)
    if keys and _has(conn, "product_score"):
        conn.execute(f"CREATE TEMP TABLE pi_sc AS SELECT crd{pivot} FROM product_score"
                     f" WHERE status = 'scored'{only} GROUP BY crd")
    else:
        keys.clear()
        conn.execute("CREATE TEMP TABLE pi_sc AS SELECT NULL::text AS crd WHERE false")
    for t in ("pi_em", "pi_ph", "pi_li"):
        conn.execute(f"CREATE INDEX ON {t} (crd, person_key)")
    conn.execute("CREATE INDEX ON pi_sc (crd)")


def _extra_people(conn, only: str = "") -> list[tuple]:
    """People at a firm who hold no registration there: Schedule A officers
    (often the COO, CFO or compliance chief) and people the firm's website or
    a search named (often HR, talent, operations). The firm page and the email
    hunt already treat them as staff; the People screen does too.
    (crd, person_key, src, name, first, last, title, is_officer) per person.
    Read in a fixed order, so the name shown and the order of an officer's
    titles do not depend on how the rows happen to be stored."""
    from .contacts import name_key
    from .emailguess import pretty
    from .people import schedule_a_display
    out: dict[tuple, list] = {}
    if _has(conn, "schedule_a"):
        for r in conn.execute("SELECT crd, name, title FROM schedule_a WHERE is_individual = 1"
                              f" AND crd IN (SELECT crd FROM firm_current){only}"
                              " ORDER BY crd, name, title"):
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
    for r in conn.execute(f"""SELECT crd, person_key, MAX(person_name) AS name,
            MAX(NULLIF(TRIM(title), '')) AS title FROM contact_point
            WHERE person_key LIKE 'n:%' AND person_name IS NOT NULL AND person_name != ''
              AND crd IN (SELECT crd FROM firm_current){only}
            GROUP BY crd, person_key ORDER BY crd, person_key"""):
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


def _tag_titles(conn, target: str) -> None:
    """Each distinct title, read once: its normalised form (what a typed title
    is matched against) and the designations it fills, for every product."""
    from . import designations
    compiled = designations.matcher()
    titles = [r["title"] for r in conn.execute(
        f"SELECT DISTINCT title FROM {target} WHERE title IS NOT NULL AND title != ''")]
    conn.execute("CREATE TEMP TABLE pi_title (title TEXT PRIMARY KEY, title_norm TEXT,"
                 " desig TEXT[])")
    batch = []
    for t in titles:
        tags = designations.tags_for(t, compiled)
        batch.append((t, designations.norm(t), "{" + ",".join(tags) + "}" if tags else None))
    for i in range(0, len(batch), 5000):
        conn.executemany("INSERT INTO pi_title VALUES (?, ?, ?::text[])", batch[i:i + 5000])
    conn.execute(f"""UPDATE {target} p SET title_norm = t.title_norm, desig = t.desig
        FROM pi_title t WHERE p.title = t.title""")


def _rows(conn, target: str, keys: list[str], only: bool) -> None:
    """Every row of the index into `target`: the whole roster for a full
    build, or with `only` just the firms in pi_crds, into a temporary table.
    One query for both, so a firm refreshed in place gets exactly the rows a
    full build would give it."""
    pick = ONLY if only else ""
    cls = _has(conn, "firm_class")
    cls_col = "fc.category" if cls else "NULL::text"
    sa_ok = _has(conn, "schedule_a")
    sa_cte = (f"""sa AS (
          SELECT DISTINCT ON (crd, k) crd, k, title FROM (
            SELECT crd, title, UPPER(TRIM(SPLIT_PART(name, ',', 1))) || '|' ||
                   UPPER(LEFT(TRIM(SPLIT_PART(name, ',', 2)), 3)) AS k
            FROM schedule_a WHERE is_individual = 1 AND title IS NOT NULL AND title != ''{pick}) z
          ORDER BY crd, k, title),""" if sa_ok else "sa AS (SELECT NULL::text crd, NULL::text k, NULL::text title),")
    _contacts_and_scores(conn, keys, pick)
    sc_cols = "".join(f", psx.sc_{k}" for k in keys)
    cur_pick = " AND e.org_pk IN (SELECT crd FROM pi_crds)" if only else ""
    # A refresh needs the previous employer of its own people only.
    prior_pick = " AND x.indvl_pk IN (SELECT indvl_pk FROM cur)" if only else ""
    conn.execute(f"DROP TABLE IF EXISTS {target}")
    conn.execute(f"""
        CREATE {"TEMP " if only else ""}TABLE {target} AS
        WITH cur AS (
          SELECT DISTINCT ON (e.indvl_pk, e.org_pk) e.indvl_pk, e.org_pk AS crd, e.start_date,
                 e.city AS bcity, e.state AS bstate
          FROM person_employment e WHERE e.kind = 'current'{cur_pick}
          ORDER BY e.indvl_pk, e.org_pk, e.start_date DESC NULLS LAST),
        prior AS (
          SELECT DISTINCT ON (x.indvl_pk) x.indvl_pk, x.org_name
          FROM person_employment x WHERE x.kind = 'previous'{prior_pick}
          ORDER BY x.indvl_pk, x.end_date DESC NULLS LAST, x.start_date DESC, x.org_pk),
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
    conn.execute(f"CREATE INDEX ON {target} (crd)")

    # Officers and website people not on the roster. A roster person's name
    # key (first and last name) leaves out anyone already listed.
    conn.execute("""CREATE TEMP TABLE pi_extra (crd TEXT, person_key TEXT, src TEXT, name TEXT,
        first_name TEXT, last_name TEXT, title TEXT, is_officer INT)""")
    extra = _extra_people(conn, pick)
    for i in range(0, len(extra), 5000):
        conn.executemany("INSERT INTO pi_extra VALUES (?,?,?,?,?,?,?,?)", extra[i:i + 5000])
    conn.execute(rf"""CREATE TEMP TABLE pi_rkeys AS SELECT DISTINCT crd,
        'n:' || SPLIT_PART(TRIM(REGEXP_REPLACE(LOWER(COALESCE(first_name, '')), '[^a-z]+', ' ', 'g')), ' ', 1)
        || ' ' || REGEXP_REPLACE(TRIM(REGEXP_REPLACE(LOWER(COALESCE(last_name, '')), '[^a-z]+', ' ', 'g')),
                                 '^.* ', '') AS k
        FROM {target}""")
    conn.execute("CREATE INDEX ON pi_rkeys (crd, k)")
    conn.execute(f"""
        INSERT INTO {target}
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
    _tag_titles(conn, target)


# ------------------------------------------------------------ what changed

def _inputs(conn, keys: list[str]) -> str:
    """Fingerprint of what every row rests on: the roster (the people job
    replaces both tables whole, so they are new tables after each load), the
    designations, the product lists, the views the rows read, and this code.
    Any change here means a full build."""
    from . import designations
    r = conn.execute("""SELECT to_regclass('person')::oid::text AS person,
            to_regclass('person_employment')::oid::text AS employment,
            (SELECT COUNT(*) FROM person) AS people,
            to_regclass('schedule_a') IS NOT NULL AS sa,
            to_regclass('firm_class') IS NOT NULL AS fc,
            to_regclass('product_score') IS NOT NULL AS ps,
            pg_get_viewdef(to_regclass('usable_contact_point')) AS usable,
            pg_get_viewdef(to_regclass('firm_current')) AS firms""").fetchone()
    parts = [BUILD_VERSION, LEADER_RE, ",".join(keys), repr(designations.matcher()),
             repr(sorted(dict(r).items()))]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _sum(cols: str) -> str:
    """SQL for the sum of a 64-bit hash of each row: the same however the rows
    are read, and different (all but certainly) once any row changes."""
    return f"SUM(('x' || LEFT(md5(ROW({cols})::text), 16))::bit(64)::bigint::numeric)"


def _firm_fps(conn) -> dict[str, str]:
    """Per firm, a fingerprint of everything its rows are made from besides
    the roster: its filed facts, every contact (the screens' choice of email
    and phone, and the people a website named, come from these), its
    Schedule A, its scores, its scope and its type. A few seconds on the full
    data, against minutes for a build."""
    none = "SELECT NULL::text AS crd, NULL::bigint AS n, NULL::numeric AS s WHERE false"
    sa = (f"SELECT crd, COUNT(*) AS n, {_sum('name, title, is_individual')} AS s"
          " FROM schedule_a GROUP BY crd") if _has(conn, "schedule_a") else none
    ps = (f"SELECT crd, COUNT(*) AS n, {_sum('product, score')} AS s FROM product_score"
          " WHERE status = 'scored' GROUP BY crd") if _has(conn, "product_score") else none
    sc = ("SELECT crd, ROW(priority, products, best_score)::text AS t FROM firm_scope"
          if _has(conn, "firm_scope") else "SELECT NULL::text AS crd, NULL::text AS t WHERE false")
    fc = ("SELECT crd, category FROM firm_class" if _has(conn, "firm_class")
          else "SELECT NULL::text AS crd, NULL::text AS category WHERE false")
    rows = conn.execute(f"""
        WITH f AS (SELECT crd, ROW(legal_name, state, city, raum, phone)::text AS t
                   FROM firm_current),
        c AS (SELECT crd, COUNT(*) AS n, {_sum("person_key, person_name, title, kind, value, label,"
                                               " is_role, source, sources, confidence, verify_status")} AS s
              FROM contact_point GROUP BY crd),
        a AS ({sa}), p AS ({ps}), s AS ({sc}), k AS ({fc})
        SELECT f.crd, md5(ROW(f.t, c.n, c.s, a.n, a.s, p.n, p.s, s.t, k.category)::text) AS fp
        FROM f LEFT JOIN c ON c.crd = f.crd LEFT JOIN a ON a.crd = f.crd
        LEFT JOIN p ON p.crd = f.crd LEFT JOIN s ON s.crd = f.crd
        LEFT JOIN k ON k.crd = f.crd""").fetchall()
    return {r["crd"]: r["fp"] for r in rows}


def _older(stamp: str | None, hours: float) -> bool:
    try:
        t = datetime.fromisoformat(stamp or "")
    except ValueError:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - t > timedelta(hours=hours)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _set_state(conn, k: str, v: str) -> None:
    conn.execute("INSERT INTO people_index_state (k, v) VALUES (?, ?)"
                 " ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))


def _count(conn) -> int:
    n = conn.execute("SELECT COUNT(*) n FROM people_index").fetchone()["n"]
    conn.commit()
    return n


def _update(conn, full: bool, info: dict) -> int:
    init(conn)
    keys = score_keys()
    # The fingerprints and the rows made from them are read in one snapshot,
    # so what is saved as built is exactly what was read: a contact found
    # (or removed) while a build runs is left for the next run to see. Only
    # this job writes the index, so the snapshot never meets a write conflict.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    inputs = _inputs(conn, keys)
    firms = _firm_fps(conn)
    state = {r["k"]: r["v"] for r in conn.execute("SELECT k, v FROM people_index_state")}
    old = {r["crd"]: r["fp"] for r in conn.execute("SELECT crd, fp FROM people_index_firm")}
    why = ("asked for" if full else "no index yet" if not exists(conn)
           else "roster, designations, product lists or code changed"
           if state.get("inputs") != inputs
           else "daily full build" if _older(state.get("full_at"), FULL_EVERY_H) else "")
    if not why:
        changed = sorted(c for c in firms.keys() | old.keys() if firms.get(c) != old.get(c))
        if not changed:
            info.update(mode="unchanged", firms=0, written=0)
            return _count(conn)
        written = _refresh(conn, changed, keys, firms)
        if written is not None:
            info.update(mode="refresh", firms=len(changed), written=written)
            return _count(conn)
        why = f"{len(changed):,} firms changed, too many to refresh in place"
    n = _build(conn, keys, inputs, firms)
    info.update(mode="full", why=why, firms=len(firms), written=n)
    return n


def _drop_temp(conn) -> None:
    for t in TEMP + REFRESH_TEMP:
        conn.execute(f"DROP TABLE IF EXISTS {t}")


def _refresh(conn, crds: list[str], keys: list[str], firms: dict[str, str]) -> int | None:
    """Rebuild these firms' rows where they stand: the new rows are made in a
    temporary table, then the firms' old rows are swapped for them and their
    fingerprints saved, all in one transaction, so a reader sees the old rows
    or the new ones and never a mix. Returns the rows written, or None when a
    full build is the better way (too many rows, or the table's columns are
    not what the query makes now); the transaction, and its snapshot, then
    carry on into that build."""
    try:
        conn.execute("DROP TABLE IF EXISTS pi_crds")
        conn.execute("CREATE TEMP TABLE pi_crds (crd TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO pi_crds (crd) VALUES (?)", [(c,) for c in crds])
        have = conn.execute("SELECT COUNT(*) n FROM people_index"
                            " WHERE crd IN (SELECT crd FROM pi_crds)").fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) n FROM people_index").fetchone()["n"]
        if have > REFRESH_MAX_SHARE * max(total, 1):
            _drop_temp(conn)
            return None
        _rows(conn, "pi_stage", keys, only=True)
        live = [r["name"] for r in conn.execute("PRAGMA table_info(people_index)")]
        new = [r["name"] for r in conn.execute("PRAGMA table_info(pi_stage)")]
        if live != new:
            _drop_temp(conn)
            return None
        cols = ", ".join(new)
        conn.execute("DELETE FROM people_index WHERE crd IN (SELECT crd FROM pi_crds)")
        written = conn.execute(f"INSERT INTO people_index ({cols}) SELECT {cols}"
                               " FROM pi_stage").rowcount
        conn.executemany("INSERT INTO people_index_firm (crd, fp) VALUES (?, ?)"
                         " ON CONFLICT (crd) DO UPDATE SET fp = excluded.fp",
                         [(c, firms[c]) for c in crds if c in firms])
        gone = [(c,) for c in crds if c not in firms]
        if gone:
            conn.executemany("DELETE FROM people_index_firm WHERE crd = ?", gone)
        _set_state(conn, "refreshed_at", _now())
        _drop_temp(conn)
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise


def _build(conn, keys: list[str], inputs: str, firms: dict[str, str]) -> int:
    _rows(conn, "people_index_new", keys, only=False)
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
    _drop_temp(conn)
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
    # What this build was made from, saved with the swap: should the swap
    # fail, the next run still sees a stale fingerprint and builds again.
    conn.execute("DELETE FROM people_index_firm")
    items = sorted(firms.items())
    for i in range(0, len(items), 5000):
        conn.executemany("INSERT INTO people_index_firm (crd, fp) VALUES (?, ?)",
                         items[i:i + 5000])
    _set_state(conn, "inputs", inputs)
    _set_state(conn, "full_at", _now())
    conn.execute("DROP TABLE IF EXISTS people_index")
    conn.execute("ALTER TABLE people_index_new RENAME TO people_index")
    conn.commit()
    _COLS.clear()
    return _count(conn)


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
