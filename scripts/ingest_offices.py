"""Office phone lines for every person, and the social pages firms file.

The owner's rule is that every person at a firm ends up with a real way to
reach them. Most advisers publish no direct line, but every SEC-registered
firm files its other offices on Form ADV Schedule D 1.F with a street address
and a telephone number, and the individual feed says which city each person's
registration places them in. Joining the two gives each person the line of
the office they actually work from, which is a far better first call than the
headquarters switchboard 2,000 miles away. Where no filed office matches,
the firm's main number (Item 1.F) is the way in, labelled as such.

Firms also file their websites and social pages: Schedule D 1.I in the
archive, and Item 1.I in the weekly feed, which is current. LinkedIn company
pages from either become the firm's LinkedIn; a personal profile a firm filed
is given to the roster person its URL spells.

Steps:
  1. offices: Schedule D 1.F and 1.I are range-fetched out of the 701MB part 1
     archive (47MB and 22MB packed), inflated straight to a gzip snapshot, and
     reduced to each firm's LATEST filing that carries Schedule D section 1
     (FilingIDs grow in submission order). A firm whose newest filing lists no
     other offices has closed them; older offices are not carried forward.
     Loaded once; the archive ends 2024-12-31 and does not change.
  2. social: the current weekly feeds' WebAddr lists are read too, so firms
     registered since 2024 and state-registered firms are covered.
  3. attach: one pass over every current employment, set-based in batches.
     Re-run only when the roster, the firm feeds or this logic change (the
     stamp in office_phone_state), so the weekly job is a no-op otherwise.

Every row carries where it came from in source_ref, and rows this job no
longer derives (a person who left, an office that closed) are removed unless
another source also reported the same value.

    python -m scripts.ingest_offices [--reload] [--force]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import time
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import (config, contacts, db, emailguess, guard, harvest,  # noqa: E402
                      runlog, snapshot)
from prospect.names import nice_name  # noqa: E402

OFFICES_KEY = "schedule_d_offices"
WEB_KEY = "schedule_d_web"
ARCHIVE_DATE = "2024-12-31"

# Bump when the attach rules change, so the next run redoes every person.
ATTACH_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_office (
    crd               TEXT NOT NULL,
    filing_id         TEXT NOT NULL,
    filing_date       TEXT,               -- NULL where the crosswalk carries no date
    street            TEXT,
    city              TEXT,
    state             TEXT,
    postal_code       TEXT,
    country           TEXT,
    phone             TEXT,               -- (555) 555-5555; NULL if not a usable US number
    fax               TEXT,
    employees         INTEGER,
    private_residence INTEGER,            -- the office is someone's home
    branch_number     TEXT,
    place_key         TEXT                -- CITY|ST, normalised, what people match on
);
CREATE INDEX IF NOT EXISTS ix_office_crd ON firm_office (crd, place_key);
CREATE TABLE IF NOT EXISTS firm_social (
    crd          TEXT NOT NULL,
    url          TEXT NOT NULL,           -- canonical for LinkedIn, else as filed
    kind         TEXT NOT NULL,           -- linkedin | facebook | twitter | instagram | youtube | ...
    filing_date  TEXT,
    source       TEXT NOT NULL,           -- adv_feed (weekly, current) | adv_archive (to 2024)
    PRIMARY KEY (crd, url)
);
CREATE INDEX IF NOT EXISTS ix_social_kind ON firm_social (kind);
CREATE TABLE IF NOT EXISTS office_phone_state (
    k        TEXT PRIMARY KEY,
    stamp    TEXT,
    done_at  TEXT,
    note     TEXT
);
"""

# What the attach step was last run against. The weekly job's backlog query
# in prospect/jobs.py repeats this expression; keep the two identical.
STAMP_SQL = ("SELECT '" + ATTACH_VERSION + "/' ||"
             " (SELECT COALESCE(MAX(id), 0) FROM snapshot WHERE source_key IN"
             " ('adv_feed','adv_state_feed','ia_indvl_feed'))::text || '/' ||"
             " (SELECT COUNT(*) FROM firm_office)::text || '/' ||"
             " (SELECT COUNT(*) FROM firm_social)::text AS stamp")

SOCIAL_KINDS = (("linkedin.com", "linkedin"), ("facebook.com", "facebook"),
                ("twitter.com", "twitter"), ("x.com", "twitter"),
                ("instagram.com", "instagram"), ("youtube.com", "youtube"),
                ("youtu.be", "youtube"), ("tiktok.com", "tiktok"),
                ("threads.net", "threads"), ("vimeo.com", "vimeo"),
                ("medium.com", "medium"), ("pinterest.com", "pinterest"))

csv.field_size_limit(1 << 24)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ helpers

_PLACE_WORDS = {"ST": "SAINT", "STE": "SAINTE", "FT": "FORT", "MT": "MOUNT",
                "PT": "POINT", "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
                "TWP": "TOWNSHIP", "HTS": "HEIGHTS", "SPGS": "SPRINGS"}


def place_key(city: str | None, state: str | None) -> str | None:
    """'St. Louis', 'SAINT LOUIS' and 'St Louis' are one place: upper case,
    punctuation gone, the common abbreviations spelled out."""
    c = re.sub(r"[^A-Z0-9 ]", " ", (city or "").upper())
    words = [_PLACE_WORDS.get(w, w) for w in c.split()]
    st = (state or "").strip().upper()
    if not words or len(st) != 2:
        return None
    return " ".join(words) + "|" + st


def social_kind(url: str) -> str | None:
    host = (url or "").lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/", 1)[0]
    for h, kind in SOCIAL_KINDS:
        if host == h or host.endswith("." + h):
            return kind
    return None


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _where(city: str | None, state: str | None) -> str:
    c = nice_name(city or "").strip()
    return f"{c}, {state}" if c and state else (c or state or "an unnamed place")


def _filed(filing_id: str | None, filing_date: str | None) -> str:
    if filing_date:
        return f"filed {filing_date}"
    return f"filing {filing_id}, archive to {ARCHIVE_DATE}" if filing_id else "current filing"


# ------------------------------------------------------------------ fetch

def _read_exact(resp, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = resp.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def fetch_member(cfg, src, dest: Path) -> int:
    """Range-fetch one member of the part 1 archive and inflate it straight
    into a gzip file, never holding the 200MB CSV in memory. The configured
    offset is asserted the same way the crosswalk's is: it must land on a zip
    local header naming exactly this member, and the deflate stream must end
    where the central directory said it would."""
    rng = src["range_fetch"]
    off, comp = int(rng["local_offset"]), int(rng["compressed_bytes"])
    req = urllib.request.Request(src["url"], headers={
        "User-Agent": cfg.http["user_agent"],
        "Range": f"bytes={off}-{off + comp + 1024}",
    })
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=cfg.http["timeout_seconds"]) as resp:
        head = _read_exact(resp, 30)
        if head[:4] != b"PK\x03\x04":
            raise guard.SchemaViolation(
                f"{src['member']}: offset {off} is not a zip local header; the "
                "upstream archive was rebuilt and the configured offset is stale.")
        nlen = int.from_bytes(head[26:28], "little")
        elen = int.from_bytes(head[28:30], "little")
        name = _read_exact(resp, nlen).decode("utf-8", "replace").split("/")[-1]
        if name != src["member"]:
            raise guard.SchemaViolation(
                f"offset {off} resolves to '{name}', expected '{src['member']}'.")
        _read_exact(resp, elen)
        inflate = zlib.decompressobj(-zlib.MAX_WBITS)
        left = comp
        size = 0
        with gzip.open(tmp, "wb", compresslevel=5) as out:
            while left > 0 and not inflate.eof:
                chunk = resp.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                data = inflate.decompress(chunk)
                size += len(data)
                out.write(data)
            tail = inflate.flush()
            size += len(tail)
            out.write(tail)
    if not inflate.eof:
        tmp.unlink(missing_ok=True)
        raise guard.SchemaViolation(
            f"{src['member']}: the deflate stream ended early ({comp - left} of {comp}"
            " bytes); the download was cut short or the offsets are stale.")
    tmp.replace(dest)
    return size


def member_file(conn, cfg, key: str, reload: bool) -> tuple[Path, int]:
    """The snapshot of one archive member, fetched only when not held."""
    src = cfg.source(key)
    dest = snapshot.snapshot_path(key, ARCHIVE_DATE, src["member"] + ".gz")
    if reload or not dest.exists():
        print(f"range-fetching {src['member']} ...", flush=True)
        t0 = time.monotonic()
        size = fetch_member(cfg, src, dest)
        print(f"  inflated {size:,} bytes in {time.monotonic() - t0:.0f}s", flush=True)
    snap_id, _ = snapshot.register(conn, key, ARCHIVE_DATE, dest, cfg.stamp)
    return dest, snap_id


def _rows(path: Path):
    fh = io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", errors="replace",
                          newline="")
    return csv.DictReader(fh)


# ------------------------------------------------------------------ load

def load_archive(conn, cfg, reload: bool) -> dict:
    """firm_office and the archive half of firm_social, from each firm's
    newest filing that carries Schedule D section 1."""
    f_path, f_snap = member_file(conn, cfg, OFFICES_KEY, reload)
    w_path, w_snap = member_file(conn, cfg, WEB_KEY, reload)
    f_src, w_src = cfg.source(OFFICES_KEY), cfg.source(WEB_KEY)

    fmap = {r["filing_id"]: (r["crd"], r["filing_date"]) for r in conn.execute(
        "SELECT filing_id, crd, filing_date FROM filing_crd")}
    conn.commit()
    guard.require_rows(len(fmap), "filing_crd crosswalk",
                       "load it first: python -m scripts.ingest_schedule_d")

    def newest(key, path, src, snap_id, keep) -> tuple[dict, dict, int]:
        rdr = _rows(path)
        cols = list(rdr.fieldnames or [])
        guard.require_columns(cols, src["required_columns"], src["member"])
        guard.record_columns(conn, key, src["member"], cols, snap_id)
        best: dict[str, int] = {}
        rows: dict[str, list] = {}
        n = 0
        for rec in rdr:
            n += 1
            fid = (rec.get("FilingID") or "").strip()
            hit = fmap.get(fid)
            if hit is None:
                continue
            crd = hit[0]
            f = int(fid)
            if f < best.get(crd, 0):
                continue
            if f > best.get(crd, 0):
                best[crd] = f
                rows[crd] = []
            got = keep(rec)
            if got is not None:
                rows[crd].append(got)
        return best, rows, n

    def office(rec):
        street = ", ".join(x for x in (" ".join((rec.get("Street 1") or "").split()),
                                       " ".join((rec.get("Street 2") or "").split())) if x)
        return (street or None, (rec.get("City") or "").strip() or None,
                (rec.get("State") or "").strip().upper() or None,
                (rec.get("Postal Code") or "").strip() or None,
                (rec.get("Country") or "").strip() or None,
                contacts.norm_phone(rec.get("Telephone Number") or ""),
                contacts.norm_phone(rec.get("Facsimile Number") or ""),
                _int(rec.get("Employees")),
                1 if (rec.get("Private Residence") or "").strip().upper() == "Y" else 0,
                (rec.get("Branch Number") or "").strip() or None)

    def website(rec):
        url = (rec.get("Website") or "").strip()
        return url if social_kind(url) else None

    t0 = time.monotonic()
    f_best, f_rows, f_n = newest(OFFICES_KEY, f_path, f_src, f_snap, office)
    w_best, w_rows, w_n = newest(WEB_KEY, w_path, w_src, w_snap, website)
    print(f"  1F: {f_n:,} rows, 1I: {w_n:,} rows read in {time.monotonic() - t0:.0f}s",
          flush=True)
    # The newest filing with any section 1 schedule is the firm's current
    # word. Offices or pages that only an older filing lists are gone.
    latest = {crd: max(f_best.get(crd, 0), w_best.get(crd, 0))
              for crd in set(f_best) | set(w_best)}
    offices = []
    for crd, rs in f_rows.items():
        fid = f_best[crd]
        if fid != latest[crd]:
            continue
        fdate = fmap.get(str(fid), (None, None))[1]
        for (street, city, state, postal, country, phone, fax, emp, home, branch) in rs:
            offices.append((crd, str(fid), fdate, street, city, state, postal, country,
                            phone, fax, emp, home, branch, place_key(city, state)))
    socials = {}
    for crd, rs in w_rows.items():
        fid = w_best[crd]
        if fid != latest[crd]:
            continue
        fdate = fmap.get(str(fid), (None, None))[1]
        for url in rs:
            kind = social_kind(url)
            canon = contacts.norm_linkedin(url) if kind == "linkedin" else None
            u = canon or url
            socials[(crd, u)] = (crd, u, kind, fdate or ARCHIVE_DATE, "adv_archive")
    guard.require_rows(len(offices), f"{f_src['member']} offices on latest filings",
                       "every multi-office adviser files 1.F; zero means the parse broke")

    conn.execute("DELETE FROM firm_office")
    sql = ("INSERT INTO firm_office (crd, filing_id, filing_date, street, city, state,"
           " postal_code, country, phone, fax, employees, private_residence, branch_number,"
           " place_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    for i in range(0, len(offices), 5000):
        conn.executemany(sql, offices[i:i + 5000])
    conn.execute("DELETE FROM firm_social WHERE source='adv_archive'")
    vals = list(socials.values())
    for i in range(0, len(vals), 5000):
        conn.executemany("INSERT INTO firm_social (crd, url, kind, filing_date, source)"
                         " VALUES (?,?,?,?,?) ON CONFLICT (crd, url) DO NOTHING",
                         vals[i:i + 5000])
    conn.commit()
    firms = len({o[0] for o in offices})
    phones = sum(1 for o in offices if o[8])
    print(f"  firm_office: {len(offices):,} offices at {firms:,} firms ({phones:,} with a"
          f" usable phone); firm_social from archive: {len(vals):,}", flush=True)
    return {"offices": len(offices), "office_firms": firms, "archive_social": len(vals),
            "rows_in": f_n + w_n}


def load_feed_social(conn) -> int:
    """Item 1.I web addresses from the current weekly feeds: the firm's word
    as of this week, so it replaces the feed rows from the last run."""
    from lxml import etree
    rows = {}
    for source in ("adv_feed", "adv_state_feed"):
        snap = snapshot.latest(conn, source)
        conn.commit()
        if snap is None:
            continue
        path = config.SNAPSHOT_DIR / snap["rel_path"]
        if not path.exists():
            print(f"  {source}: snapshot file missing ({path.name}), skipped")
            continue
        n = 0
        for _, fm in etree.iterparse(gzip.open(path, "rb"), events=("end",), tag="Firm"):
            info = fm.find("Info")
            crd = info.get("FirmCrdNb") if info is not None else None
            filing = fm.find("Filing")
            fdate = filing.get("Dt") if filing is not None else None
            for w in fm.findall("./FormInfo/Part1A/Item1/WebAddrs/WebAddr"):
                url = (w.text or "").strip()
                kind = social_kind(url)
                if not crd or not kind:
                    continue
                u = (contacts.norm_linkedin(url) if kind == "linkedin" else None) or url
                rows[(crd, u)] = (crd, u, kind, fdate, "adv_feed")
                n += 1
            fm.clear()
        print(f"  {source}: {n:,} social pages listed", flush=True)
    if not rows:
        return 0
    conn.execute("DELETE FROM firm_social WHERE source='adv_feed'")
    vals = list(rows.values())
    sql = ("INSERT INTO firm_social (crd, url, kind, filing_date, source) VALUES (?,?,?,?,?)"
           " ON CONFLICT (crd, url) DO UPDATE SET filing_date=excluded.filing_date,"
           " source=excluded.source")
    for i in range(0, len(vals), 5000):
        conn.executemany(sql, vals[i:i + 5000])
    conn.commit()
    return len(vals)


# ------------------------------------------------------------------ attach

def _titles(conn) -> dict:
    """(crd, LAST, FIRST) -> title, from Schedule A officers. Filed as
    'LAST, FIRST MIDDLE', one row per title held."""
    out: dict = defaultdict(list)
    for r in conn.execute("SELECT crd, name, title FROM schedule_a"
                          " WHERE is_individual=1 AND title IS NOT NULL"):
        last, _, rest = (r["name"] or "").partition(",")
        first = rest.split()[0] if rest.split() else ""
        k = (r["crd"], re.sub(r"[^A-Z]", "", last.upper()), re.sub(r"[^A-Z]", "", first.upper()))
        t = nice_name(r["title"])
        if t and t not in out[k]:
            out[k].append(t)
    conn.commit()
    return {k: "; ".join(v) for k, v in out.items()}


def attach_phones(conn) -> dict:
    """Give every current employment at a firm with any filed phone a line:
    the office in their city, else the main number. Set-based in batches."""
    firms = {}
    for r in conn.execute("SELECT crd, phone, city, state, business_name FROM firm_current"):
        firms[r["crd"]] = (contacts.norm_phone(r["phone"] or ""),
                           place_key(r["city"], r["state"]), r["city"], r["state"])
    offices: dict = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT * FROM firm_office WHERE phone IS NOT NULL"
                          " AND place_key IS NOT NULL"):
        offices[r["crd"]][r["place_key"]].append(dict(r))
    titles = _titles(conn)
    conn.commit()

    stats = defaultdict(int)
    batch: list[dict] = []
    cur = conn.execute(
        "SELECT e.indvl_pk, e.org_pk, e.city, e.state, p.name, p.first_name, p.last_name"
        "  FROM person_employment e JOIN person p ON p.indvl_pk = e.indvl_pk"
        " WHERE e.kind = 'current' ORDER BY e.org_pk, e.indvl_pk")
    for r in cur:
        stats["employments"] += 1
        crd = r["org_pk"]
        f = firms.get(crd)
        if f is None:
            continue
        main, main_place, main_city, main_state = f
        pk = place_key(r["city"], r["state"])
        where = _where(r["city"], r["state"])
        phone = label = ref = None
        conf = 55
        if main and pk and pk == main_place:
            phone, label, conf = main, "main", 60
            ref = (f"Form ADV main office line; the principal office is in {where},"
                   f" where this person's registration places them")
        else:
            cands = offices.get(crd, {}).get(pk or "", [])
            if cands:
                distinct = {o["phone"] for o in cands}
                o = max(cands, key=lambda x: (x["employees"] or -1, x["phone"] == main))
                phone = o["phone"]
                label = "main" if phone == main else "office"
                conf = 70 if len(distinct) == 1 else 60
                addr = ", ".join(x for x in (nice_name(o["street"] or ""), where) if x)
                more = (f"; one of {len(distinct)} offices filed in {where}"
                        if len(distinct) > 1 else "")
                ref = (f"Form ADV Schedule D 1.F: office at {addr}"
                       f" ({_filed(o['filing_id'], o['filing_date'])}){more}")
            elif main:
                phone, label, conf = main, "main", 55
                ref = (f"Form ADV main office line, {_where(main_city, main_state)};"
                       f" no office filed in {where}, where this person is registered")
        if not phone:
            stats["no_phone"] += 1
            continue
        stats[label] += 1
        if label == "office" and contacts.phone_label(re.sub(r"\D", "", phone)[:10]):
            # An 800 number filed for a branch is a service desk, not the
            # branch's own line. The main number keeps 'main' even when it is
            # toll free, as it does on the firm itself.
            label = "toll_free"
        title = titles.get((crd, re.sub(r"[^A-Z]", "", (r["last_name"] or "").upper()),
                            re.sub(r"[^A-Z]", "", (r["first_name"] or "").upper())))
        batch.append({"crd": crd, "kind": "phone", "value": phone, "source": "adv_office",
                      "person_key": f"i:{r['indvl_pk']}", "person_name": r["name"],
                      "title": title, "label": label, "source_ref": ref,
                      "confidence": conf})
        if len(batch) >= 5000:
            contacts.bulk_upsert(conn, batch)
            conn.commit()
            stats["written"] += len(batch)
            batch.clear()
    if batch:
        contacts.bulk_upsert(conn, batch)
        conn.commit()
        stats["written"] += len(batch)
    return dict(stats)


def _roster(conn, crds: set[str]) -> dict:
    """crd -> [(person_key, display name, title)] for roster people and the
    Schedule A officers no roster record covers."""
    out: dict = defaultdict(list)
    if not crds:
        return out
    crd_list = sorted(crds)
    for i in range(0, len(crd_list), 1000):
        chunk = crd_list[i:i + 1000]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT e.org_pk, e.indvl_pk, p.name FROM person_employment e"
                " JOIN person p ON p.indvl_pk=e.indvl_pk"
                f" WHERE e.kind='current' AND e.org_pk IN ({marks})", chunk):
            out[r["org_pk"]].append((f"i:{r['indvl_pk']}", r["name"], None))
        for r in conn.execute(
                "SELECT crd, name, title FROM schedule_a WHERE is_individual=1"
                f" AND crd IN ({marks})", chunk):
            name = emailguess.pretty(r["name"])
            if any(harvest.name_fit(name, n) == "strong" for _, n, _ in out[r["crd"]]):
                continue
            key = contacts.name_key(name)
            if key:
                out[r["crd"]].append((key, name, nice_name(r["title"] or "") or None))
    conn.commit()
    return out


def attach_social(conn) -> dict:
    """Filed LinkedIn pages into contact_point: company pages as the firm's,
    personal profiles to the one roster person (or officer) the slug spells."""
    current = {r["crd"] for r in conn.execute("SELECT crd FROM firm_current")}
    rows = conn.execute("SELECT crd, url, filing_date, source FROM firm_social"
                        " WHERE kind='linkedin'").fetchall()
    conn.commit()
    rows = [r for r in rows if r["crd"] in current]
    person_crds = {r["crd"] for r in rows if "/in/" in r["url"]}
    roster = _roster(conn, person_crds)
    out, stats = [], defaultdict(int)
    for r in rows:
        url = contacts.norm_linkedin(r["url"])
        if not url:
            continue
        where = ("Form ADV Item 1.I, current filing" if r["source"] == "adv_feed"
                 else "Form ADV Schedule D 1.I (archive)")
        ref = f"{where}, filed {r['filing_date']}" if r["filing_date"] else where
        if "/company/" in url:
            out.append({"crd": r["crd"], "kind": "linkedin", "value": url,
                        "source": "adv_social", "source_ref": ref, "confidence": 90,
                        "verify_status": "matched"})
            stats["company"] += 1
            continue
        fits = {}
        for key, name, title in roster.get(r["crd"], []):
            f = harvest.linkedin_slug_fit(url, name)
            if f:
                fits.setdefault(f, {})[key] = (name, title)
        strong, weak = fits.get("strong", {}), fits.get("weak", {})
        if len(strong) == 1:
            (key, (name, title)), = strong.items()
            status, conf = "matched", 90
        elif not strong and len(weak) == 1:
            (key, (name, title)), = weak.items()
            status, conf = "probable", 70
        else:
            stats["person_unmatched"] += 1
            continue
        out.append({"crd": r["crd"], "kind": "linkedin", "value": url, "source": "adv_social",
                    "person_key": key, "person_name": name, "title": title,
                    "source_ref": f"{ref}; the firm listed this profile", "confidence": conf,
                    "verify_status": status})
        stats["person_" + status] += 1
    contacts.bulk_upsert(conn, out)
    conn.commit()
    return dict(stats)


def retire(conn, ts: str) -> int:
    """Rows only this job vouched for and this run did not derive again."""
    cur = conn.execute("DELETE FROM contact_point WHERE source IN ('adv_office','adv_social')"
                       " AND sources = source AND updated_at < ?", (ts,))
    n = max(cur.rowcount or 0, 0)
    conn.commit()
    return n


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reload", action="store_true",
                    help="fetch the archive members again and rebuild firm_office")
    ap.add_argument("--force", action="store_true",
                    help="redo the attach step even if nothing changed")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    db.init(conn)
    conn.executescript(SCHEMA)
    contacts.init(conn)
    conn.commit()

    with runlog.Run(conn, OFFICES_KEY, "ingest", cfg.stamp) as run:
        t0 = time.monotonic()
        notes = []
        have = conn.execute("SELECT COUNT(*) n FROM firm_office").fetchone()["n"]
        conn.commit()
        if args.reload or not have:
            got = load_archive(conn, cfg, args.reload)
            run.rows_in = got["rows_in"]
            notes.append(f"{got['offices']} offices at {got['office_firms']} firms")
        feed = load_feed_social(conn)
        notes.append(f"{feed} social pages from the weekly feeds")

        stamp = conn.execute(STAMP_SQL).fetchone()["stamp"]
        prev = conn.execute("SELECT stamp FROM office_phone_state WHERE k='attach'").fetchone()
        conn.commit()
        if prev and prev["stamp"] == stamp and not args.force:
            print(f"people already attached for {stamp}; nothing changed")
            run.skip(f"unchanged since {stamp}")
            return 0

        ts = _now()
        t1 = time.monotonic()
        ph = attach_phones(conn)
        t2 = time.monotonic()
        so = attach_social(conn)
        gone = retire(conn, ts)
        conn.execute("INSERT INTO office_phone_state (k, stamp, done_at, note) VALUES"
                     " ('attach', ?, ?, ?) ON CONFLICT (k) DO UPDATE SET stamp=excluded.stamp,"
                     " done_at=excluded.done_at, note=excluded.note",
                     (stamp, ts, f"{ph.get('written', 0)} phones"))
        conn.commit()
        msg = (f"{ph.get('written', 0):,} people given a line: {ph.get('office', 0):,} branch"
               f" office, {ph.get('main', 0):,} main ({ph.get('no_phone', 0):,} at firms with"
               f" no phone); LinkedIn: {so.get('company', 0):,} company pages,"
               f" {so.get('person_matched', 0):,} profiles matched,"
               f" {so.get('person_probable', 0):,} probable,"
               f" {so.get('person_unmatched', 0):,} unmatched; {gone:,} stale rows retired;"
               f" attach {t2 - t1:.0f}s, total {time.monotonic() - t0:.0f}s")
        print(msg)
        notes.append(msg)
        run.rows_out = ph.get("written", 0)
        run.note("; ".join(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
