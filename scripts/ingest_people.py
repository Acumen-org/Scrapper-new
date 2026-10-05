"""Rosters and hiring movement for every tracked firm, from the SEC individual feed.

The IA_INDVL weekly compilation lists every registered investment adviser
representative: where they work now (by firm CRD), every firm they were
registered with before and when, ten years of employment history, exams,
designations and disclosure flags. This loads all of it for anyone who works
at, or used to be registered with, a firm in the firm table, and derives from
it who joined and who left each firm, from where and to where. See
prospect/people.py for the tables and what they can and cannot show.

The run:

  1. Capture this week's feed as an immutable snapshot, unless already held.
     Same manifest and the same no-archive rule as the firm feeds: a week not
     captured is gone. The first record is checked against the structure the
     config declares before anything else trusts the file.
  2. Stream every XML member with iterparse, one <Indvl> at a time, and write
     rows into staging tables with COPY after each member. Nothing holds more
     than one member's rows in memory; the XML is over a gigabyte.
  3. Index the staging tables and compute firm_people_stats from them in SQL.
  4. Rename the staging tables over the live ones in one short transaction.

Every run is a full rebuild from one snapshot, so a rerun gives the same
tables, and readers see either last week's roster or this week's, never half
of each.

    python -m scripts.ingest_people                 capture if new, then load
    python -m scripts.ingest_people --no-fetch      load the newest held snapshot
    python -m scripts.ingest_people --file X.zip    load a local zip (testing)
    python -m scripts.ingest_people --force         reload even if already loaded
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, net, people, runlog, snapshot  # noqa: E402
from prospect.people import DESIGNATION_CODES, DISCLOSURE_KINDS, STAGE  # noqa: E402
from scripts.snapshot_adv_feed import pick_file  # noqa: E402

SOURCE_KEY = "ia_indvl_feed"

# Registrations with one firm that stop and restart within this many days are
# one stint. Re-registrations, lapsed state renewals and a firm moving between
# state and SEC registration all produce gaps like this, and none of them is a
# person leaving.
CHAIN_GAP_DAYS = 90

# How far apart two stints can be and still count as one move. A hire's
# previous firm is the stint that ended closest to the start date, at most two
# years before it or 90 days after (overlap while the paperwork transfers). A
# leaver's next firm is the stint that started closest to the end date, from
# 90 days before it to two years after. Beyond that the gap is a career break,
# and naming a firm from either side of it would be a guess.
SOURCE_BEFORE, SOURCE_AFTER = 730, 90
DEST_BEFORE, DEST_AFTER = 90, 730

TOP_FLOWS = 5          # firms listed in top_sources and top_destinations
FLOW_YEARS = 3         # window those lists cover

PERSON_COLS = ("indvl_pk", "first_name", "middle_name", "last_name", "suffix", "name",
               "other_names", "exams", "designations", "disclosures", "has_disclosure",
               "active_broker", "other_business", "iapd_link", "snapshot_id", "updated_at")
EMP_COLS = ("indvl_pk", "org_pk", "org_name", "kind", "reg_cats", "start_date",
            "end_date", "city", "state")
EVENT_COLS = ("crd", "indvl_pk", "kind", "event_date", "other_org_pk", "other_org_name")

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^(\d{1,2})/(\d{4})$")
_GENON = re.compile(rb'GenOn="(\d{4}-\d{2}-\d{2})"')
_ORG_STOP = {"LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
             "LP", "LLP", "LLLP", "LTD", "LIMITED", "THE", "PLLC", "PC", "PA", "NA",
             "AND"}


# ------------------------------------------------------------------ capture

def check_structure(path: Path, expected: dict) -> None:
    """Assert the first record of the first member against the config.

    A restructure is global, so one record is enough to catch it before a
    gigabyte of XML is parsed into nulls."""
    with zipfile.ZipFile(path) as z:
        members = feed_members(z)
        if not members:
            raise guard.SchemaViolation(f"{path.name}: no XML members in the zip")
        with z.open(members[0]) as fh:
            for event, el in etree.iterparse(fh, events=("start", "end")):
                if event == "start":
                    if el.getparent() is None and el.tag != expected["root_element"]:
                        raise guard.SchemaViolation(
                            f"{members[0]}: root is <{el.tag}>, expected "
                            f"<{expected['root_element']}>. Feed restructured upstream.")
                    continue
                if el.tag == expected["record_element"]:
                    guard.assert_xml_record(el, expected, f"{members[0]} first <{el.tag}>")
                    return
    raise guard.SchemaViolation(
        f"{path.name}: no <{expected['record_element']}> records in {members[0]}")


def capture(conn, cfg) -> None:
    """Snapshot this week's individual feed if it is not already held."""
    src = cfg.source(SOURCE_KEY)
    fetcher = net.Fetcher(cfg.http)
    with runlog.Run(conn, SOURCE_KEY, "snapshot", cfg.stamp) as run:
        manifest = fetcher.json(src["manifest_url"])
        entry = pick_file(manifest, src["wanted"][0]["filename_prefix"])
        filename, published = entry["name"], entry.get("date", "")
        print(f"manifest: {filename}  published {published}  ({entry.get('size')})")
        held = snapshot.held_for_date(conn, SOURCE_KEY, published)
        dest = snapshot.snapshot_path(SOURCE_KEY, published, filename)
        if held is not None and dest.exists():
            print(f"already held: snapshot {held['id']} at {held['rel_path']}")
            run.skip(f"{published} already captured, no action")
            return
        print(f"downloading -> {dest}")
        written = fetcher.download(src["base_url"] + filename, dest)
        print(f"  {written:,} bytes")
        run.rows_in = written
        snap_id, was_new = snapshot.register(conn, SOURCE_KEY, published, dest, cfg.stamp)
        print(f"snapshot id {snap_id} ({'new' if was_new else 'already registered'})")
        check_structure(dest, src["expected_structure"])
        run.note(f"{filename}; {written} bytes")


# ------------------------------------------------------------------ parsing

def feed_members(z: zipfile.ZipFile) -> list[str]:
    """XML members in numeric order (Feeds1, Feeds2 ... Feeds20), so runs
    print and fail in the same order every time."""
    def key(m: str):
        n = re.search(r"(\d+)\.xml$", m, re.I)
        return (int(n.group(1)) if n else 0, m)
    return sorted((m for m in z.namelist() if m.lower().endswith(".xml")), key=key)


def iso(s: str | None) -> str:
    """A feed date as ISO, or '' when missing or not a real date."""
    s = (s or "").strip()
    if not _ISO.match(s):
        return ""
    try:
        date.fromisoformat(s)
    except ValueError:
        return ""
    return s


def month(s: str | None) -> str:
    """Employment history dates are MM/YYYY; stored as the first of the month."""
    m = _MONTH.match((s or "").strip())
    if not m or not 1 <= int(m.group(1)) <= 12:
        return ""
    return f"{m.group(2)}-{int(m.group(1)):02d}-01"


def days(a: str, b: str) -> int:
    """b minus a, in days."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def org_key(name: str | None) -> str:
    """Firm name reduced for comparing history entries with registrations:
    'Ameriprise Financial Services, Inc.' and 'AMERIPRISE FINANCIAL SERVICES,
    LLC' are the same employer under the same CRD."""
    s = (name or "").upper().replace("&", " AND ").replace(".", "")
    return " ".join(t for t in re.split(r"[^A-Z0-9]+", s) if t and t not in _ORG_STOP)


def text(v: str | None) -> str | None:
    """An attribute with its whitespace normalised. The feed pads some firm
    names (' MORGAN STANLEY'), which would otherwise split one firm into two
    in every grouping by name."""
    v = " ".join((v or "").split())
    return v or None


def place(el) -> tuple[str | None, str | None]:
    """The person's branch where the feed gives one, else the employer's office."""
    loc = el.find("BrnchOfLocs/BrnchOfLoc")
    src = loc if loc is not None and text(loc.get("city")) else el
    return text(src.get("city")), text(src.get("state"))


class Stint:
    __slots__ = ("org", "name", "start", "end", "current")

    def __init__(self, org, name, start, end, current):
        self.org, self.name = org, name
        self.start = start        # ISO or ''
        self.end = end            # ISO, '' unknown, None ongoing
        self.current = current


def stints(regs: list[tuple[str, str, str, str | None, bool]]) -> list[Stint]:
    """Chain each firm's registrations into stints. regs are
    (org_pk, org_name, start, end, is_current); end None means ongoing."""
    by_org: dict[str, list] = {}
    for r in regs:
        by_org.setdefault(r[0], []).append(r)
    out: list[Stint] = []
    for org, rs in by_org.items():
        chain: Stint | None = None
        for _o, name, s, e, cur in sorted((r for r in rs if r[2]), key=lambda r: r[2]):
            if e == "" and not cur:
                e = s          # an ended registration with no end date: assume short
            if chain is not None and (chain.end is None or
                                      days(chain.end or chain.start, s) <= CHAIN_GAP_DAYS):
                if chain.end is not None:
                    chain.end = None if e is None else max(chain.end, e)
                chain.current = chain.current or cur
                chain.name = name or chain.name
                continue
            if chain is not None:
                out.append(chain)
            chain = Stint(org, name, s, e, cur)
        if chain is not None:
            out.append(chain)
        for _o, name, s, e, cur in (r for r in rs if not r[2]):
            out.append(Stint(org, name, "", e, cur))
    return out


def came_from(st: Stint, all_stints: list[Stint]) -> Stint | None:
    """The stint at another firm that ended closest to this one's start."""
    best, gap = None, None
    for t in all_stints:
        if t.org == st.org or not t.end:
            continue
        d = days(st.start, t.end)          # positive: ended after the new start
        if -SOURCE_BEFORE <= d <= SOURCE_AFTER and (gap is None or abs(d) < gap):
            best, gap = t, abs(d)
    return best


def went_to(st: Stint, all_stints: list[Stint]) -> Stint | None:
    """The stint at another firm that started closest to this one's end,
    preferring one still current when two are equally close."""
    best, rank = None, None
    for t in all_stints:
        if t.org == st.org or not t.start:
            continue
        d = days(st.end, t.start)          # positive: started after leaving
        if -DEST_BEFORE <= d <= DEST_AFTER:
            r = (abs(d), not t.current)
            if rank is None or r < rank:
                best, rank = t, r
    if best is None:
        # Nothing started near the departure. They are registered somewhere
        # today (everyone in the feed is), so name the latest of those firms.
        now = [t for t in all_stints if t.current and t.org != st.org]
        if now:
            best = max(now, key=lambda t: t.start or "")
    return best


def parse_person(ind, tracked: set[str], snapshot_id: int | None, stamp: str):
    """One <Indvl> to (person row, employment rows, event rows, history rows
    dropped as duplicates), or None when the person has never been registered
    with a tracked firm."""
    info = ind.find("Info")
    if info is None or not info.get("indvlPK"):
        return None
    pk = info.get("indvlPK")

    current, previous = [], []
    for emp in ind.iterfind("CrntEmps/CrntEmp"):
        org = (emp.get("orgPK") or "").strip()
        regs = emp.findall("CrntRgstns/CrntRgstn")
        starts = [d for d in (iso(r.get("stDt")) for r in regs) if d]
        cats = sorted({r.get("regCat") for r in regs if r.get("regCat")})
        city, state = place(emp)
        current.append((org, text(emp.get("orgNm")), min(starts) if starts else "",
                        ",".join(cats) or None, city, state))
    for prv in ind.iterfind("PrevRgstns/PrevRgstn"):
        city, state = place(prv)
        previous.append(((prv.get("orgPK") or "").strip(), text(prv.get("orgNm")),
                         iso(prv.get("regBeginDt")), iso(prv.get("regEndDt")),
                         city, state))

    if not any(c[0] in tracked for c in current) and \
            not any(p[0] in tracked for p in previous):
        return None

    # ---- stints and events
    regs = [(c[0], c[1], c[2], None, True) for c in current]
    regs += [(p[0], p[1], p[2], p[3], False) for p in previous]
    all_stints = stints(regs)
    stint_start = {s.org: s.start for s in all_stints if s.current}

    events = []
    seen_ev = set()
    for st in all_stints:
        if st.org not in tracked:
            continue
        if st.start:
            src = came_from(st, all_stints)
            k = (st.org, "joined", st.start)
            if k not in seen_ev:
                seen_ev.add(k)
                events.append((st.org, pk, "joined", st.start,
                               src.org if src else None, src.name if src else None))
        if st.end:
            dst = went_to(st, all_stints)
            k = (st.org, "left", st.end)
            if k not in seen_ev:
                seen_ev.add(k)
                events.append((st.org, pk, "left", st.end,
                               dst.org if dst else None, dst.name if dst else None))

    # ---- employment rows, deduplicated on the primary key
    emp_rows: dict[tuple, tuple] = {}
    for org, name, start, cats, city, state in current:
        start = stint_start.get(org, start) or start
        emp_rows.setdefault((org, "current", start),
                            (pk, org, name, "current", cats, start, None, city, state))
    for org, name, start, end, city, state in previous:
        key = (org, "previous", start)
        row = (pk, org, name, "previous", None, start, end or None, city, state)
        old = emp_rows.get(key)
        if old is None or (row[6] or "") > (old[6] or ""):
            emp_rows[key] = row
    known = {}
    for org, name, *_ in current + previous:
        if org and org_key(name):
            known.setdefault(org_key(name), org)
    hist_dropped = 0
    for h in ind.iterfind("EmpHss/EmpHs"):
        name = text(h.get("orgNm"))
        k = org_key(name)
        org = known.get(k, "")
        if not org and k:
            # 'MERRILL LYNCH' in history against the registered 'MERRILL LYNCH
            # PIERCE FENNER AND SMITH': a two-word or longer prefix of the
            # person's own registered firm is that firm.
            for kk, o in known.items():
                short, long_ = sorted((k, kk), key=len)
                if len(short.split()) >= 2 and long_.startswith(short + " "):
                    org = o
                    break
        start = month(h.get("fromDt"))
        key = (org, "history", start)
        if key in emp_rows:
            hist_dropped += 1      # two jobs the same month at unregistered employers
            continue
        emp_rows[key] = (pk, org, name, "history", None, start,
                         month(h.get("toDt")) or None, text(h.get("city")),
                         text(h.get("state")))

    # ---- the person
    first, mid, last, suf = people.name_parts(info.get("firstNm"), info.get("midNm"),
                                              info.get("lastNm"), info.get("sufNm"))
    name = " ".join(p for p in (first, mid, last, suf) if p) or pk
    others, seen_names = [], {name.upper()}
    for o in ind.iterfind("OthrNms/OthrNm"):
        dn = people.display_name(o.get("firstNm"), o.get("midNm"), o.get("lastNm"),
                                 o.get("sufNm"))
        if dn and dn.upper() not in seen_names:
            seen_names.add(dn.upper())
            others.append(dn)
    exams = [{"code": e.get("exmCd"), "name": text(e.get("exmNm")),
              "date": iso(e.get("exmDt")) or None}
             for e in ind.iterfind("Exms/Exm") if e.get("exmCd")]
    desig = []
    for d in ind.iterfind("Dsgntns/Dsgntn"):
        nm = (d.get("dsgntnNm") or "").strip()
        code = DESIGNATION_CODES.get(nm, nm)
        if code and code not in desig:
            desig.append(code)
    disc = []
    for drp in ind.iterfind("DRPs/DRP"):
        for attr, word in DISCLOSURE_KINDS.items():
            if (drp.get(attr) or "").upper() == "Y" and word not in disc:
                disc.append(word)
    other_bus = "\n".join(d for d in (b.get("desc", "").strip()
                                      for b in ind.iterfind("OthrBuss/OthrBus")) if d)
    person_row = (
        pk, first or None, mid or None, last or None, suf or None, name,
        json.dumps(others) if others else None,
        json.dumps(exams) if exams else None,
        json.dumps(desig) if desig else None,
        json.dumps(disc) if disc else None,
        1 if disc else 0,
        1 if (info.get("actvAGReg") or "").upper() == "Y" else 0,
        other_bus or None, info.get("link"), snapshot_id, stamp)
    return person_row, list(emp_rows.values()), events, hist_dropped


# --------------------------------------------------------------------- load

def copy_rows(conn, table: str, cols: tuple[str, ...], rows: list[tuple]) -> None:
    """COPY into a staging table over the raw psycopg connection. The wrapper
    speaks sqlite3 and has no COPY; row-at-a-time INSERTs of four million rows
    would take most of an hour."""
    if not rows:
        return
    with conn._conn.cursor() as cur:
        with cur.copy(f"COPY {table}{STAGE} ({', '.join(cols)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)


def create_staging(conn) -> None:
    for t in people.TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}{STAGE}")
    conn.executescript(people.table_ddl(STAGE))
    conn.commit()


STATS_SQL = """
INSERT INTO firm_people_stats{s}
  (crd, headcount, hires_12m, departures_12m, hires_prev_12m, departures_prev_12m,
   net_12m, avg_tenure_years, cfp_count, cfa_count, broker_dual_count,
   disclosure_count, top_sources, top_destinations, computed_at)
WITH tracked AS (SELECT DISTINCT crd FROM firm),
cur AS (
  SELECT e.org_pk AS crd,
         COUNT(*) AS headcount,
         ROUND(AVG(CASE WHEN e.start_date <> ''
                        THEN (?::date - e.start_date::date) / 365.25 END)::numeric, 2)
           AS avg_tenure,
         SUM(CASE WHEN p.designations LIKE '%"CFP"%' THEN 1 ELSE 0 END) AS cfp,
         SUM(CASE WHEN p.designations LIKE '%"CFA"%' THEN 1 ELSE 0 END) AS cfa,
         SUM(p.active_broker) AS broker,
         SUM(p.has_disclosure) AS disc
    FROM person_employment{s} e JOIN person{s} p ON p.indvl_pk = e.indvl_pk
   WHERE e.kind = 'current' AND e.org_pk IN (SELECT crd FROM tracked)
   GROUP BY e.org_pk),
ev AS (
  SELECT crd,
         COUNT(DISTINCT indvl_pk) FILTER (WHERE kind = 'joined'
                                            AND event_date > ? AND event_date <= ?) AS h12,
         COUNT(DISTINCT indvl_pk) FILTER (WHERE kind = 'left'
                                            AND event_date > ? AND event_date <= ?) AS d12,
         COUNT(DISTINCT indvl_pk) FILTER (WHERE kind = 'joined'
                                            AND event_date > ? AND event_date <= ?) AS h24,
         COUNT(DISTINCT indvl_pk) FILTER (WHERE kind = 'left'
                                            AND event_date > ? AND event_date <= ?) AS d24
    FROM people_event{s}
   WHERE event_date > ?
   GROUP BY crd),
flows AS (
  SELECT crd, kind, other_org_pk, MAX(other_org_name) AS org_name,
         COUNT(DISTINCT indvl_pk) AS n,
         ROW_NUMBER() OVER (PARTITION BY crd, kind
                            ORDER BY COUNT(DISTINCT indvl_pk) DESC, MAX(other_org_name)) AS rn
    FROM people_event{s}
   WHERE other_org_pk IS NOT NULL AND event_date > ? AND event_date <= ?
   GROUP BY crd, kind, other_org_pk),
tops AS (
  SELECT crd,
         json_agg(json_build_object('org_pk', other_org_pk, 'org_name', org_name, 'n', n)
                  ORDER BY rn) FILTER (WHERE kind = 'joined') AS src,
         json_agg(json_build_object('org_pk', other_org_pk, 'org_name', org_name, 'n', n)
                  ORDER BY rn) FILTER (WHERE kind = 'left') AS dst
    FROM flows WHERE rn <= ?
   GROUP BY crd)
SELECT k.crd,
       COALESCE(c.headcount, 0),
       COALESCE(ev.h12, 0), COALESCE(ev.d12, 0),
       COALESCE(ev.h24, 0), COALESCE(ev.d24, 0),
       COALESCE(ev.h12, 0) - COALESCE(ev.d12, 0),
       c.avg_tenure,
       COALESCE(c.cfp, 0), COALESCE(c.cfa, 0), COALESCE(c.broker, 0), COALESCE(c.disc, 0),
       COALESCE(t.src::text, '[]'), COALESCE(t.dst::text, '[]'),
       ?
  FROM (SELECT crd FROM cur UNION SELECT crd FROM ev UNION SELECT crd FROM tops) k
  LEFT JOIN cur c ON c.crd = k.crd
  LEFT JOIN ev ON ev.crd = k.crd
  LEFT JOIN tops t ON t.crd = k.crd
"""


def compute_stats(conn, as_of: date) -> int:
    """firm_people_stats for every tracked firm with people or movement.

    Windows are measured back from the feed's own date rather than the day
    the job happens to run, so the numbers describe the snapshot."""
    d0 = as_of.isoformat()
    d1 = (as_of - timedelta(days=365)).isoformat()
    d2 = (as_of - timedelta(days=730)).isoformat()
    d3 = (as_of - timedelta(days=round(365.25 * FLOW_YEARS))).isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(STATS_SQL.replace("{s}", STAGE), (
        d0,
        d1, d0, d1, d0, d2, d1, d2, d1,
        d2,
        d3, d0,
        TOP_FLOWS,
        now))
    return cur.rowcount


def swap(conn) -> None:
    """Rename the staging tables over the live ones, atomically.

    The lock this needs is brief, but it waits behind any open reader. A lock
    timeout keeps a long-running page query from turning the wait into a
    queue that stalls every other request; the swap backs off and retries."""
    raw = conn._conn
    conn.commit()
    for attempt in range(1, 7):
        try:
            with raw.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
                for t in people.TABLES:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                    cur.execute(f"ALTER TABLE {t}{STAGE} RENAME TO {t}")
                # Index, constraint and sequence names still carry the staging
                # suffix. Renamed back so the next run's staging names are free
                # and the live names match what people.init would create.
                for t in people.TABLES:
                    cur.execute("SELECT indexname FROM pg_indexes"
                                " WHERE schemaname = current_schema() AND tablename = %s",
                                (t,))
                    for (ix,) in [tuple(r.values()) for r in cur.fetchall()]:
                        if STAGE in ix:
                            cur.execute(f'ALTER INDEX "{ix}" RENAME TO "{ix.replace(STAGE, "")}"')
                cur.execute("SELECT pg_get_serial_sequence('people_event', 'id') AS seq")
                row = cur.fetchone()
                seq = row["seq"] if row else None
                if seq and STAGE in seq:
                    cur.execute(f"ALTER SEQUENCE {seq} RENAME TO "
                                f"{seq.split('.')[-1].replace(STAGE, '')}")
            raw.commit()
            return
        except psycopg.errors.LockNotAvailable:
            raw.rollback()
            print(f"  swap waiting on readers (attempt {attempt})")
            time.sleep(5 * attempt)
    raise RuntimeError("could not lock the people tables for the swap after six "
                       "attempts; staging tables are left in place for the next run")


def load(conn, path: Path, tracked: set[str], snapshot_id: int | None,
         expected: dict) -> dict:
    """Parse every member into the staging tables. Returns counters."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = {"records": 0, "people": 0, "employment": 0, "events": 0, "hist_dropped": 0,
         "duplicates": 0}
    feed_date = None
    # An individual listed twice would break the COPY on the primary key and
    # fail the whole run, so a second listing is skipped and counted instead.
    seen: set[str] = set()
    with zipfile.ZipFile(path) as z:
        members = feed_members(z)
        guard.require_rows(len(members), f"{path.name} XML members")
        with z.open(members[0]) as fh:
            m = _GENON.search(fh.read(4096))
            feed_date = m.group(1).decode() if m else None
        checked = False
        for member in members:
            t0 = time.monotonic()
            p_rows, e_rows, v_rows = [], [], []
            recs = 0
            with z.open(member) as fh:
                for _, ind in etree.iterparse(fh, events=("end",), tag=expected["record_element"]):
                    if not checked:
                        guard.assert_xml_record(ind, expected, f"{member} first <Indvl>")
                        checked = True
                    recs += 1
                    got = parse_person(ind, tracked, snapshot_id, stamp)
                    if got is not None and got[0][0] in seen:
                        n["duplicates"] += 1
                        got = None
                    if got is not None:
                        prow, erows, evs, dropped = got
                        seen.add(prow[0])
                        p_rows.append(prow)
                        e_rows.extend(erows)
                        v_rows.extend(evs)
                        n["hist_dropped"] += dropped
                    ind.clear()
                    while ind.getprevious() is not None:
                        del ind.getparent()[0]
            guard.require_rows(recs, member, "a feed member with no individuals is a "
                                             "truncated or restructured file")
            t1 = time.monotonic()
            copy_rows(conn, "person", PERSON_COLS, p_rows)
            copy_rows(conn, "person_employment", EMP_COLS, e_rows)
            copy_rows(conn, "people_event", EVENT_COLS, v_rows)
            conn.commit()
            n["records"] += recs
            n["people"] += len(p_rows)
            n["employment"] += len(e_rows)
            n["events"] += len(v_rows)
            print(f"  {member:<24} {recs:>7,} records  {len(p_rows):>7,} people"
                  f"  {len(e_rows):>8,} jobs  {len(v_rows):>7,} events"
                  f"  parse {t1 - t0:4.1f}s  write {time.monotonic() - t1:4.1f}s")
    n["feed_date"] = feed_date
    return n


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="ingest this local IA_INDVL zip instead of a snapshot")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the SEC manifest and load the newest held snapshot")
    ap.add_argument("--force", action="store_true",
                    help="reload even when this snapshot is already loaded")
    args = ap.parse_args()

    cfg = config.load()
    config.ensure_dirs()
    src = cfg.source(SOURCE_KEY)
    conn = db.connect()
    db.init(conn)
    people.init(conn)

    if args.file:
        path, snap_id = Path(args.file), None
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 1
    else:
        if not args.no_fetch:
            capture(conn, cfg)
        snap = snapshot.latest(conn, SOURCE_KEY)
        if snap is None:
            print("no individual feed snapshot held; run without --no-fetch",
                  file=sys.stderr)
            return 1
        snap_id = int(snap["id"])
        path = snapshot.resolve(conn, snap_id)
        if not path.exists():
            print(f"snapshot {snap_id} is registered but its file is missing: {path}",
                  file=sys.stderr)
            return 1
        loaded = conn.execute("SELECT snapshot_id FROM person LIMIT 1").fetchone()
        if loaded and loaded["snapshot_id"] == snap_id and not args.force:
            print(f"snapshot {snap_id} ({snap['published_at']}) already loaded; "
                  "--force to rebuild")
            return 0

    started = time.monotonic()
    with runlog.Run(conn, "people", "ingest", cfg.stamp) as run:
        tracked = {r["crd"] for r in conn.execute("SELECT DISTINCT crd FROM firm")}
        guard.require_rows(len(tracked), "firm table",
                           "people are scoped to tracked firms; ingest firms first")
        print(f"{path.name}: tracked firms {len(tracked):,}")

        create_staging(conn)
        n = load(conn, path, tracked, snap_id, src["expected_structure"])
        guard.require_rows(n["people"], f"{path.name} people at tracked firms")
        run.rows_in = n["records"]

        print("indexing ...")
        conn.executescript(people.index_ddl(STAGE))
        conn.commit()
        as_of = date.fromisoformat(n["feed_date"]) if n["feed_date"] else date.today()
        n_stats = compute_stats(conn, as_of)
        print(f"firm_people_stats: {n_stats:,} firms (as of {as_of})")
        for t in people.TABLES:
            conn.execute(f"ANALYZE {t}{STAGE}")
        conn.commit()
        swap(conn)

        rc = src.get("row_count", {})
        if n["records"] < (rc.get("min_plausible") or 0):
            run.flag(f"feed has {n['records']} records, below plausible floor "
                     f"{rc.get('min_plausible')}")
        run.check_row_delta(n["people"], rc.get("warn_pct_change", 5.0))
        run.note(f"{path.name} (feed {n['feed_date']}): {n['people']} people, "
                 f"{n['employment']} employment rows, {n['events']} events, "
                 f"{n_stats} firms")
        if run.flagged:
            print(f"FLAGGED: {run.message}")

    kinds = conn.execute("SELECT kind, COUNT(*) AS n FROM person_employment"
                         " GROUP BY kind ORDER BY kind").fetchall()
    evk = conn.execute("SELECT kind, COUNT(*) AS n FROM people_event"
                       " GROUP BY kind ORDER BY kind").fetchall()
    print(f"\n{n['records']:,} feed records -> {n['people']:,} people at tracked firms")
    print("employment: " + ", ".join(f"{r['kind']} {r['n']:,}" for r in kinds)
          + f" (history rows dropped as same-month duplicates: {n['hist_dropped']:,})")
    if n["duplicates"]:
        print(f"individuals listed twice in the feed, second copy skipped: {n['duplicates']:,}")
    print("events: " + ", ".join(f"{r['kind']} {r['n']:,}" for r in evk))
    print(f"done in {time.monotonic() - started:.0f}s; config {cfg.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
