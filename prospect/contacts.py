"""Every way to reach a firm or a person at it, in one table.

Contact details used to live in four tables, one per source (the filed ADV
phone, brochure contacts, website contacts, pattern guesses), which meant every
screen and export re-implemented the merge and none of them agreed. They now
land in contact_point, one row per address or number per person, with:

  source        where it came from: adv, brochure, website, vcard, directory,
                pattern, ai, manual. `sources` keeps every source that has
                reported the same value, so agreement between sources shows.
  confidence    0 to 100. A filed or published address is high; a pattern
                guess is low until a mail server confirms it.
  is_role       a shared inbox (info@, compliance@) rather than a person. Kept,
                because a firm inbox is still a way in, but never presented as
                a named contact.
  verify_status what an email check said: valid, invalid, risky, catch_all,
                unknown, no_mail_server, or unverified. Only `valid` means a
                mail server accepted that exact mailbox; everything else is
                labelled for what it is.

The provenance rule from the @linkedin.com incident still holds: a guessed
address is always marked as one, and only verification can promote it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_point (
    id            INTEGER PRIMARY KEY,
    crd           TEXT NOT NULL,
    person_key    TEXT NOT NULL DEFAULT '',  -- '' firm level; i:<indvl_pk>; n:<normalised name>
    person_name   TEXT,
    title         TEXT,
    kind          TEXT NOT NULL,             -- email | phone
    value         TEXT NOT NULL,             -- normalised, see norm_email / norm_phone
    label         TEXT,                      -- phone: direct | mobile | office | main | toll_free
    is_role       INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,
    sources       TEXT NOT NULL,             -- comma separated, every source that reported it
    source_ref    TEXT,                      -- page URL, directory id, pattern name
    confidence    INTEGER NOT NULL DEFAULT 50,
    verify_status TEXT NOT NULL DEFAULT 'unverified',
    verify_detail TEXT,
    verified_at   TEXT,
    found_at      TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (crd, kind, value, person_key)
);
CREATE INDEX IF NOT EXISTS ix_cp_crd ON contact_point (crd);
CREATE INDEX IF NOT EXISTS ix_cp_verify ON contact_point (kind, verify_status);
CREATE INDEX IF NOT EXISTS ix_cp_value ON contact_point (value);
CREATE INDEX IF NOT EXISTS ix_cp_person ON contact_point (crd, person_key);
CREATE INDEX IF NOT EXISTS ix_cp_usable ON contact_point (crd, person_key, kind)
    WHERE kind='phone' OR (kind='email' AND verify_status='valid');
CREATE OR REPLACE VIEW usable_contact_point AS
    SELECT * FROM contact_point WHERE kind='phone' OR (kind='email' AND verify_status='valid');
"""

# How much a source is trusted before any verification. Verification overrides:
# a valid check lifts any source to 95, an invalid one drops it to 0.
SOURCE_CONFIDENCE = {
    "adv": 95, "brochure": 90, "vcard": 90, "website": 85, "directory": 70,
    "manual": 90, "ai": 60, "pattern": 35,
}

VERIFY_LABEL = {
    "valid": "Verified", "invalid": "Bounces", "risky": "Risky",
    "catch_all": "Accept-all domain", "unknown": "Could not tell",
    "no_mail_server": "No mail server", "unverified": "Not checked",
    "queued": "Queued",
}

# Shared inboxes. Real and reachable, but nobody in particular reads them.
ROLE_RE = re.compile(
    r"^(info|contact|hello|hi|hey|enquir(y|ies)|inquir(y|ies)|sales|support|help|admin"
    r"|office|team|mail|press|media|marketing|careers|jobs|hr|recruit(ing|ment)?"
    r"|billing|accounts?|accounting|finance|invoices?|legal|privacy|security|partners"
    r"|partnerships|business|bd|general|customerservice|customer[-_.]?care|service"
    r"|services|booking|reservations|orders?|webmaster|hostmaster|it|desk|reception"
    r"|newsletter|subscribe|feedback|questions|ask|talk|connect|compliance|operations"
    r"|ops|clientservices?|client[-_.]?service|clients?|advisors?|planning|wealth"
    r"|invest(ments?|ing)?|trading|trade|custody|transfers?|documents?|statements?"
    r"|noreply|no[-_.]?reply|frontdesk|main|appointments?|scheduling|events?)"
    r"s?([-_.].*)?$", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_email(v: str) -> str:
    return (v or "").strip().strip(".,;:<>()[]\"'").lower()


def norm_phone(v: str) -> str | None:
    """US numbers to (555) 555-5555, keeping an extension. Anything that is not a
    plausible North American number comes back None rather than half-formatted."""
    raw = (v or "").strip()
    ext = ""
    m = re.search(r"(?:ext\.?|x|extension)\s*(\d{1,6})\s*$", raw, re.I)
    if m:
        ext = f" x{m.group(1)}"
        raw = raw[:m.start()]
    d = re.sub(r"\D", "", raw)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d[0] in "01" or d[3] in "01":
        return None
    if d[3:6] == "555" and d[6:7] == "0":
        return None   # fictional block 555-01xx
    return f"({d[:3]}) {d[3:6]}-{d[6:]}{ext}"


def phone_label(d10: str) -> str | None:
    if d10[:3] in ("800", "833", "844", "855", "866", "877", "888"):
        return "toll_free"
    return None


def is_role_email(email: str) -> bool:
    return bool(ROLE_RE.match(norm_email(email).split("@", 1)[0]))


def name_key(name: str) -> str:
    """A person key for someone not matched to an IAPD record: lowercase first and
    last name, punctuation and middle names dropped."""
    parts = [p for p in re.sub(r"[^a-z ]", " ", (name or "").lower()).split() if p]
    parts = [p for p in parts if p not in ("jr", "sr", "ii", "iii", "iv", "mr", "mrs",
                                           "ms", "dr", "cfp", "cfa", "cpa", "chfc",
                                           "clu", "aif", "crpc", "cima", "esq")]
    if len(parts) < 2:
        return ""
    return f"n:{parts[0]} {parts[-1]}"


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert(conn, crd: str, kind: str, value: str, source: str, *,
           person_key: str = "", person_name: str | None = None,
           title: str | None = None, label: str | None = None,
           source_ref: str | None = None, confidence: int | None = None,
           is_role: bool | None = None) -> bool:
    """Record one contact detail. Returns True if it is new.

    Normalises the value, merges with an existing row for the same value and
    person (keeping the higher confidence and every source that reported it),
    and removes the firm-level copy once the same address is attributed to a
    person, so one mailbox never appears twice on a firm page. The caller
    commits."""
    if kind == "email":
        value = norm_email(value)
        if "@" not in value:
            return False
        if is_role is None:
            is_role = is_role_email(value)
    elif kind == "phone":
        p = norm_phone(value)
        if not p:
            return False
        value = p
        if label is None:
            label = phone_label(re.sub(r"\D", "", value)[:10])
    else:
        raise ValueError(f"unknown contact kind {kind}")
    conf = confidence if confidence is not None else SOURCE_CONFIDENCE.get(source, 50)
    ts = now()
    row = conn.execute(
        "SELECT id, sources, confidence FROM contact_point"
        " WHERE crd=? AND kind=? AND value=? AND person_key=?",
        (crd, kind, value, person_key)).fetchone()
    if row:
        srcs = [s for s in (row["sources"] or "").split(",") if s]
        if source not in srcs:
            srcs.append(source)
        conn.execute(
            "UPDATE contact_point SET sources=?, confidence=?, updated_at=?,"
            " person_name=COALESCE(?, person_name), title=COALESCE(title, ?),"
            " label=COALESCE(label, ?),"
            " source=CASE WHEN ? > confidence THEN ? ELSE source END,"
            " source_ref=CASE WHEN ? > confidence THEN ? ELSE source_ref END"
            " WHERE id=?",
            (",".join(srcs), max(conf, row["confidence"]), ts, person_name, title, label,
             conf, source, conf, source_ref, row["id"]))
        return False
    conn.execute(
        "INSERT INTO contact_point (crd, person_key, person_name, title, kind, value,"
        " label, is_role, source, sources, source_ref, confidence, found_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (crd, person_key, person_name, title, kind, value, label, 1 if is_role else 0,
         source, source, source_ref, conf, ts, ts))
    if person_key:
        conn.execute("DELETE FROM contact_point WHERE crd=? AND kind=? AND value=?"
                     " AND person_key=''", (crd, kind, value))
    return True


def set_verification(conn, cp_id: int, status: str, detail: str | None) -> None:
    conf_sql = ("CASE WHEN ?='valid' THEN GREATEST(confidence, 95)"
                " WHEN ? IN ('invalid','no_mail_server') THEN 0 ELSE confidence END")
    conn.execute(f"UPDATE contact_point SET verify_status=?, verify_detail=?,"
                 f" verified_at=?, confidence={conf_sql} WHERE id=?",
                 (status, detail, now(), status, status, cp_id))


OLD_CRAWL = ("NOT EXISTS (SELECT 1 FROM web_enrich_state w WHERE w.crd = c.crd"
             " AND w.method IS NOT NULL)")


def backfill(conn) -> int:
    """Copy the four legacy contact tables into contact_point. Idempotent: the
    unique key makes a second run a no-op, so it is safe at every startup and
    after any job that still writes a legacy table."""
    n = 0
    ts = now()

    def run(sql: str) -> int:
        try:
            cur = conn.execute(sql)
            return max(cur.rowcount or 0, 0)
        except Exception:
            conn.rollback()
            return 0

    # Phones are normalised in Python, so a number filed as 212-555-0100 and
    # one scraped as (212) 555-0100 land on the same row.
    def phones(sql: str, source: str, conf: int, label: str | None = None) -> int:
        got = 0
        try:
            rows = conn.execute(sql).fetchall()
        except Exception:
            conn.rollback()
            return 0
        batch = []
        for r in rows:
            p = norm_phone(r["v"])
            if not p:
                continue
            person = r.get("person") if hasattr(r, "get") else None
            key = name_key(person) if person else ""
            batch.append((r["crd"], key, person, r.get("title"), p,
                          label or phone_label(re.sub(r"\D", "", p)[:10]),
                          source, source, r.get("ref"), conf,
                          r.get("found") or ts, ts))
        for i in range(0, len(batch), 2000):
            conn.executemany(
                "INSERT INTO contact_point (crd, person_key, person_name, title, kind,"
                " value, label, is_role, source, sources, source_ref, confidence,"
                " found_at, updated_at) VALUES (?,?,?,?,'phone',?,?,0,?,?,?,?,?,?)"
                " ON CONFLICT DO NOTHING", batch[i:i + 2000])
            got += len(batch[i:i + 2000])
        return got

    # Main office phone, as filed on Form ADV.
    n += phones("SELECT crd, phone v FROM firm_current WHERE phone IS NOT NULL"
                " AND phone != ''", "adv", 95, "main")
    # Emails and phones the firm printed in its own brochure.
    n += run(f"""INSERT INTO contact_point (crd, person_key, kind, value, is_role,
            source, sources, source_ref, confidence, found_at, updated_at)
        SELECT crd, '', 'email', LOWER(TRIM(value)), 0, 'brochure', 'brochure', context,
               90, COALESCE(found_at, '{ts}'), '{ts}'
        FROM firm_contact_info WHERE kind = 'email'
        ON CONFLICT DO NOTHING""")
    n += phones("SELECT crd, value v, context ref, found_at found FROM firm_contact_info"
                " WHERE kind='phone'", "brochure", 90)
    # People and firm details read off the firm's own website, for firms only
    # the old crawler has read. The current one writes contact_point itself,
    # keyed to each person's IAPD record, and copying its web_contact mirror
    # back here would add every person a second time under a name key.
    n += run(f"""INSERT INTO contact_point (crd, person_key, person_name, title, kind,
            value, is_role, source, sources, source_ref, confidence, found_at, updated_at)
        SELECT crd,
               CASE WHEN person IS NULL THEN '' ELSE 'n:' || LOWER(person) END,
               person, title, 'email', LOWER(TRIM(email)), 0, 'website', 'website',
               source_url, 85, found_at, '{ts}'
        FROM web_contact c WHERE email IS NOT NULL AND {OLD_CRAWL}
        ON CONFLICT DO NOTHING""")
    n += phones("SELECT crd, phone v, person, title, source_url ref, found_at found"
                f" FROM web_contact c WHERE phone IS NOT NULL AND {OLD_CRAWL}", "website", 85)
    # Pattern guesses, carried with what the old domain check said.
    n += run(f"""INSERT INTO contact_point (crd, person_key, person_name, title, kind,
            value, is_role, source, sources, source_ref, confidence, verify_status,
            verified_at, found_at, updated_at)
        SELECT crd, CASE WHEN COALESCE(name, '') = '' THEN ''
                         ELSE 'n:' || LOWER(name) END,
               name, title, 'email', LOWER(email),
               0, 'pattern', 'pattern', pattern, 35,
               CASE status WHEN 'no_mail_server' THEN 'no_mail_server'
                           WHEN 'bad_syntax' THEN 'invalid' ELSE 'unverified' END,
               checked_at, COALESCE(checked_at, '{ts}'), '{ts}'
        FROM contact_email WHERE email IS NOT NULL
        ON CONFLICT DO NOTHING""")
    conn.commit()
    # Role inboxes recognised after the fact, so the legacy rows read right.
    rows = conn.execute("SELECT id, value FROM contact_point WHERE kind='email'"
                        " AND is_role=0 AND source != 'pattern'").fetchall()
    ids = [r["id"] for r in rows if is_role_email(r["value"])]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        conn.execute(f"UPDATE contact_point SET is_role=1 WHERE id IN"
                     f" ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
    # Legacy person keys were whole lowercase names; reduce them to first and
    # last so the same person from two sources lands on one key. A row whose
    # reduced key already exists for the same value is a duplicate and goes.
    rows = conn.execute("SELECT id, crd, kind, value, person_key, person_name"
                        " FROM contact_point WHERE person_key LIKE 'n:%'"
                        " AND person_name IS NOT NULL").fetchall()
    for r in rows:
        k = name_key(r["person_name"])
        if not k or k == r["person_key"]:
            continue
        dup = conn.execute("SELECT 1 FROM contact_point WHERE crd=? AND kind=?"
                           " AND value=? AND person_key=?",
                           (r["crd"], r["kind"], r["value"], k)).fetchone()
        if dup:
            conn.execute("DELETE FROM contact_point WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE contact_point SET person_key=? WHERE id=?", (k, r["id"]))
    # A value attributed to a person no longer needs its firm-level copy.
    conn.execute("""DELETE FROM contact_point f WHERE f.person_key='' AND EXISTS (
        SELECT 1 FROM contact_point p WHERE p.crd=f.crd AND p.kind=f.kind
        AND p.value=f.value AND p.person_key != '')""")
    conn.commit()
    return n
