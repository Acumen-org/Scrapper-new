"""Sending contacts to Cadence's Cold outreach, and reading back where they stand.

Cadence, the sales sequencer beside the CRM, owns the API. Its plan, section 14,
is the contract, copied byte for byte into docs/outreach-sandbox-plan.md as
"Shared contract with Bellwether"; every number below (500 per request, 7 days,
5 s, 30 s, 2 min, 30 and 60 requests a minute) comes from there. Bellwether
sends contacts, never firms, and shows per person whether they went, who owns
them in Cadence, and whether they reached the CRM. The code and the contract
still say "push" and "sandbox"; the screens say "Send to Cold outreach".

Contract v1.1 (the cold-outreach plan, 7.3) only adds. Cadence's ping lists the
versions it speaks ("contracts": ["1", "1.1"]); when 1.1 is among them every
request carries X-Cadence-Contract: 1.1 and Bellwether reads the new status
fields, asks for the owners list and sends a Do not contact set here through
POST /contacts/suppress. A Cadence that answers ping without 1.1 gets exactly
the v1 requests it always had, so the two apps can be deployed, and rolled
back, one at a time.

What this module holds:

  records      one contact as Bellwether knows it at the moment of the push:
               the People index row for the name and title, and the live
               usable_contact_point rows for emails, phones and LinkedIn, so a
               guess can never be sent and nothing is half an hour old.
  rules        the block rules a push runs before anything leaves: do not
               contact, opted out in Cadence, a Customer firm, no way to reach
               them, already pushed. Then Cadence's own check (14.14) says who
               is already in the CRM, and those are not sent either.
  payload      the 14.4 contact, normalised as 14.5 says, within its limits.
               list:ATS and TOLLBOOTH are stripped whatever asks for them.
  sending      one request per chunk of at most 500, each with its own UUID v4
               Idempotency-Key and its body stored exactly as sent, so a retry
               resends the same bytes under the same key (14.10). Retries after
               5 s, 30 s and 2 min, honouring Retry-After; 401 and a switched
               off integration hold the whole queue; contract errors fail.
  status       the cadence_sync job reads every change since the last run
               (14.12, changedSince with nextCursor, 15 minutes of overlap),
               and a person opening a firm reads that firm's pushed people by
               id. An opt-out in Cadence becomes a suppression here that
               belongs to the person (every firm they work at, their other
               emails and phones), shown as "Do not contact · Cadence".

Every call to Cadence has a hard total deadline, the name lookup included, and
no pooled database connection is held while one is in flight (_call, _idle).

The API key comes from CADENCE_API_KEY and the address from CADENCE_API_URL,
the environment only. The key is never stored, logged, put in an event or an
exception, or shown on a screen.

Every statement here is written in the SQL both Postgres (through prospect.pg)
and SQLite understand, so scripts/qa_requirements.py can run the whole push
against an in-memory copy of the tables and a fake Cadence, offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

SCHEMA = """
-- One row per contact ever pushed: where it stands now.
CREATE TABLE IF NOT EXISTS cadence_push (
    id              INTEGER PRIMARY KEY,
    crd             TEXT NOT NULL,
    person_key      TEXT NOT NULL,           -- i:<indvl_pk> | n:<name key>, as in contact_point
    indvl_pk        TEXT,                    -- SEC individual CRD when registered
    name            TEXT NOT NULL,           -- as sent
    email           TEXT,                    -- as sent, normalised
    phone           TEXT,
    linkedin        TEXT,
    state           TEXT NOT NULL,           -- sending | retry_wait | in_sandbox | in_crm
                                             -- | moved_to_crm | opted_out | rejected | removed
                                             -- | failed. Screens show any other as "Other".
    reason          TEXT,                    -- Cadence's reason and detail, or the last error, in words
    cadence_id      TEXT,                    -- cadenceContactId
    crm_id          TEXT,                    -- crmPersonId, once in the CRM
    crm_url         TEXT,                    -- crmUrl
    cadence_owner   TEXT,                    -- ownerEmail as Cadence reports it; NULL = unclaimed
    cadence_owner_name TEXT,                 -- ownerName
    owner_email     TEXT,                    -- assignTo.email Bellwether sent; NULL = the pusher
    product         TEXT,                    -- Bellwether product key, e.g. phh_1031
    pushed_by       TEXT NOT NULL,           -- app_user.login of the latest push
    first_pushed_at TEXT NOT NULL,
    pushed_at       TEXT,                    -- last time Cadence accepted it
    moved_at        TEXT,                    -- movedToCrmAt
    batch_id        TEXT,
    request_key     TEXT,                    -- Idempotency-Key of the request that carries it
    payload_hash    TEXT,                    -- sha256 of the contact as sent
    claimed_at      TEXT,                    -- set while state = 'sending'
    status_at       TEXT,                    -- last read of Cadence's status
    status_changed_at TEXT,                  -- Cadence's statusChangedAt
    last_pushed_at  TEXT,                    -- when pushed_by sent it last
    -- Contract v1.1 status fields, NULL from a Cadence that speaks only v1.
    cadence_url     TEXT,                    -- cadenceUrl: the contact in Cadence
    removed_by      TEXT,                    -- removedBy, removedAt, removedReason
    removed_at      TEXT,
    removed_reason  TEXT,
    erased_at       TEXT,                    -- erasedAt: Bellwether blanked their details
    email_bounced_at TEXT,                   -- emailBouncedAt: the email sent bounced
    possible_crm_id TEXT,                    -- possibleCrmPersonId
    last_outcome    TEXT,                    -- lastOutcome, e.g. not_interested
    last_outcome_at TEXT,
    opt_out_lifted_at TEXT,                  -- optOutLiftedAt
    UNIQUE (crd, person_key)
);
CREATE INDEX IF NOT EXISTS ix_cadence_push_state ON cadence_push (state);
CREATE INDEX IF NOT EXISTS ix_cadence_push_req ON cadence_push (request_key);
CREATE INDEX IF NOT EXISTS ix_cadence_push_batch ON cadence_push (batch_id);
CREATE INDEX IF NOT EXISTS ix_cadence_push_indvl ON cadence_push (indvl_pk) WHERE indvl_pk IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cadence_push_email ON cadence_push (email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cadence_push_li ON cadence_push (linkedin) WHERE linkedin IS NOT NULL;
-- Not unique: two Bellwether rows (a rep's SEC row and a website spelling)
-- can resolve to one Cadence contact.
CREATE INDEX IF NOT EXISTS ix_cadence_push_cid ON cadence_push (cadence_id);

-- One row per push request (a chunk of a batch). The body is kept exactly as
-- sent, so a retry resends the same bytes under the same Idempotency-Key
-- (14.10). It is cleared once the request ends.
CREATE TABLE IF NOT EXISTS cadence_request (
    request_key   TEXT PRIMARY KEY,          -- Idempotency-Key: a fresh UUID v4 per request
    batch_id      TEXT NOT NULL,
    body          TEXT,                      -- JSON exactly as sent; NULL once finished
    body_sha256   TEXT NOT NULL,
    contacts      INTEGER NOT NULL,          -- 1 to 500
    state         TEXT NOT NULL,             -- sending | retry_wait | done | failed
    retries       INTEGER NOT NULL DEFAULT 0, -- of the contract's 3
    first_sent_at TEXT NOT NULL,             -- the key is retried for 7 days from this, no longer
    next_retry_at TEXT,
    claimed_at    TEXT,                      -- set while state = 'sending'
    last_error    TEXT,                      -- http_<code> <error.code> | timeout | connection
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_cadence_request_due ON cadence_request (state, next_retry_at);
CREATE INDEX IF NOT EXISTS ix_cadence_request_batch ON cadence_request (batch_id);

CREATE TABLE IF NOT EXISTS cadence_batch (
    id          TEXT PRIMARY KEY,            -- uuid4
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,               -- people | firm
    filter_qs   TEXT,                        -- the People filter, for "all matching"
    product     TEXT,
    owner_email TEXT,
    note        TEXT,
    requested   INTEGER NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    client_key  TEXT                         -- the dialog's own id for the send, so it can find
                                             -- the batch after a gateway error (B078)
);

-- Append only: the audit. Never the API key, never contact values.
CREATE TABLE IF NOT EXISTS cadence_push_event (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,               -- login, or 'system' for the job
    crd         TEXT NOT NULL,               -- '' for an event about the whole queue
    person_key  TEXT NOT NULL,
    batch_id    TEXT,
    action      TEXT NOT NULL,               -- push | resend | retry | skipped | result | status
                                             -- | suppress | lift | hold | told
    outcome     TEXT,                        -- created | duplicate_in_sandbox | already_in_crm
                                             -- | rejected:<reason> | failed | http_<code>
                                             -- | timeout | skipped:<rule> ...
    http_status INTEGER,
    request_key TEXT,
    detail      TEXT                         -- JSON: reason, warnings, filledFields, fields sent
                                             -- (names only), Cadence ids
);
CREATE INDEX IF NOT EXISTS ix_cpe_person ON cadence_push_event (crd, person_key, at);
CREATE INDEX IF NOT EXISTS ix_cpe_at ON cadence_push_event (at);
CREATE INDEX IF NOT EXISTS ix_cpe_actor ON cadence_push_event (actor, action, at);

-- Do not contact. A lifted row is kept, for the audit.
CREATE TABLE IF NOT EXISTS contact_suppression (
    id        INTEGER PRIMARY KEY,
    scope     TEXT NOT NULL,                 -- person | individual | email | phone | domain | firm
    value     TEXT NOT NULL,                 -- crd:person_key | SEC individual CRD | norm_email
                                             -- | E.164 phone | domain | crd
    channel   TEXT NOT NULL DEFAULT 'all',   -- all (the dialog no longer offers email or call)
    reason    TEXT NOT NULL,                 -- opted_out | do_not_call | do_not_email | erasure | other
    source    TEXT NOT NULL,                 -- cadence | manual
    note      TEXT,
    set_by    TEXT,
    set_at    TEXT NOT NULL,
    lifted_by TEXT,
    lifted_at TEXT,
    for_id    TEXT,                          -- source cadence: the contact whose opt-out wrote it,
                                             -- so Cadence lifting that opt-out lifts these rows only
    to_cadence TEXT,                         -- source manual: pending | sent | not_sent (Cadence
                                             -- does not speak v1.1) | NULL when nobody was sent
    to_cadence_at TEXT,
    to_cadence_note TEXT                     -- what Cadence answered, or the last error, in words
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_suppression_active
    ON contact_suppression (scope, value, channel) WHERE lifted_at IS NULL;

CREATE TABLE IF NOT EXISTS cadence_state (
    k TEXT PRIMARY KEY,                      -- backoff_until | hold | status_since | push_slot
                                             -- | status_slot | last_sync_ok | max_batch
                                             -- | contracts | owners | ping | names_rekeyed ...
    v TEXT
);
"""

# Columns added after the tables first shipped. Bellwether has no migration
# tool: init() adds any that are missing, and a build that does not know them
# reads around them, so a rollback is always safe (the plan, 7.2).
COLUMNS = (("cadence_push", "last_pushed_at", "TEXT"), ("cadence_push", "cadence_url", "TEXT"),
           ("cadence_push", "removed_by", "TEXT"), ("cadence_push", "removed_at", "TEXT"),
           ("cadence_push", "removed_reason", "TEXT"), ("cadence_push", "erased_at", "TEXT"),
           ("cadence_push", "email_bounced_at", "TEXT"), ("cadence_push", "possible_crm_id", "TEXT"),
           ("cadence_push", "last_outcome", "TEXT"), ("cadence_push", "last_outcome_at", "TEXT"),
           ("cadence_push", "opt_out_lifted_at", "TEXT"), ("cadence_batch", "client_key", "TEXT"),
           ("contact_suppression", "for_id", "TEXT"), ("contact_suppression", "to_cadence", "TEXT"),
           ("contact_suppression", "to_cadence_at", "TEXT"),
           ("contact_suppression", "to_cadence_note", "TEXT"))
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_cadence_batch_key ON cadence_batch (client_key) WHERE client_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_suppression_for ON contact_suppression (for_id) WHERE for_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_suppression_told ON contact_suppression (to_cadence) WHERE to_cadence = 'pending';
"""

TABLES = ("cadence_push", "cadence_request", "cadence_batch", "cadence_push_event",
          "contact_suppression", "cadence_state")

# ------------------------------------------------------------------ the contract

API = "/api/integrations/bellwether/v1"
CONTRACT = "1.1"              # the newest contract Bellwether speaks (the plan, 7.3)
CONTRACT_MAX = 500            # contacts per request, ids per status request (14.11)
TIMEOUT_S = 60                # client timeout (14.11): the whole call, the name lookup included
PING_TIMEOUT_S = 15           # Test the connection says what happened within this
STATUS_TIMEOUT_S = 20         # a status read by id while someone looks at a firm
STATUS_OVERLAP_S = 900        # changedSince starts 15 minutes before the last serverTime (B062)
OWNERS_TTL_S = 600            # Cadence's owners list is asked again after 10 minutes
PING_FRESH_H = 24             # switching sending on needs a test that passed this recently
WORKSPACE_TZ = "America/Chicago"  # Cadence's workspace time zone: dates, and "today" for the cap
RETRY_STEPS = (5, 30, 120)    # seconds; at most three retries (14.10)
IN_PROGRESS_CAP_S = 60        # 409 idempotency_in_progress waits, then it counts as a timeout
KEY_DAYS = 7                  # Cadence keeps a key and its response this long (14.10)
PUSH_GAP_S = 2.5              # one push or check request every 2.5 s: 24 a minute, under 30
STATUS_GAP_S = 1.25           # one status request every 1.25 s: 48 a minute, under 60
STALE_CLAIM_S = 300           # a 'sending' claim older than one attempt goes back in the queue
PREVIEW_TTL_S = 900           # a preview's CRM check stands for 15 minutes
BY_ID_FRESH_S = 60            # a contact read by id in the last minute is not asked again
NOTE_MAX = 500                # the dialog's note; the contract allows 5000
SYNC_LATE_H = 2               # status sync failing this long raises an alert
WAIT_LATE_H = 1               # a push waiting this long raises an alert

ACTIVE = ("sending", "retry_wait")
# States a person may be sent again from, each as a new request with a new key.
# A refusal is not one of them: Cadence keeps it, and says why on the chip (B015).
REPUSH = ("failed", "removed")
# Every state this build writes. Screens show any other as "Other" and offer
# nothing for it, so an older or newer build's rows never break a page.
STATES = ACTIVE + ("in_sandbox", "in_crm", "moved_to_crm", "opted_out", "rejected", "failed",
                   "removed")

# Never sent, whatever a caller passes (decision 25).
NEVER_PRODUCTS = {"TOLLBOOTH"}
NEVER_TAGS = {"list:ats"}
PRODUCT_INTEREST = {"PHH": "PHH", "AcuBooth": "ACUBOOTH", "Glynac": "GLYNAC"}

STATUS_STATE = {"in_sandbox": "in_sandbox", "moved_to_crm": "moved_to_crm",
                "already_in_crm": "in_crm", "opted_out": "opted_out", "removed": "removed",
                # v1.1: Cadence keeps every refusal and reports it (B015).
                "refused": "rejected"}

MATCHED_WORDS = {"email": "email", "phone": "phone", "linkedin_url": "LinkedIn",
                 "individual_crd": "SEC individual CRD", "bellwether_contact_id": "Bellwether id"}
REASON_WORDS = {"invalid_field": "a required field is missing or wrong",
                "do_not_contact": "marked do not contact",
                "suppressed": "on Cadence's do-not-contact list",
                "blocked_account": "Cadence blocks this firm",
                "internal_contact": "an Acumen address",
                "duplicate_in_request": "the same person twice in one send",
                "internal_error": "Cadence hit an error on this contact"}
WARN_WORDS = {"email_dropped": "Cadence dropped the email",
              "phone_dropped": "Cadence dropped the phone",
              "linkedin_dropped": "Cadence dropped the LinkedIn profile",
              "product_dropped": "Cadence dropped the product",
              "pod_ignored": "Cadence ignored the pod",
              "possible_crm_match": "Possible match in the CRM",
              # v1.1
              "owner_not_found": "The owner is not a Cadence user who can own contacts; it arrives unclaimed",
              "assign_to_not_found": "The chosen owner is not a Cadence user who can own contacts",
              "note_added": "Already in Cold outreach; the note was added to it",
              "details_changed": "Already in Cold outreach; the newer email or phone was added for the owner to review",
              "note_truncated": "Cadence shortened the note",
              "value_defaulted": "Cadence did not recognise a value and used its default",
              "no_reach": "No email, phone or LinkedIn profile to reach them",
              "possible_sandbox_duplicate": "Possibly the same person as a contact already in Cold outreach"}
# The v1 warning codes (14.8). Contract v1.1's warnings carry a plain sentence
# saying who or what (B017, B068, B070, B072), shown as Cadence wrote it.
V1_WARNINGS = {"email_dropped", "phone_dropped", "linkedin_dropped", "product_dropped", "pod_ignored",
               "possible_crm_match"}


def warn_line(w: dict) -> str:
    """One warning of Cadence's check, in words, under the person it is about."""
    code, detail = w.get("code") or "", w.get("detail")
    if detail and code not in V1_WARNINGS:
        return str(detail)
    return WARN_WORDS.get(code, code) + (f": {detail}" if code == "possible_crm_match" and detail else "")

# The latest call outcome Cadence reports (v1.1 lastOutcome, section 14.12):
# the post-call form's outcome keys, in Cadence's own words. A key an admin
# added in Cadence's Settings is shown humanised.
OUTCOME_WORDS = {"connected": "Connected", "gatekeeper": "Spoke to gatekeeper",
                 "voicemail": "Left voicemail", "no_answer": "No answer", "busy": "Busy / call back",
                 "wrong_number": "Wrong number", "not_interested": "Not interested",
                 "meeting_booked": "Meeting booked", "do_not_contact": "Asked not to be contacted",
                 "no_conversation": "Line connected, no conversation"}

# Injected by the tests, so retries and pacing run on a fake clock.
_clock = time.time
_sleep = time.sleep


def _now() -> datetime:
    return datetime.fromtimestamp(_clock(), timezone.utc)


def _iso(t: datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _z(t: datetime) -> str:
    """Cadence's wire format: ISO 8601 UTC with Z (14.1)."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def init(conn) -> None:
    conn.executescript(SCHEMA)
    for table, column, decl in COLUMNS:
        # Ask the catalogue first: ALTER TABLE takes the table's lock even
        # when the column is already there (db.add_column says why).
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.executescript(INDEXES)
    conn.commit()


def ready(conn) -> bool:
    """The tables exist (startup made them). Screens read nothing otherwise."""
    try:
        conn.execute("SELECT 1 FROM cadence_push WHERE 1=0").fetchall()
        return True
    except Exception:
        conn.rollback()
        return False


# ------------------------------------------------------------------ connection

def api_url() -> str:
    return os.environ.get("CADENCE_API_URL", "").strip().rstrip("/")


def api_key() -> str:
    return os.environ.get("CADENCE_API_KEY", "").strip()


def _url_ok(url: str) -> bool:
    """HTTPS only (14.1). Plain http is allowed to this machine alone, which is
    where the tests' fake Cadence listens."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")


def configured() -> bool:
    return bool(api_key()) and _url_ok(api_url())


def switched_on() -> bool:
    from . import settings
    return settings.get_bool("cadence.enabled")


def problem() -> str | None:
    """Why nothing can be sent right now, in words, or None."""
    if not api_url() or not api_key():
        missing = " and ".join(n for n, v in (("CADENCE_API_URL", api_url()),
                                              ("CADENCE_API_KEY", api_key())) if not v)
        return (f"Cadence is not connected: this server has no {missing}. An admin sets "
                "them in the server environment.")
    if not _url_ok(api_url()):
        return "Cadence is not connected: CADENCE_API_URL must be an https address."
    if not switched_on():
        return ("Sending to Cold outreach is switched off. An admin turns it on in Settings, Cadence, "
                "after a test of the connection passes.")
    return None


def max_per_push() -> int:
    from . import settings
    return max(1, min(CONTRACT_MAX, settings.get_int("cadence.max_per_push", 200) or 200))


def daily_per_user() -> int:
    from . import settings
    return max(1, settings.get_int("cadence.daily_per_user", 500) or 500)


# ------------------------------------------------------------------ normalisation (14.5)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CRD_RE = re.compile(r"^[0-9]{1,12}$")
# A name key holds letters of any script (contacts.name_key, B064).
KEY_RE = re.compile(r"^(i:[0-9]{1,12}|n:[^\W\d_]+(?: [^\W\d_]+)*)$")

COUNTRY = {"united states": "US", "united states of america": "US", "usa": "US",
           "u.s.a.": "US", "u.s.": "US", "canada": "CA", "united kingdom": "GB",
           "england": "GB", "scotland": "GB", "great britain": "GB", "ireland": "IE",
           "bermuda": "BM", "cayman islands": "KY", "switzerland": "CH", "germany": "DE",
           "france": "FR", "netherlands": "NL", "luxembourg": "LU", "australia": "AU",
           "japan": "JP", "hong kong": "HK", "singapore": "SG", "india": "IN",
           "israel": "IL", "mexico": "MX", "brazil": "BR", "puerto rico": "PR",
           "virgin islands, british": "VG", "british virgin islands": "VG",
           "guernsey": "GG", "jersey": "JE", "south africa": "ZA", "sweden": "SE",
           "spain": "ES", "italy": "IT", "china": "CN", "korea, republic of": "KR"}


def norm_email(v) -> str | None:
    e = str(v or "").strip().lower()
    if not e or len(e) > 254 or not EMAIL_RE.match(e):
        return None
    return e


def norm_phone(v, country: str | None = None) -> tuple[str | None, str | None]:
    """(E.164, extension digits) with the region from `country`, else US."""
    import phonenumbers as pn
    raw = str(v or "").strip().removeprefix("tel:")
    if not raw:
        return None, None
    try:
        n = pn.parse(raw, (country or "US").upper())
    except pn.NumberParseException:
        return None, None
    if not pn.is_valid_number(n):
        return None, None
    ext = re.sub(r"\D", "", n.extension or "")[:10] or None
    n.extension = None
    return pn.format_number(n, pn.PhoneNumberFormat.E164), ext


def readable_phone(e164: str, ext: str | None = None) -> str:
    """An E.164 number the way the screens write phones: (212) 555-0142 in
    the US, +44 20 8366 1177 elsewhere."""
    import phonenumbers as pn
    try:
        n = pn.parse(e164, None)
        out = pn.format_number(n, pn.PhoneNumberFormat.NATIONAL if n.country_code == 1
                               else pn.PhoneNumberFormat.INTERNATIONAL)
    except pn.NumberParseException:
        out = e164
    return out + (f" x{ext}" if ext else "")


def norm_linkedin(v) -> str | None:
    """linkedin.com/in/<slug>: no scheme, no www. or country subdomain, no
    query, fragment or trailing path, the slug percent-decoded and then lower
    case, as Cadence does (14.5, B169). Anything else is None."""
    from urllib.parse import unquote
    s = str(v or "").strip()
    s = re.sub(r"^[a-z]+://", "", s, flags=re.I).split("#")[0].split("?")[0]
    s = re.sub(r"^(?:www\.|[a-z]{2,3}\.)", "", s, flags=re.I)
    m = re.match(r"^linkedin\.com/in/([^/\s]+)", s, re.I)
    return f"linkedin.com/in/{unquote(m.group(1)).lower()}" if m else None


def norm_crd(v) -> str | None:
    d = re.sub(r"\D", "", str(v or "")).lstrip("0")
    return d or None


def norm_domain(v) -> str | None:
    s = str(v or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split("?")[0].split("#")[0]
    s = s.rsplit("@", 1)[-1].split(":")[0]
    s = re.sub(r"^www\.", "", s).strip(".")
    return s if "." in s else None


def country_code(v) -> str | None:
    s = str(v or "").strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return COUNTRY.get(s.lower())


def contact_id(crd: str, person_key: str) -> str:
    """bellwetherContactId: <crd>:<person_key>, sent as stored (Cadence
    compares it exactly, so it is never normalised)."""
    return f"{crd}:{person_key}"


def parse_id(cid) -> tuple[str, str] | None:
    crd, _, key = str(cid or "").partition(":")
    if CRD_RE.match(crd) and KEY_RE.match(key) and len(cid) <= 200:
        return crd, key
    return None


def _cut(v, n: int) -> str | None:
    s = " ".join(str(v or "").split())
    return s[:n].rstrip() or None


# ------------------------------------------------------------------ products

def push_product(key: str | None) -> str | None:
    """The product a push carries. ATS goes as no product at all (decision
    25), so it is never a product here."""
    from . import products
    if not key or key not in products.product_keys():
        return None
    return None if products.product(key).get("family") == "ATS" else key


def product_choices() -> list[tuple[str, str]]:
    from . import products
    return [(k, products.product(k)["name"]) for k in products.product_keys()
            if products.product(k).get("family") != "ATS"]


def _interest(product: str | None) -> list[str]:
    from . import products
    if not product:
        return []
    fam = products.product(product).get("family")
    v = PRODUCT_INTEREST.get(fam)
    return [v] if v else []


# ------------------------------------------------------------------ the record

def _rows(conn, sql: str, args=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        conn.rollback()
        return []


def _pairs_sql(pairs: list[tuple[str, str]], a: str = "crd", b: str = "person_key") -> tuple[str, list]:
    cond = " OR ".join(f"({a} = ? AND {b} = ?)" for _ in pairs) or "1=0"
    return f"({cond})", [x for p in pairs for x in p]


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


PHONE_ORDER = {"direct": 0, "mobile": 1, "office": 2, "main": 3}


def load_records(conn, ids: list[str]) -> dict[str, dict]:
    """Each contact as Bellwether knows it now. The name, title and firm come
    from the People index; emails, phones and LinkedIn from the live
    usable_contact_point view, never from the index's copy."""
    pairs = list(dict.fromkeys(p for p in (parse_id(i) for i in ids) if p))
    if not pairs:
        return {}
    idx: dict[tuple, dict] = {}
    for chunk in _chunks(pairs, 100):
        where, args = _pairs_sql(chunk)
        for r in _rows(conn, f"SELECT * FROM people_index WHERE {where}", args):
            idx[(r["crd"], r["person_key"])] = r
    crds = sorted({p[0] for p in pairs})
    marks = ",".join("?" * len(crds))
    firms = {r["crd"]: r for r in _rows(conn, f"""SELECT crd, legal_name, business_name, website,
        city, state, country, phone, raum, total_employees FROM firm_current
        WHERE crd IN ({marks})""", crds)}
    status = {r["crd"]: r["status"] for r in _rows(
        conn, f"SELECT crd, status FROM firm_status WHERE crd IN ({marks})", crds)}
    category = {r["crd"]: r["category"] for r in _rows(
        conn, f"SELECT crd, category FROM firm_class WHERE crd IN ({marks})", crds)}
    keys = sorted({p[1] for p in pairs})
    kmarks = ",".join("?" * len(keys))
    want = set(pairs)
    cps: dict[tuple, list] = {}
    for r in _rows(conn, f"""SELECT * FROM usable_contact_point WHERE crd IN ({marks})
                   AND person_key IN ({kmarks})""", crds + keys):
        if (r["crd"], r["person_key"]) in want:
            cps.setdefault((r["crd"], r["person_key"]), []).append(r)
    out = {}
    for crd, key in pairs:
        firm = firms.get(crd)
        if not firm:
            continue
        pts = cps.get((crd, key), [])
        row = idx.get((crd, key))
        if row is None:
            # Not in the index yet (it is rebuilt every half hour): a person
            # the website or a search just named still has a name on their
            # contact rows.
            named = next((p for p in pts if p.get("person_name")), None)
            if not named:
                continue
            parts = (named["person_name"] or "").split()
            row = {"name": " ".join(parts), "first_name": parts[0] if parts else None,
                   "last_name": parts[-1] if len(parts) > 1 else None, "title": named.get("title"),
                   "indvl_pk": key[2:] if key.startswith("i:") else None,
                   "src": "sec" if key.startswith("i:") else "web"}
        emails = sorted((p for p in pts if p["kind"] == "email" and not p.get("is_role")),
                        key=lambda p: (p["verify_status"] != "valid", -(p["confidence"] or 0), p["id"]))
        phones = sorted((p for p in pts if p["kind"] == "phone"),
                        key=lambda p: (PHONE_ORDER.get(p.get("label") or "", 4),
                                       -(p["confidence"] or 0), p["id"]))
        links = sorted((p for p in pts if p["kind"] == "linkedin"),
                       key=lambda p: (p["verify_status"] != "matched", -(p["confidence"] or 0), p["id"]))
        desig = row.get("desig") or []
        if isinstance(desig, str):
            try:
                desig = json.loads(desig)
            except ValueError:
                desig = [t for t in desig.strip("{}").split(",") if t]
        out[contact_id(crd, key)] = {
            "id": contact_id(crd, key), "crd": crd, "person_key": key,
            "indvl_pk": row.get("indvl_pk") or (key[2:] if key.startswith("i:") else None),
            "src": row.get("src") or ("sec" if key.startswith("i:") else "web"),
            "name": " ".join(str(row.get("name") or "").split()),
            "first": row.get("first_name"), "last": row.get("last_name"),
            "title": row.get("title"), "designations": row.get("designations"),
            "desig": list(desig), "has_disclosure": bool(row.get("has_disclosure")),
            "iapd_link": row.get("iapd_link"), "bcity": row.get("bcity"),
            "bstate": row.get("bstate"), "firm": firm, "firm_status": status.get(crd),
            "category": category.get(crd), "emails": emails, "phones": phones, "links": links,
            "role_inbox": any(p["kind"] == "email" and p.get("is_role") for p in pts)}
    return out


def _email_of(rec: dict) -> dict | None:
    return rec["emails"][0] if rec["emails"] else None


def _firm_phone(rec: dict) -> tuple[str | None, str | None]:
    return norm_phone(rec["firm"].get("phone"), country_code(rec["firm"].get("country")))


SHARED_TYPES = ("main", "toll_free")


def _phone_type(p: dict) -> str:
    label = p.get("label") or ""
    return label if label in PHONE_ORDER or label == "toll_free" else "unknown"


def _phone_of(rec: dict) -> tuple[str | None, str | None, str]:
    """(E.164, extension, phoneType) of the person's own best line. A
    switchboard (a main or toll-free line, or the firm's own number) is never
    the person's phone: it goes only as firm.phone (B018, contract v1.1)."""
    cc = country_code(rec["firm"].get("country"))
    firm = _firm_phone(rec)[0]
    for p in rec["phones"]:
        e164, ext = norm_phone(p["value"], cc)
        if e164 and e164 != firm and _phone_type(p) not in SHARED_TYPES:
            return e164, ext, _phone_type(p)
    return None, None, "unknown"


def split_name(rec: dict) -> tuple[str | None, str | None]:
    first, last = _cut(rec.get("first"), 100), _cut(rec.get("last"), 100)
    if not (first and last):
        from .contacts import unspaced
        from .names import person_name
        parts = person_name(rec.get("name") or "").split()
        if len(parts) == 1 and unspaced(parts[0]) and not last and (not first or first == parts[0]):
            # A Chinese or Korean name written as one word: the family name
            # is its first character (B064). The people index stores such a
            # web-only name whole as the first name, with no last name.
            return _cut(parts[0][1:], 100), parts[0][0]
        first = first or (parts[0] if parts else None)
        last = last or (parts[-1] if len(parts) > 1 else None)
    return _cut(first, 100), _cut(last, 100)


# ------------------------------------------------------------------ the payload (14.4)

SOURCE_WORDS = {"sec": "SEC IAPD", "adv": "Form ADV Schedule A",
                "web": "Firm website or web search"}
EMAIL_SOURCE = {"website": "email from the firm website", "vcard": "email from a vCard on the firm website",
                "brochure": "email from the firm's brochure", "directory": "email from a directory",
                "web_search": "email from web search", "adv": "email from Form ADV",
                "manual": "email added by hand", "pattern": "email confirmed with the mail server",
                "ai": "email from AI reading of the firm website",
                "ai_web": "email from AI research, confirmed with the mail server"}
VERIFICATION = {"valid": "valid", "catch_all": "risky"}


def _designations(text) -> list[str]:
    out = []
    for d in re.split(r"[,;/|]+", str(text or "")):
        d = " ".join(d.split())
        if d and len(d) <= 20 and d not in out:
            out.append(d)
    return out[:20]


def _tags(rec: dict, product: str | None) -> list[str]:
    from . import designations, products
    tags = []
    if product:
        tags.append(f"list:{products.product(product)['name']}")
    if product:
        mine = [designations.tag(product, d["key"]) for d in designations.for_product(product)]
    else:
        mine = [designations.tag(p, d["key"]) for p in products.product_keys()
                if products.product(p).get("family") != "ATS"
                for d in designations.for_product(p)]
    for t in rec["desig"]:
        if t in mine:
            tags.append(f"designation:{designations.label_of(t)}")
    if rec.get("category"):
        try:
            from . import firmtype
            tags.append(f"firm type:{firmtype.label(rec['category'])}")
        except Exception:
            pass
    if rec.get("has_disclosure"):
        tags.append("sec disclosure")
    seen, out = set(), []
    for t in tags:
        t = _cut(t, 50)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:20]


def anchor(person_key: str) -> str:
    """The id the firm page gives a person's card (firm_view._person_card).
    Letters of any script stay, so two people named in Chinese never share one."""
    return "p-" + re.sub(r"[\W_]+", "-", person_key.lower()).strip("-")


def build_contact(rec: dict, *, product: str | None = None, note: str | None = None,
                  base_url: str | None = None, extras: list[dict] | None = None) -> dict:
    """One contact as 14.4 describes it: normalised (14.5), within limits,
    null rather than a placeholder, and never a guess or a shared inbox.
    Every phone the person has goes, each typed (Cadence's suppression and
    dedupe judge each by its own type), and the firm's switchboard goes only
    as firm.phone. `extras` are the same person's rows at other firms
    (N092): their emails and phones go as extra details."""
    firm = rec["firm"]
    cc = country_code(firm.get("country"))
    first, last = split_name(rec)
    em = _email_of(rec)
    email = norm_email(em["value"]) if em else None
    extra = []
    for p in rec["emails"][1:] + [p for x in extras or [] for p in x["emails"]]:
        e = norm_email(p["value"])
        if e and e != email and e not in extra:
            extra.append(e)
    phone, ext, ptype = _phone_of(rec)
    more_phones, seen = [], {phone, _firm_phone(rec)[0]}
    for x in [rec] + list(extras or []):
        xcc = country_code(x["firm"].get("country"))
        for p in x["phones"]:
            e164, _ = norm_phone(p["value"], xcc)
            if e164 and e164 not in seen and e164 != _firm_phone(x)[0]:
                seen.add(e164)
                more_phones.append({"number": e164, "type": _phone_type(p)})
    matched = next((p for p in rec["links"] if p.get("verify_status") == "matched"), None)
    probable = next((p for p in rec["links"] if p.get("verify_status") != "matched"), None)
    lines = []
    if probable and not matched and norm_linkedin(probable["value"]):
        lines.append(f"Possible LinkedIn, not confirmed: https://www.{norm_linkedin(probable['value'])}")
    legal = _cut(firm.get("legal_name"), 200)
    fname = _firm_name(firm)
    if legal and fname and legal.lower() != fname.lower():
        from .names import nice_name
        lines.append(f"Legal name: {nice_name(legal)}")
    if rec.get("iapd_link"):
        lines.append(f"SEC record: {rec['iapd_link']}")
    notes = _notes(note, lines)
    src = SOURCE_WORDS.get(rec.get("src") or "", "Firm website or web search")
    if em:
        how = EMAIL_SOURCE.get(em.get("source") or "", "email from a public source")
        if em.get("verify_status") == "valid" and em.get("source") != "pattern":
            how += ", mailbox confirmed"
        src = f"{src}; {how}"
    found = em
    if not found and phone:
        found = next((p for p in rec["phones"] if norm_phone(p["value"], cc)[0] == phone), None)
    collected = str((found or {}).get("found_at") or "")[:10] or None
    url = None
    if base_url and base_url.lower().startswith("https://"):
        url = f"{base_url.rstrip('/')}/firm/{rec['crd']}#{anchor(rec['person_key'])}"
    from .names import nice_name
    from . import roles
    out = {
        "bellwetherContactId": rec["id"],
        "firstName": first, "lastName": last,
        "jobTitle": _cut(roles.clean_title(rec.get("title") or "", 200), 200),
        "email": email,
        "emailVerification": VERIFICATION.get((em or {}).get("verify_status"), "unknown") if email else "unknown",
        "additionalEmails": extra[:5],
        "phone": phone, "phoneType": ptype if phone else "unknown",
        "phoneExtension": ext,
        "additionalPhones": more_phones[:5],
        "linkedinUrl": norm_linkedin(matched["value"]) if matched else None,
        "city": _cut(nice_name(rec.get("bcity") or ""), 100),
        "state": _cut(rec.get("bstate"), 100),
        "country": cc,
        "individualCrd": norm_crd(rec.get("indvl_pk")),
        "designations": _designations(rec.get("designations")),
        "productInterest": _interest(product),
        "tags": _tags(rec, product),
        "notes": notes,
        "bellwetherUrl": url,
        "source": _cut(src, 200),
        "collectedAt": collected,
        "doNotContact": False,
        "firm": _firm(firm, cc),
    }
    # The status of an email or phone goes with it, and is left out without it.
    if not email:
        out.pop("emailVerification")
    if not phone:
        out.pop("phoneType")
    return out


def _firm_name(firm: dict) -> str | None:
    from .names import nice_name
    return _cut(nice_name(firm.get("business_name") or firm.get("legal_name") or ""), 200)


def _firm(firm: dict, cc: str | None) -> dict:
    from .names import nice_name
    site = (firm.get("website") or "").strip()
    if site and not re.match(r"^[a-z]+://", site, re.I):
        site = "https://" + site
    fphone, _ = norm_phone(firm.get("phone"), cc)
    return {"name": _firm_name(firm) or f"CRD {firm['crd']}", "crd": norm_crd(firm.get("crd")),
            "website": site or None, "domain": norm_domain(site),
            "city": _cut(nice_name(firm.get("city") or ""), 100), "state": _cut(firm.get("state"), 100),
            "country": cc, "phone": fphone,
            "aumUsd": int(firm["raum"]) if firm.get("raum") is not None else None,
            "employees": int(firm["total_employees"]) if firm.get("total_employees") is not None else None}


def _notes(note: str | None, lines: list[str]) -> str | None:
    """The user's note, then Bellwether's lines after a blank line. Within
    5000 characters, Bellwether's lines going first if anything has to."""
    mine = (note or "").strip()[:NOTE_MAX]
    ours = "\n".join(lines)
    room = 5000 - len(mine) - (2 if mine and ours else 0)
    ours = ours[:max(0, room)].rstrip()
    text = "\n\n".join(x for x in (mine, ours) if x)
    return text or None


def scrub(contact: dict) -> dict:
    """The last word on what leaves: never TOLLBOOTH, never the list:ATS tag
    (decision 25), whoever built the contact."""
    contact["productInterest"] = [p for p in contact.get("productInterest") or []
                                  if str(p).strip().upper() not in NEVER_PRODUCTS]
    contact["tags"] = [t for t in contact.get("tags") or []
                       if str(t).strip().lower() not in NEVER_TAGS]
    return contact


def account_email(account: dict | None) -> str | None:
    a = account or {}
    return norm_email(a.get("email")) or norm_email(a.get("login"))


def envelope(account: dict, owner_email: str | None, contacts: list[dict], pod: str | None = None) -> dict:
    """The 14.4 envelope. `pod` is the owner's pod as Cadence's owners list
    names it (push_pod), so a rep in several pods gets the contacts in the
    pod the sender chose, not the first of theirs by name."""
    me = account_email(account)
    pushed_by = {"email": me}
    if account.get("name"):
        pushed_by["name"] = _cut(account["name"], 200)
    if account.get("ms_oid"):
        pushed_by["microsoftObjectId"] = account["ms_oid"]
    owner = norm_email(owner_email)
    return {"pushedBy": pushed_by,
            "assignTo": {"email": owner} if owner and owner != me else None,
            "pod": pod or None,
            "contacts": [scrub(dict(c)) for c in contacts]}


def body_bytes(env: dict) -> bytes:
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ the HTTP client

class Answer:
    """What one call to Cadence came back with. Builds its own words for
    every failure, so nothing from the request (the key above all) can reach
    a message."""

    def __init__(self, kind: str, status: int | None = None, data=None,
                 retry_after: float | None = None):
        self.kind, self.status, self.data, self.retry_after = kind, status, data, retry_after

    @property
    def ok(self) -> bool:
        return self.kind == "http" and self.status == 200 and isinstance(self.data, dict)

    @property
    def code(self) -> str:
        if isinstance(self.data, dict) and isinstance(self.data.get("error"), dict):
            return str(self.data["error"].get("code") or "")
        return ""

    def error(self) -> str:
        if self.kind != "http":
            return self.kind
        return f"http_{self.status}" + (f" {self.code}" if self.code else "")


def _state_get(conn, k: str) -> str | None:
    r = conn.execute("SELECT v FROM cadence_state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def _state_set(conn, k: str, v: str | None) -> None:
    if v is None:
        conn.execute("DELETE FROM cadence_state WHERE k=?", (k,))
    else:
        conn.execute("INSERT INTO cadence_state (k, v) VALUES (?, ?)"
                     " ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))


def _reserve(conn, kind: str) -> float:
    """Wait for this process's turn to call Cadence, and return it. The slot
    is shared by every process through cadence_state (the later of now and the
    last slot plus the gap), taken with a compare-and-set, so the pace holds
    under 14.11 however many web workers and jobs send at once."""
    k = "push_slot" if kind == "push" else "status_slot"
    gap = PUSH_GAP_S if kind == "push" else STATUS_GAP_S
    for _ in range(200):
        now = _clock()
        old = _state_get(conn, k)
        if old is None:
            cur = conn.execute("INSERT INTO cadence_state (k, v) VALUES (?, ?) ON CONFLICT DO NOTHING",
                               (k, repr(now)))
            conn.commit()
            if cur.rowcount == 1:
                return now
            continue
        try:
            last = float(old)
        except ValueError:
            last = 0.0
        slot = max(now, last + gap)
        cur = conn.execute("UPDATE cadence_state SET v=? WHERE k=? AND v=?", (repr(slot), k, old))
        conn.commit()
        if cur.rowcount == 1:
            wait = slot - _clock()
            if wait > 0:
                _idle(conn)
                _sleep(wait)
            return slot
    return _clock()


def _idle(conn) -> None:
    """Commit, and hand a pooled connection back to the pool until the next
    statement, before waiting on Cadence or the clock (B016). A page held
    while Cadence is slow must never hold one of the pool's connections too."""
    conn.commit()
    release = getattr(conn, "release", None)
    if release:
        release()


def contracts(conn) -> set[str]:
    """The contract versions Cadence's last ping listed. A Cadence that lists
    none speaks v1 only."""
    try:
        v = _state_get(conn, "contracts")
    except Exception:
        conn.rollback()
        v = None
    return set((v or "1").split(","))


def speaks_11(conn) -> bool:
    return CONTRACT in contracts(conn)


def _fetch(method: str, url: str, body: bytes | None, headers: dict, limit: float) -> Answer:
    """One HTTP call that ends within `limit` seconds whatever the network
    does. requests' own timeout bounds each connect and each read, never the
    whole call and never the name lookup, so the call runs on a thread of its
    own and is given up on at the deadline. A thread given up on ends by itself
    when its socket times out."""
    import requests
    box: dict = {}

    def go():
        try:
            box["r"] = requests.request(method, url, data=body, headers=headers,
                                        timeout=(min(10.0, limit), limit), allow_redirects=False)
        except requests.Timeout:
            box["kind"] = "timeout"
        except Exception:
            box["kind"] = "connection"
    t = threading.Thread(target=go, daemon=True, name="cadence-call")
    t.start()
    t.join(limit)
    if t.is_alive():
        return Answer("timeout")
    if "r" not in box:
        return Answer(box.get("kind") or "connection")
    r = box["r"]
    try:
        data = r.json()
    except ValueError:
        data = None
    ra = None
    try:
        ra = float(r.headers.get("Retry-After")) if r.headers.get("Retry-After") else None
    except (TypeError, ValueError):
        ra = None
    return Answer("http", r.status_code, data, ra)


def _call(conn, method: str, path: str, body: bytes | None = None, *,
          idem_key: str | None = None, kind: str = "push", timeout: float | None = None) -> Answer:
    """One request to Cadence, paced (14.11), within a hard total deadline
    (TIMEOUT_S unless a shorter one is given), and with the database
    connection handed back while it is in flight."""
    _reserve(conn, kind)
    headers = {"X-Cadence-Api-Key": api_key(), "Accept": "application/json",
               "User-Agent": "Bellwether"}
    if speaks_11(conn):
        headers["X-Cadence-Contract"] = CONTRACT
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    _idle(conn)
    return _fetch(method, api_url() + API + path, body, headers, float(timeout or TIMEOUT_S))


def ping(conn, *, by: str = "system", timeout: float | None = None) -> tuple[bool, dict, str]:
    """Key and connectivity (14.3). (reachable and accepted, the answer,
    words). The answer is kept, with when and who asked, for Settings,
    Cadence; so is the list of contract versions Cadence speaks."""
    if not configured():
        return False, {}, problem() or "Cadence is not connected."
    limit = timeout or PING_TIMEOUT_S
    ans = _call(conn, "GET", "/ping", kind="status", timeout=limit)
    ok, data = False, {}
    if ans.ok:
        ok, data = True, ans.data
        if data.get("maxBatch"):
            _state_set(conn, "max_batch", str(int(data["maxBatch"])))
        listed = [str(v) for v in data.get("contracts") or [] if str(v).strip()]
        _state_set(conn, "contracts", ",".join(sorted(set(listed) | {"1"})))
        words = ("Cadence answered and accepted the key; Cold outreach takes up to "
                 f"{data.get('maxBatch') or CONTRACT_MAX} contacts a request"
                 + ("." if CONTRACT in listed else
                    ". It speaks contract v1 only, so a Do not contact set here is not sent to it yet."))
    elif ans.kind == "timeout":
        words = f"Cadence did not answer within {limit:g} seconds."
    elif ans.kind == "connection":
        words = "Could not reach Cadence at CADENCE_API_URL from this server."
    elif ans.status == 401:
        words = "Cadence refused the key in CADENCE_API_KEY."
    elif ans.status == 503 and ans.code == "integration_disabled":
        words = "Cadence has the Bellwether integration or Cold outreach switched off."
    else:
        words = f"Cadence answered {ans.error()}."
    _state_set(conn, "ping", json.dumps({"at": _iso(), "ok": ok, "words": words, "by": by}))
    conn.commit()
    return ok, data, words


def last_ping(conn) -> dict | None:
    """The last answer to ping: {at, ok, words, by}, or None when never tested."""
    try:
        v = _state_get(conn, "ping")
    except Exception:
        conn.rollback()
        return None
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def cannot_switch_on(conn) -> str | None:
    """Why sending may not be switched on yet, or None: the last test must
    have passed, within the last day (B020)."""
    p = last_ping(conn)
    if not p:
        return "Test the connection first. Sending is switched on only after a test passes."
    if not p.get("ok"):
        return "The last test failed: " + (p.get("words") or "no answer") + " Test again once it is fixed."
    t = _parse(p.get("at"))
    if not t or _now() - t > timedelta(hours=PING_FRESH_H):
        return "The last test that passed is more than a day old. Test the connection again."
    return None


def _chunk_size(conn) -> int:
    try:
        mb = int(_state_get(conn, "max_batch") or CONTRACT_MAX)
    except ValueError:
        mb = CONTRACT_MAX
    return max(1, min(CONTRACT_MAX, mb))


# ------------------------------------------------------------------ audit

def _event(conn, actor: str, crd: str, person_key: str, action: str, outcome: str | None = None,
           *, http: int | None = None, key: str | None = None, batch: str | None = None,
           detail: dict | None = None) -> None:
    conn.execute("INSERT INTO cadence_push_event (at, actor, crd, person_key, batch_id, action,"
                 " outcome, http_status, request_key, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (_iso(), actor or "system", crd, person_key, batch, action, outcome, http, key,
                  json.dumps(detail, ensure_ascii=False) if detail else None))


# ------------------------------------------------------------------ suppression

SCOPES = ("person", "individual", "email", "phone", "domain", "firm")


def suppress(conn, scope: str, value: str, *, channel: str = "all", reason: str = "other",
             source: str = "manual", note: str | None = None, by: str | None = None,
             for_id: str | None = None) -> bool:
    """A do-not-contact. Returns False when the same one is already active.
    `for_id` names the contact whose opt-out in Cadence wrote it."""
    if scope not in SCOPES or not value:
        return False
    channel = channel if channel in ("all", "email", "call") else "all"
    have = conn.execute("SELECT 1 FROM contact_suppression WHERE scope=? AND value=? AND channel=?"
                        " AND lifted_at IS NULL", (scope, value, channel)).fetchone()
    if have:
        return False
    conn.execute("INSERT INTO contact_suppression (scope, value, channel, reason, source, note,"
                 " set_by, set_at, for_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                 (scope, value, channel, reason, source, (note or "")[:300] or None, by, _iso(),
                  for_id))
    pair = parse_id(value) if scope == "person" else (value, "") if scope == "firm" else None
    _event(conn, by or "system", pair[0] if pair else "", pair[1] if pair else "", "suppress",
           f"{source}:{reason}", detail={"scope": scope, "channel": channel})
    return True


def suppress_person(conn, cid: str, *, reason: str, source: str, note: str | None, by: str | None,
                    emails=(), phones=(), indvl_pk: str | None = None) -> int:
    """An opt-out that belongs to the person, not to one row (N007, B002): the
    contact itself, the SEC individual, so every firm they work at shows it,
    and each of their emails and phones. Returns how many rows it added."""
    keys = [("person", cid)]
    if norm_crd(indvl_pk):
        keys.append(("individual", norm_crd(indvl_pk)))
    keys += [("email", e) for e in dict.fromkeys(norm_email(e) for e in emails) if e]
    keys += [("phone", p) for p in dict.fromkeys(phones) if p]
    return sum(1 for scope, value in keys
               if suppress(conn, scope, value, reason=reason, source=source, note=note, by=by,
                           for_id=cid if source == "cadence" else None))


def lift(conn, sid: int, by: str) -> tuple[bool, str]:
    """Lift a Do not contact set here (admins, after a confirm). An opt-out
    that came from Cadence is never lifted here: only Cadence can lift it
    (B074). Returns (lifted, words that say what happened)."""
    r = conn.execute("SELECT * FROM contact_suppression WHERE id=? AND lifted_at IS NULL",
                     (sid,)).fetchone()
    if not r:
        return False, "Already lifted."
    if r["source"] == "cadence":
        return False, "Opted out in Cadence; it can only be lifted there."
    conn.execute("UPDATE contact_suppression SET lifted_by=?, lifted_at=? WHERE id=?",
                 (by, _iso(), sid))
    pair = (parse_id(r["value"]) if r["scope"] == "person" else
            (r["value"], "") if r["scope"] == "firm" else None)
    _event(conn, by, pair[0] if pair else "", pair[1] if pair else "", "lift",
           f"{r['source']}:{r['reason']}", detail={"scope": r["scope"]})
    who = "Everyone at the firm" if r["scope"] == "firm" else "The person"
    if r["to_cadence"] == "sent":
        return True, (f"Lifted here. {who} can be sent again once nothing else blocks them; Cadence "
                      "keeps the opt-out it was sent, and only Cadence can lift that.")
    return True, f"Lifted. {who} can be sent to Cold outreach again once nothing else blocks them."


def lift_from_cadence(conn, cid: str, lifted_at: str | None, actor: str = "system") -> int:
    """Cadence lifted this contact's opt-out (v1.1 optOutLiftedAt) and no
    longer reports it opted out. Only the rows that opt-out wrote here go
    (for_id): never a Do not contact set in Bellwether, and never one another
    contact's opt-out wrote for the same person."""
    n = conn.execute("UPDATE contact_suppression SET lifted_by='cadence', lifted_at=? WHERE"
                     " source='cadence' AND for_id=? AND lifted_at IS NULL", (_iso(), cid)).rowcount
    if n:
        pair = parse_id(cid) or ("", "")
        _event(conn, actor, pair[0], pair[1], "lift", "cadence:opted_out",
               detail={"rows": n, "liftedAt": lifted_at})
    return n or 0


def suppressions(conn, recs: dict[str, dict]) -> dict[str, list[dict]]:
    """Active suppressions that touch each contact, by any of its scopes."""
    if not recs:
        return {}
    want: dict[tuple, set] = {}
    for cid, rec in recs.items():
        cc = country_code(rec["firm"].get("country"))
        keys = {("person", cid), ("firm", rec["crd"])}
        if norm_crd(rec.get("indvl_pk")):
            keys.add(("individual", norm_crd(rec.get("indvl_pk"))))
        for p in rec["emails"]:
            e = norm_email(p["value"])
            if e:
                keys.add(("email", e))
                keys.add(("domain", e.split("@")[1]))
        for p in rec["phones"]:
            e164, _ = norm_phone(p["value"], cc)
            if e164:
                keys.add(("phone", e164))
        dom = norm_domain(rec["firm"].get("website"))
        if dom:
            keys.add(("domain", dom))
        for k in keys:
            want.setdefault(k, set()).add(cid)
    out: dict[str, list] = {}
    for chunk in _chunks(sorted(want), 200):
        where, args = _pairs_sql(chunk, "scope", "value")
        for r in _rows(conn, f"SELECT * FROM contact_suppression WHERE lifted_at IS NULL AND {where}",
                       args):
            for cid in want.get((r["scope"], r["value"]), ()):
                out.setdefault(cid, []).append(r)
    return out


def supp_kind(rows: list[dict], crd: str = "", firm_name=None) -> dict:
    """The strongest suppression and its words (the plan, 7.1): Cadence's
    opt-out first ("Do not contact · Cadence", "at <firm>" when it came from
    the person's row at another firm), then one set here for the whole firm,
    then one set here for the person. firm_name(crd) names another firm."""
    cad = sorted((r for r in rows if r["source"] == "cadence"), key=lambda r: r.get("reason") != "erasure")
    if cad:
        r = cad[0]
        words = "Erased in Cadence" if r.get("reason") == "erasure" else "Do not contact · Cadence"
        other = (parse_id(r.get("for_id")) or ("", ""))[0]
        other = other if other and crd and other != crd else None
        if other and firm_name:
            words += f" at {firm_name(other)}"
        return {"rule": "opted_out", "source": "cadence", "row": r, "words": words, "other": other}
    firm = [r for r in rows if r["scope"] == "firm"]
    r = (firm or rows)[0]
    return {"rule": "do_not_contact", "source": "manual", "row": r,
            "words": "Do not contact · " + ("whole firm" if firm else "set here")}


# ------------------------------------------------------------------ the rules

def _name_of(login: str | None) -> str:
    try:
        from . import users
        return users.display_name(login) or (login or "someone")
    except Exception:
        return login or "someone"


def _zone():
    """Cadence's workspace time zone, so both apps write the same day and
    hour (B205), and "today" is the workspace's day (N093)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(WORKSPACE_TZ)
    except Exception:
        return timezone.utc


def _local(stamp: str | None) -> datetime | None:
    t = _parse(stamp)
    return t.astimezone(_zone()) if t else None


def _day(stamp: str | None) -> str:
    t = _local(stamp)
    return f"{t.day} {t.strftime('%b')}" if t else ""


def _clock_time(stamp: str | None) -> str:
    """17:05, in the workspace's time zone."""
    t = _local(stamp)
    return t.strftime("%H:%M") if t else ""


def _existing_words(row: dict) -> str | None:
    """Why an existing send stops this one, or None when it may go again."""
    st = row["state"]
    if st in REPUSH:
        return None
    who = _name_of(row["pushed_by"])
    owner = row.get("cadence_owner_name") or row.get("cadence_owner")
    if st in ACTIVE:
        return f"Being sent by {who} now"
    if st == "in_sandbox":
        return (f"{'In Cold outreach · ' + owner if owner else 'Unclaimed in Cold outreach'}, "
                f"sent by {who} on {_day(row.get('last_pushed_at') or row['first_pushed_at'])}")
    if st == "in_crm":
        return f"Already in CRM{' · ' + owner if owner else ''}"
    if st == "moved_to_crm":
        return "Moved to CRM"
    if st == "opted_out":
        return "Erased in Cadence" if row.get("erased_at") else "Do not contact · Cadence"
    if st == "rejected":
        return f"Not sent: {row.get('reason') or 'Cadence refused it'}"
    return f"Already sent by {who}"


def again_words(row: dict | None) -> str | None:
    """What to ask before someone sends again a person Cadence removed or
    has called (B086, the plan's 7.1), or None."""
    if not row or row["state"] not in REPUSH:
        return None
    bits = []
    if row["state"] == "removed":
        by = f" by {row['removed_by']}" if row.get("removed_by") else ""
        when = f" on {_day(row['removed_at'])}" if row.get("removed_at") else ""
        why = f": {row['removed_reason']}" if row.get("removed_reason") else ""
        bits.append(f"Removed from Cold outreach{by}{when}{why}.")
    if row.get("last_outcome"):
        caller = row.get("cadence_owner_name") or row.get("cadence_owner") or "Cadence"
        bits.append(f"{caller} called on {_day(row.get('last_outcome_at'))}: "
                    f"{outcome_words(row['last_outcome'])}.")
    return " ".join(bits) or None


def outcome_words(code: str | None) -> str:
    c = str(code or "")
    return OUTCOME_WORDS.get(c, c.replace("_", " ").capitalize())


def _reach(rec: dict) -> tuple[str | None, str | None]:
    """(email, own phone) a send would carry: one rule for the People chips
    and the dialog (B177)."""
    em = _email_of(rec)
    return (norm_email(em["value"]) if em else None), _phone_of(rec)[0]


def assess(conn, recs: dict[str, dict], ids: list[str]) -> dict:
    """Bellwether's own rules, in the plan's order, for every selected id:
    {"send": [rec...], "skipped": [(id, rule, words, rec|None)], "disqualified": {crd: name},
    "again": {id: question}, "extras": {id: [rec...]}, "people": n}.

    One send per SEC individual (N092): when two firm rows of one person are
    selected, the row at the firm their email belongs to goes, and the other
    row's email and phone go with it as extra details. Counts are of people.
    Writes nothing."""
    send, skipped, disq, again, extras = [], [], {}, {}, {}
    seen_mail, sent_pk = {}, {}
    pushes = {}
    pairs = [(r["crd"], r["person_key"]) for r in recs.values()]
    for chunk in _chunks(pairs, 100):
        where, args = _pairs_sql(chunk)
        for r in _rows(conn, f"SELECT * FROM cadence_push WHERE {where}", args):
            pushes[contact_id(r["crd"], r["person_key"])] = r
    indvl = sorted({r["indvl_pk"] for r in recs.values() if r.get("indvl_pk")})
    others_by_pk: dict[str, list] = {}
    if indvl:
        for r in _rows(conn, f"SELECT * FROM cadence_push WHERE indvl_pk IN ({','.join('?' * len(indvl))})"
                       " AND state NOT IN ('rejected', 'failed', 'removed')", indvl):
            others_by_pk.setdefault(r["indvl_pk"], []).append(r)
    mails = sorted({norm_email(p["value"]) for r in recs.values() for p in r["emails"][:1]} - {None})
    links = sorted({norm_linkedin(p["value"]) for r in recs.values() for p in r["links"]
                    if p.get("verify_status") == "matched"} - {None})
    by_mail, by_link = {}, {}
    if mails:
        for r in _rows(conn, f"SELECT * FROM cadence_push WHERE email IN ({','.join('?' * len(mails))})"
                       " AND state NOT IN ('rejected', 'failed', 'removed')", mails):
            by_mail.setdefault(r["email"], []).append(r)
    if links:
        for r in _rows(conn, f"SELECT * FROM cadence_push WHERE linkedin IN ({','.join('?' * len(links))})"
                       " AND state NOT IN ('rejected', 'failed', 'removed')", links):
            by_link.setdefault(r["linkedin"], []).append(r)
    supp = suppressions(conn, recs)

    def pk_of(cid):
        return norm_crd((recs.get(cid) or {}).get("indvl_pk"))

    def at_home(cid):
        rec = recs[cid]
        email, dom = _reach(rec)[0], norm_domain(rec["firm"].get("website"))
        return bool(email and dom and email.split("@")[1] == dom)

    # Each selected person's row that goes first: the one at their own firm.
    ids = list(dict.fromkeys(ids))
    lead: dict[str, str] = {}
    for cid in ids:
        pk = pk_of(cid)
        if pk and (pk not in lead or (at_home(cid) and not at_home(lead[pk]))):
            lead[pk] = cid
    leads = set(lead.values())
    # B173: when two people in one send share an email, the one whose firm's
    # website has the address's domain keeps it, whatever the selection order.
    home_mail: dict[str, str] = {}
    for cid in ids:
        if cid in recs and at_home(cid):
            home_mail.setdefault(_reach(recs[cid])[0], cid)
    where_in = {cid: n for n, cid in enumerate(ids)}
    for cid in sorted(ids, key=lambda i: (pk_of(i) is not None and i not in leads, where_in[i])):
        rec = recs.get(cid)
        if rec is None:
            skipped.append((cid, "not_found", "Not in Bellwether's people list yet. It is "
                            "rebuilt every half hour.", None))
            continue
        pk = pk_of(cid)
        if pk and pk in sent_pk:
            first = sent_pk[pk]
            extras.setdefault(first["id"], []).append(rec)
            skipped.append((cid, "same_person", f"Same person as {first['name']} in this send "
                            f"(at {_firm_name(first['firm']) or first['crd']}); this row's email and "
                            "phone go with them", rec))
            continue
        first, last = split_name(rec)
        ex = pushes.get(cid)
        if ex and ex.get("erased_at"):
            skipped.append((cid, "erased", "Erased in Cadence", rec))
            continue
        if cid in supp:
            kind = supp_kind(supp[cid], rec["crd"], firm_name=lambda c: _firm_label(conn, c))
            skipped.append((cid, kind["rule"], kind["words"], rec))
            continue
        if ex and _existing_words(ex):
            skipped.append((cid, "already_in_crm" if ex["state"] == "in_crm" else "in_cadence",
                            _existing_words(ex), rec))
            continue
        if (rec.get("firm_status") or "").lower() == "customer":
            skipped.append((cid, "customer", "Firm marked Customer", rec))
            continue
        if not (first and last):
            skipped.append((cid, "no_name", "Needs a first and last name", rec))
            continue
        email, phone = _reach(rec)
        if not (email or phone or _firm_phone(rec)[0]):
            skipped.append((cid, "no_contact", "Only a shared inbox, and no valid phone"
                            if rec.get("role_inbox") else "No email and no valid phone", rec))
            continue
        other = [r for r in others_by_pk.get(rec.get("indvl_pk") or "", []) if r["crd"] != rec["crd"]]
        if other:
            skipped.append((cid, "other_firm", f"Already sent from {_firm_label(conn, other[0]['crd'])}", rec))
            continue
        same = [r for r in by_mail.get(email or "", []) if contact_id(r["crd"], r["person_key"]) != cid]
        li = next((norm_linkedin(p["value"]) for p in rec["links"] if p.get("verify_status") == "matched"), None)
        same += [r for r in by_link.get(li or "", []) if contact_id(r["crd"], r["person_key"]) != cid]
        if same:
            r = same[0]
            skipped.append((cid, "same_email", f"Same {'email' if r['email'] == email else 'LinkedIn'} as "
                            f"{r['name']} at {_firm_label(conn, r['crd'])}, sent "
                            f"{_day(r.get('last_pushed_at') or r['first_pushed_at'])}", rec))
            continue
        keeper = home_mail.get(email or "")
        if email and keeper and keeper != cid:
            skipped.append((cid, "same_in_push", f"Same email as {recs[keeper]['name']} in this send, "
                            "whose firm uses that address", rec))
            continue
        if email and email in seen_mail:
            skipped.append((cid, "same_in_push", f"Same email as {seen_mail[email]} in this send", rec))
            continue
        if email:
            seen_mail[email] = rec["name"]
        if pk:
            sent_pk[pk] = rec
        if (rec.get("firm_status") or "").lower() == "disqualified":
            disq[rec["crd"]] = _firm_name(rec["firm"]) or rec["crd"]
        if again_words(ex):
            again[cid] = again_words(ex)
        send.append(rec)
    return {"send": send, "skipped": skipped, "disqualified": disq, "again": again, "extras": extras,
            "people": len({pk_of(i) or i for i in ids})}


def _firm_label(conn, crd: str) -> str:
    r = _rows(conn, "SELECT legal_name, business_name FROM firm_current WHERE crd=?", (crd,))
    return (_firm_name(r[0]) if r else None) or f"CRD {crd}"


# ------------------------------------------------------------------ the CRM check (14.14)

_PREVIEWS: dict[str, dict] = {}
_PREVIEW_LOCK = threading.Lock()


def _check(conn, env: dict) -> dict:
    """Cadence's check before a send: {"state": "ok" | "refused" | "unavailable",
    "results": by id, "message": words, "owner": the owner Cadence would give
    them, None for unclaimed, "owner_known": whether it said}. Never retried
    in the background."""
    out = {"state": "unavailable", "results": {}, "message": "", "owner": None, "owner_known": False}
    ans = _call(conn, "POST", "/contacts/check", body_bytes(env), kind="push")
    if ans.ok:
        owner = ans.data.get("owner")
        out.update(state="ok", owner=owner if isinstance(owner, dict) else None,
                   owner_known="owner" in ans.data,
                   results={str(r.get("bellwetherContactId")): r for r in ans.data.get("results") or []
                            if isinstance(r, dict)})
        return out
    if ans.kind == "http" and ans.status == 401:
        out.update(state="refused", message="Cadence refused Bellwether's key, so nothing can be sent. "
                                            "An admin checks Settings, Cadence.")
    elif ans.kind == "http" and ans.status == 503 and ans.code == "integration_disabled":
        out.update(state="refused", message="Cadence has the Bellwether integration or Cold outreach "
                                            "switched off, so nothing can be sent.")
    elif ans.kind == "http" and ans.status in (400, 413, 415, 422):
        out.update(state="refused", message=f"Cadence refused the check ({ans.error()}). An admin checks "
                                            "Settings, Cadence.")
    elif ans.kind == "http" and ans.status == 429:
        wait = f" in {_count(int(ans.retry_after), 'second')}" if ans.retry_after else " in a minute"
        out.update(message=f"Too many sends a minute for Cadence's CRM check: try again{wait}. "
                           "Cadence still refuses anyone already in the CRM when the send arrives.")
    else:
        out.update(message="Cadence's CRM check did not answer, so these contacts were not checked against "
                           "the CRM. Cadence still refuses anyone already in the CRM when the send arrives.")
    return out


def check_words(res: dict) -> tuple[str, str] | None:
    """(rule, words) when a check result stops a contact, else None."""
    r = res.get("result")
    if r == "already_in_crm":
        on = MATCHED_WORDS.get(res.get("matchedOn") or "", res.get("matchedOn") or "a field")
        owner = res.get("ownerName") or res.get("ownerEmail")
        return "already_in_crm", f"Already in CRM{' · ' + owner if owner else ''} · matched on {on}"
    if r == "rejected":
        return ("cadence_refuses", "Cadence would refuse: "
                + REASON_WORDS.get(res.get("reason") or "", res.get("reason") or "no reason given"))
    return None


def _remember_crm(conn, rec: dict, res: dict, login: str) -> None:
    """A person Cadence's check found already in the CRM is remembered (B083),
    so People and the firm page say "Already in CRM · owner" with Open in CRM,
    and offer no Send. A row Bellwether sent stays as it is."""
    on = MATCHED_WORDS.get(res.get("matchedOn") or "", res.get("matchedOn") or "")
    now = _iso()
    cur = conn.execute(
        "INSERT INTO cadence_push (crd, person_key, indvl_pk, name, state, reason, crm_id, crm_url,"
        " cadence_owner, cadence_owner_name, pushed_by, first_pushed_at, status_at, status_changed_at)"
        " VALUES (?,?,?,?,'in_crm',?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (crd, person_key) DO UPDATE SET state='in_crm', reason=excluded.reason,"
        " crm_id=excluded.crm_id, crm_url=excluded.crm_url, cadence_owner=excluded.cadence_owner,"
        " cadence_owner_name=excluded.cadence_owner_name, status_at=excluded.status_at,"
        " status_changed_at=excluded.status_changed_at"
        f" WHERE cadence_push.state IN ({','.join('?' * len(REPUSH))})",
        (rec["crd"], rec["person_key"], norm_crd(rec.get("indvl_pk")), rec["name"] or rec["id"],
         f"matched on {on} by Cadence's check" if on else "found by Cadence's check",
         res.get("crmPersonId"), _https(res.get("crmUrl")), norm_email(res.get("ownerEmail")),
         res.get("ownerName"), login, now, now, now) + REPUSH)
    if cur.rowcount:
        _event(conn, login, rec["crd"], rec["person_key"], "result", "already_in_crm",
               detail={"matchedOn": res.get("matchedOn"), "check": True})


def _remember(pid: str, data: dict) -> None:
    with _PREVIEW_LOCK:
        now = time.monotonic()
        for k in [k for k, v in _PREVIEWS.items() if now - v["t"] > PREVIEW_TTL_S]:
            _PREVIEWS.pop(k, None)
        while len(_PREVIEWS) > 200:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
        _PREVIEWS[pid] = dict(data, t=now)


def _recall(pid: str | None, login: str, sig: str | None = None) -> dict | None:
    """A preview this person made in the last 15 minutes: with `sig`, only
    one made with that product and owner."""
    if not pid:
        return None
    with _PREVIEW_LOCK:
        v = _PREVIEWS.get(pid)
    if v and v["login"] == login and (sig is None or v["sig"] == sig) \
            and time.monotonic() - v["t"] <= PREVIEW_TTL_S:
        return v
    return None


def _sig(product: str | None, owner: str | None) -> str:
    return f"{product or ''}|{norm_email(owner) or ''}"


def _who(rec: dict | None, cid: str) -> tuple[str, str]:
    if not rec:
        return cid, ""
    return rec["name"] or cid, _firm_name(rec["firm"]) or ""


def _owner_verdict(conn, owner: str | None, chk: dict | None, memo: dict | None) -> dict:
    """Who the contacts will belong to in Cadence (B022): the chosen owner,
    or nobody ("unclaimed") when Cadence says that person cannot own contacts.
    Cadence's owners list (v1.1) decides; else the check's own answer; else
    what an earlier check said about the same owner."""
    names = dict(owner_choices(conn))
    listed = owners(conn)
    if listed is not None:
        ok = any(o["email"] == owner for o in listed)
    elif chk and chk.get("state") == "ok" and chk.get("owner_known"):
        ok = bool(chk.get("owner"))
    elif memo and owner in (memo.get("owner_ok") or {}):
        ok = memo["owner_ok"][owner]
    else:
        return {"email": owner, "name": names.get(owner) or owner, "unclaimed": None}
    return {"email": owner, "name": names.get(owner) or owner, "unclaimed": not ok}


def day_start() -> datetime:
    """Midnight today in the workspace's time zone: the daily cap's day (N093)."""
    local = _now().astimezone(_zone())
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _count(n: int, one: str, many: str | None = None) -> str:
    """'1 second', '30 seconds': a count with its noun in the right number."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def cap_words(left: int) -> str:
    return (f"You can send {max(left, 0):,} more today (at most {_count(daily_per_user(), 'person', 'people')} a day; the day "
            "starts again at midnight, Cadence's time).")


def preview(conn, ids: list[str], *, account: dict, product: str | None = None,
            owner_email: str | None = None, note: str | None = None,
            base_url: str | None = None, check: bool = True, reuse: str | None = None,
            pod: str | None = None) -> dict:
    """What sending these ids would do: the contacts that would go, with the
    email and phone each carries and the check's warnings under each, and
    those that would not, with why. The contacts that pass Bellwether's rules
    go to Cadence's check (14.14), and anyone already in the CRM, or that
    Cadence would refuse, is not sent (decision 17); someone found in the CRM
    is remembered (B083), and that is all a preview writes. `reuse` names the
    preview this one only changes the owner or product of: its check stands
    (B065), so a change of owner costs Cadence nothing."""
    login = (account or {}).get("login") or ""
    product = push_product(product)
    ids = [i for i in dict.fromkeys(ids)][:CONTRACT_MAX * 2]
    me = account_email(account)
    owner = norm_email(owner_email) or me
    out = {"ok": True, "problem": problem(), "count": len(ids), "send": [], "skipped": [],
           "disqualified": [], "again": [], "check": {"state": "off", "message": ""},
           "product": product, "preview_id": None, "owner": owner,
           "pods": owner_pods(conn, owner), "pod": push_pod(conn, owner, pod)}
    if not me:
        out["problem"] = out["problem"] or ("Your Bellwether account needs an email address to send. "
                                            "An admin adds it in Settings, Users.")
    cap = max_per_push()
    if len(ids) > cap:
        out["problem"] = out["problem"] or (f"At most {cap:,} people go in one send. Select fewer, or "
                                            "narrow the filter.")
    recs = load_records(conn, ids)
    a = assess(conn, recs, ids)
    out["count"] = a["people"]
    for cid, rule, words, rec in a["skipped"]:
        name, firm = _who(rec, cid)
        out["skipped"].append({"id": cid, "name": name, "firm": firm, "rule": rule, "why": words})
    out["disqualified"] = sorted(a["disqualified"].values())
    contacts = [build_contact(r, product=product, note=note, base_url=base_url,
                              extras=a["extras"].get(r["id"])) for r in a["send"]]
    by_id = {c["bellwetherContactId"]: c for c in contacts}
    memo = _recall(reuse, login)
    if memo and memo.get("ids") != _hash(sorted(ids)):
        memo = None
    chk = None
    blocked: dict[str, tuple] = {}
    checked: dict[str, dict] = {}
    if memo and memo.get("checked") is not None:
        checked = memo["checked"]
        out["check"] = {"state": memo["state"], "message": memo.get("message") or ""}
    elif check and contacts and not out["problem"] and not held(conn):
        chk = _check(conn, envelope(account, owner, contacts, out["pod"]))
        checked = chk["results"]
        out["check"] = {"state": chk["state"], "message": chk["message"]}
        if chk["state"] == "refused":
            out["problem"] = chk["message"]
    if held(conn) and not out["problem"]:
        out["problem"] = _hold_words(held(conn))
    for rec in a["send"]:
        c = by_id[rec["id"]]
        res = checked.get(rec["id"]) or {}
        stop = check_words(res)
        name, firm = _who(rec, rec["id"])
        if stop:
            blocked[rec["id"]] = stop
            if res.get("result") == "already_in_crm":
                _remember_crm(conn, rec, res, login)
            out["skipped"].append({"id": rec["id"], "name": name, "firm": firm, "rule": stop[0],
                                   "why": stop[1], "owner": res.get("ownerName") or res.get("ownerEmail"),
                                   "crm_url": _https(res.get("crmUrl"))})
            continue
        fphone = (c.get("firm") or {}).get("phone")
        line = {"id": rec["id"], "name": name, "firm": firm, "email": c["email"],
                "email_status": ("Verified" if c.get("emailVerification") == "valid" else
                                 "Accept-all domain" if c.get("emailVerification") == "risky" else
                                 "Published") if c["email"] else None,
                "phone": readable_phone(c["phone"], c.get("phoneExtension")) if c["phone"] else
                (readable_phone(fphone) if fphone else None),
                "phone_type": c.get("phoneType") if c["phone"] else ("firm" if fphone else None),
                "disqualified": rec["crd"] in a["disqualified"], "note": None,
                "warnings": [warn_line(w) for w in res.get("warnings") or [] if isinstance(w, dict) and w.get("code")],
                "again": a["again"].get(rec["id"])}
        if a["extras"].get(rec["id"]):
            line["note"] = ("Also at " + ", ".join(_firm_name(x["firm"]) or x["crd"] for x in a["extras"][rec["id"]])
                            + ": sent once, with those details too")
        if res.get("result") == "duplicate_in_sandbox":
            had = res.get("ownerName") or res.get("ownerEmail")
            line["note"] = (f"Already in Cold outreach ({'owner: ' + had if had else 'unclaimed'}); the send "
                            "fills its empty fields")
        if line["again"]:
            out["again"].append({"id": rec["id"], "name": name, "why": line["again"]})
        out["send"].append(line)
    conn.commit()
    verdict = _owner_verdict(conn, owner, chk, memo)
    out["owner_verdict"] = verdict
    owner_ok = dict((memo or {}).get("owner_ok") or {})
    if verdict["unclaimed"] is not None:
        owner_ok[owner] = not verdict["unclaimed"]
    left = daily_per_user() - used_today(conn, login)
    out["left_today"] = max(left, 0)
    k = len(out["send"])
    if k and k > left and not out["problem"]:
        out["over_cap"] = f"{cap_words(left)} This would send {k:,}."
    pid = str(uuid.uuid4())
    _remember(pid, {"login": login, "sig": _sig(product, owner), "blocked": blocked,
                    "state": out["check"]["state"], "message": out["check"]["message"],
                    "checked": checked if out["check"]["state"] == "ok" else None,
                    "ids": _hash(sorted(ids)), "owner_ok": owner_ok})
    out["preview_id"] = pid
    out["can_send"] = bool(out["send"]) and not out["problem"] and not out.get("over_cap")
    out["nothing"] = None if out["can_send"] or out.get("over_cap") else (
        out["problem"] or ("Nobody here can be sent: each one's reason is listed." if out["skipped"]
                           else "Nobody selected."))
    who = ("unclaimed" if verdict["unclaimed"] else f"owned by {verdict['name']}")
    out["verdict"] = (f"{k:,} will go to Cold outreach, {who}."
                      + (f" {out['count'] - k:,} will not." if out["count"] > k else "")
                      ) if k and not out["problem"] else None
    return out


# ------------------------------------------------------------------ sending

def used_today(conn, login: str) -> int:
    """People this login sent today, the workspace's day (N093); one person
    sent twice counts once."""
    r = conn.execute("SELECT COUNT(DISTINCT crd || ':' || person_key) n FROM cadence_push_event"
                     " WHERE actor=? AND action='push' AND at >= ?", (login, _iso(day_start()))).fetchone()
    return int(r["n"] or 0)


def push(conn, ids: list[str], *, account: dict, product: str | None = None,
         owner_email: str | None = None, note: str | None = None,
         confirm_disqualified: bool = False, preview_id: str | None = None,
         origin: str = "people", filter_qs: str | None = None, base_url: str | None = None,
         inline: bool = True, background: bool = True, actor: str | None = None,
         daily_cap: bool = True, client_key: str | None = None, pod: str | None = None) -> dict:
    """Send these contacts. The rules run again, the contacts the preview's
    check stopped stay out (or the check runs now, without a fresh preview),
    each contact is claimed so nobody else sends it at the same moment, and
    the requests go: the first one inside this call, the rest and any retries
    on a background thread. Returns the batch as batch_view() shows it.
    `account` is who the send is in the name of (pushedBy); `actor`, when
    different, is who pressed the button (an admin's Retry): their send is
    logged as a resend and never counts against the rep's daily cap (B217).
    `confirm_disqualified` is the dialog's tick, and covers a firm marked
    Disqualified and a person Cadence removed or called (B086)."""
    login = (account or {}).get("login") or ""
    actor = actor or login
    why = problem()
    if why:
        return {"ok": False, "message": why}
    me = account_email(account)
    if not me:
        return {"ok": False, "message": "Your Bellwether account needs an email address to send. "
                                        "An admin adds it in Settings, Users."}
    ids = list(dict.fromkeys(i for i in ids if parse_id(i)))
    if not ids:
        return {"ok": False, "message": "Nobody selected to send."}
    cap = max_per_push()
    if len(ids) > cap:
        return {"ok": False, "message": f"At most {cap:,} people go in one send."}
    if held(conn):
        return {"ok": False, "message": _hold_words(held(conn))}
    owner = norm_email(owner_email)
    if owner and owner != me and owner not in {e for e, _ in owner_choices(conn)}:
        return {"ok": False, "message": "Choose an owner from the list."}
    product = push_product(product)
    pod = push_pod(conn, owner or me, pod)
    note = (note or "").strip()[:NOTE_MAX] or None
    if client_key:
        ex = _rows(conn, "SELECT id FROM cadence_batch WHERE client_key=? AND created_by=?",
                   (client_key[:64], actor))
        if ex:
            # The dialog pressed Send twice, or retried after a gateway error.
            return batch_view(conn, ex[0]["id"])
    recs = load_records(conn, ids)
    a = assess(conn, recs, ids)
    if (a["disqualified"] or a["again"]) and not confirm_disqualified:
        asks = []
        if a["disqualified"]:
            asks.append("a firm marked Disqualified: " + ", ".join(sorted(a["disqualified"].values())))
        if a["again"]:
            asks.append(f"{len(a['again']):,} {'person' if len(a['again']) == 1 else 'people'} Cadence "
                        "removed or has called")
        return {"ok": False, "needs_confirm": True,
                "message": "Tick the box to send anyway: " + "; ".join(asks) + "."}
    contacts = {r["id"]: build_contact(r, product=product, note=note, base_url=base_url,
                                       extras=a["extras"].get(r["id"]))
                for r in a["send"]}
    skipped = list(a["skipped"])
    memo = _recall(preview_id, login, _sig(product, owner))
    blocked = dict(memo["blocked"]) if memo else {}
    if memo is None and contacts:
        chk = _check(conn, envelope(account, owner, list(contacts.values()), pod))
        if chk["state"] == "refused":
            return {"ok": False, "message": chk["message"]}
        blocked = {cid: check_words(r) for cid, r in chk["results"].items() if check_words(r)}
        for cid, res in chk["results"].items():
            if res.get("result") == "already_in_crm" and cid in recs:
                _remember_crm(conn, recs[cid], res, login)
    for cid, stop in blocked.items():
        if cid in contacts:
            contacts.pop(cid)
            skipped.append((cid, stop[0], stop[1], recs.get(cid)))
    left = daily_per_user() - used_today(conn, login)
    if daily_cap and actor == login and len(contacts) > left:
        conn.commit()
        return {"ok": False, "message": f"{cap_words(left)} This would send {len(contacts):,}."}
    now = _iso()
    batch_id = str(uuid.uuid4())
    conn.execute("INSERT INTO cadence_batch (id, created_by, created_at, origin, filter_qs, product,"
                 " owner_email, note, requested, skipped, client_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (batch_id, actor, now, origin if origin in ("people", "firm", "retry") else "people",
                  (filter_qs or "")[:2000] or None, product, owner if owner != me else None, note,
                  a["people"], 0, (client_key or "")[:64] or None))
    claimed = []
    for cid, c in contacts.items():
        rec = recs[cid]
        row = conn.execute(
            "INSERT INTO cadence_push (crd, person_key, indvl_pk, name, email, phone, linkedin, state,"
            " product, owner_email, pushed_by, first_pushed_at, last_pushed_at, batch_id, claimed_at,"
            " payload_hash) VALUES (?,?,?,?,?,?,?,'sending',?,?,?,?,?,?,?,?)"
            " ON CONFLICT (crd, person_key) DO UPDATE SET state='sending', claimed_at=excluded.claimed_at,"
            " pushed_by=excluded.pushed_by, last_pushed_at=excluded.last_pushed_at, reason=NULL,"
            " name=excluded.name, email=excluded.email, phone=excluded.phone, linkedin=excluded.linkedin,"
            " product=excluded.product, owner_email=excluded.owner_email, batch_id=excluded.batch_id,"
            " request_key=NULL, payload_hash=excluded.payload_hash"
            f" WHERE cadence_push.state IN ({','.join('?' * len(REPUSH))})"
            " RETURNING id",
            (rec["crd"], rec["person_key"], norm_crd(rec.get("indvl_pk")), rec["name"] or cid,
             c["email"], c["phone"], c["linkedinUrl"], product, owner if owner != me else None,
             login, now, now, batch_id, now, _hash(c)) + REPUSH).fetchone()
        if row is None:
            ex = _rows(conn, "SELECT * FROM cadence_push WHERE crd=? AND person_key=?",
                       (rec["crd"], rec["person_key"]))
            skipped.append((cid, "in_cadence", (_existing_words(ex[0]) if ex else None)
                            or "Being sent by someone else now", rec))
            continue
        claimed.append(cid)
        # The daily cap counts 'push' rows by the sender's login; an admin's
        # Retry in someone else's name is a 'resend' and counts for nobody.
        _event(conn, login if actor == login else actor, rec["crd"], rec["person_key"],
               "push" if actor == login else "resend", batch=batch_id,
               detail={"fields": sorted(k for k, v in c.items() if v not in (None, [], "")),
                       **({"for": login} if actor != login else {})})
    for cid, rule, words, rec in skipped:
        pair = parse_id(cid) or ("", "")
        _event(conn, actor, pair[0], pair[1], "skipped", f"skipped:{rule}", batch=batch_id,
               detail={"why": words})
    keys = []
    size = _chunk_size(conn)
    for i, chunk in enumerate(_chunks(claimed, size)):
        env = envelope(account, owner, [contacts[c] for c in chunk], pod)
        body = body_bytes(env)
        key = str(uuid.uuid4())
        keys.append(key)
        conn.execute("INSERT INTO cadence_request (request_key, batch_id, body, body_sha256, contacts,"
                     " state, retries, first_sent_at, next_retry_at, claimed_at)"
                     " VALUES (?,?,?,?,?,?,0,?,?,?)",
                     (key, batch_id, body.decode("utf-8"), hashlib.sha256(body).hexdigest(), len(chunk),
                      "sending" if (i == 0 and inline) else "retry_wait", now, now,
                      now if (i == 0 and inline) else None))
        pairs = [parse_id(c) for c in chunk]
        where, args = _pairs_sql(pairs)
        conn.execute(f"UPDATE cadence_push SET request_key=?, state=? WHERE {where}",
                     [key, "sending" if (i == 0 and inline) else "retry_wait"] + args)
    conn.execute("UPDATE cadence_batch SET sent=?, skipped=? WHERE id=?",
                 (len(claimed), len(skipped), batch_id))
    conn.commit()
    if keys and inline:
        send_request(conn, keys[0], actor=actor)
    _finish_batch(conn, batch_id)
    conn.commit()
    if background and pending(conn, batch_id):
        threading.Thread(target=_drain_thread, args=(batch_id,), daemon=True,
                         name=f"cadence-{batch_id[:8]}").start()
    return batch_view(conn, batch_id)


def owners(conn) -> list[dict] | None:
    """Who can own contacts in Cadence, with their pods (v1.1 GET /owners),
    asked again after 10 minutes. None from a Cadence that speaks v1 only."""
    if conn is None or not configured() or not speaks_11(conn):
        return None
    try:
        cached, at = _state_get(conn, "owners"), _parse(_state_get(conn, "owners_at"))
    except Exception:
        conn.rollback()
        return None
    if cached and at and (_now() - at).total_seconds() < OWNERS_TTL_S:
        return json.loads(cached)
    ans = _call(conn, "GET", "/owners", kind="status", timeout=STATUS_TIMEOUT_S)
    if ans.ok:
        items = ans.data.get("owners") if isinstance(ans.data.get("owners"), list) else ans.data.get("results")
        listed = [{"email": norm_email(o.get("email")), "name": _cut(o.get("name"), 200) or norm_email(o.get("email")),
                   "pods": [str(p) for p in o.get("pods") or [] if p]}
                  for o in items or [] if isinstance(o, dict) and norm_email(o.get("email"))]
        _state_set(conn, "owners", json.dumps(listed, ensure_ascii=False))
        _state_set(conn, "owners_at", _iso())
        conn.commit()
        return listed
    if ans.kind == "http" and ans.status == 404:
        # It answered ping with 1.1 but has no owners list: back to v1.
        _state_set(conn, "contracts", "1")
        conn.commit()
    return json.loads(cached) if cached else None


def owner_pods(conn, owner: str | None) -> list[str]:
    """The pods Cadence's owners list (v1.1) gives this owner, as it wants
    them back in `pod`. Empty when Cadence speaks v1 only or does not list them."""
    o = norm_email(owner)
    return next((list(x.get("pods") or []) for x in owners(conn) or [] if x["email"] == o), []) if o else []


def push_pod(conn, owner: str | None, chosen: str | None = None) -> str | None:
    """The pod a send names (S3 asked Bellwether to send it): the one the
    sender chose among the owner's pods, else the owner's only pod, else none,
    and Cadence takes the owner's first pod by name."""
    pods = owner_pods(conn, owner)
    want = str(chosen or "").strip().lower()
    hit = next((p for p in pods if p.lower() == want), None) if want else None
    return hit or (pods[0] if len(pods) == 1 else None)


def owner_choices(conn=None) -> list[tuple[str, str]]:
    """The Owner field. People Cadence says can own contacts (v1.1), else
    active Bellwether users with an email address."""
    listed = owners(conn)
    if listed:
        return sorted({(o["email"], o["name"]) for o in listed}, key=lambda x: x[1].lower())
    from . import users
    out = []
    for u in users.all_users():
        e = account_email(u)
        if e and u.get("active", 1):
            out.append((e, u.get("name") or e))
    return sorted(set(out), key=lambda x: x[1].lower())


def pending(conn, batch_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) n FROM cadence_request WHERE state IN ('sending', 'retry_wait')"
    args: tuple = ()
    if batch_id:
        sql += " AND batch_id=?"
        args = (batch_id,)
    return int(conn.execute(sql, args).fetchone()["n"] or 0)


def _finish_batch(conn, batch_id: str) -> None:
    if not pending(conn, batch_id):
        conn.execute("UPDATE cadence_batch SET finished_at=? WHERE id=? AND finished_at IS NULL",
                     (_iso(), batch_id))


def _drain_thread(batch_id: str | None) -> None:
    """Retries, and requests after the first, for one batch (or, with None,
    the whole queue once a hold is lifted), in the web process. If the
    process dies, the cadence_sync job resends what is due."""
    from . import db
    c = db.connect()
    try:
        drain(c, batch_id=batch_id, max_s=900)
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        c.close()


# ------------------------------------------------------------------ sending one request

def held(conn) -> str | None:
    try:
        return _state_get(conn, "hold")
    except Exception:
        conn.rollback()
        return None


def _hold_words(why: str | None) -> str:
    if why == "integration_disabled":
        return ("Waiting: Cadence has the Bellwether integration or Cold outreach switched off. Sends "
                "go when it is back on; Settings, Cadence shows the queue.")
    return ("Waiting: Cadence refused Bellwether's key. Sends go once an admin fixes CADENCE_API_KEY; "
            "Settings, Cadence shows the queue.")


def drain_now() -> None:
    """Send the waiting queue now, in the background (B066): after a hold is
    lifted nobody should wait for the next sync."""
    threading.Thread(target=_drain_thread, args=(None,), daemon=True, name="cadence-drain").start()


def _hold(conn, why: str, actor: str) -> None:
    if _state_get(conn, "hold") != why:
        _state_set(conn, "hold", why)
        _state_set(conn, "hold_at", _iso())
        _event(conn, actor, "", "", "hold", why)


def lift_hold(conn, actor: str = "system") -> None:
    if _state_get(conn, "hold"):
        _event(conn, actor, "", "", "hold", "lifted")
    _state_set(conn, "hold", None)
    _state_set(conn, "hold_at", None)


def _request_people(conn, key: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM cadence_push WHERE request_key=?", (key,))


def send_request(conn, key: str, *, actor: str = "system") -> str:
    """Send one claimed request (state 'sending') and act on the answer as
    14.9 and 14.10 say. Returns the request's state afterwards."""
    req = _rows(conn, "SELECT * FROM cadence_request WHERE request_key=?", (key,))
    if not req or req[0]["state"] != "sending":
        return req[0]["state"] if req else "missing"
    req = req[0]
    first = _parse(req["first_sent_at"]) or _now()
    if _now() - first > timedelta(days=KEY_DAYS):
        _fail(conn, req, "expired", "Cadence keeps a request for 7 days; send again", actor)
        conn.commit()
        return "failed"
    body = (req["body"] or "").encode("utf-8")
    waited = 0.0
    while True:
        ans = _call(conn, "POST", "/contacts", body, idem_key=key, kind="push")
        if ans.kind == "http" and ans.status == 409 and ans.code == "idempotency_in_progress":
            if waited >= IN_PROGRESS_CAP_S:
                ans = Answer("timeout")
                break
            w = ans.retry_after if ans.retry_after and ans.retry_after > 0 else 5.0
            _idle(conn)
            _sleep(w)
            waited += w
            continue
        break
    try:
        if ans.ok:
            _apply_results(conn, req, ans.data, actor)
            state = "done"
        elif ans.kind in ("timeout", "connection"):
            state = _retry_later(conn, req, ans, actor)
        elif ans.status == 500:
            if str(req.get("last_error") or "").startswith("http_500"):
                _fail(conn, req, ans.error(), "Cadence failed twice with an internal error", actor,
                      http=500)
                state = "failed"
            else:
                state = _retry_later(conn, req, ans, actor)
        elif ans.status == 401:
            _hold(conn, "unauthorized", actor)
            _requeue(conn, req, "Waiting: Cadence refused Bellwether's key", ans.error())
            state = "retry_wait"
        elif ans.status == 503 and ans.code == "integration_disabled":
            _hold(conn, "integration_disabled", actor)
            _requeue(conn, req, "Waiting: Cadence has the integration switched off", ans.error())
            state = "retry_wait"
        elif ans.status == 429 or (ans.status or 0) >= 500:
            # rate_limited, unavailable, and any other 5xx: same key, same body.
            state = _retry_later(conn, req, ans, actor)
        else:
            words = {"idempotency_conflict": "Cadence saw this request before with a different body",
                     "invalid_json": "Cadence could not read the request",
                     "payload_too_large": "the request was too large for Cadence",
                     "unsupported_media_type": "Cadence refused the request's format",
                     "invalid_request": "Cadence refused the request as invalid"}.get(
                ans.code, f"Cadence answered {ans.error()}")
            _fail(conn, req, ans.error(), words, actor, http=ans.status)
            state = "failed"
        _finish_batch(conn, req["batch_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return state


def _requeue(conn, req: dict, reason: str, error: str) -> None:
    """Back in the queue with its key and body, no retry used (a hold)."""
    now = _iso()
    conn.execute("UPDATE cadence_request SET state='retry_wait', next_retry_at=?, claimed_at=NULL,"
                 " last_error=? WHERE request_key=?", (now, error, req["request_key"]))
    conn.execute("UPDATE cadence_push SET state='retry_wait', reason=?, claimed_at=NULL"
                 " WHERE request_key=? AND state IN ('sending', 'retry_wait')",
                 (reason, req["request_key"]))


def _retry_later(conn, req: dict, ans: Answer, actor: str) -> str:
    n = int(req["retries"] or 0) + 1
    if n > len(RETRY_STEPS):
        _fail(conn, req, ans.error(), "Cadence did not answer after three retries", actor,
              http=ans.status)
        return "failed"
    delay = float(RETRY_STEPS[n - 1])
    if ans.retry_after:
        delay = max(delay, ans.retry_after)
        until = _now() + timedelta(seconds=ans.retry_after)
        cur = _state_get(conn, "backoff_until")
        if not cur or (_parse(cur) or _now()) < until:
            _state_set(conn, "backoff_until", _iso(until))
    due = _now() + timedelta(seconds=delay)
    first = _parse(req["first_sent_at"]) or _now()
    if due - first > timedelta(days=KEY_DAYS):
        _fail(conn, req, "expired", "Cadence keeps a request for 7 days; send again", actor)
        return "failed"
    conn.execute("UPDATE cadence_request SET state='retry_wait', retries=?, next_retry_at=?,"
                 " claimed_at=NULL, last_error=? WHERE request_key=?",
                 (n, _iso(due), ans.error(), req["request_key"]))
    # Said for what it was (B065, B085): a busy Cadence is not a silent one.
    if ans.status == 429:
        why = "Too many sends a minute for Cadence"
    elif ans.kind == "http" and (ans.status or 0) >= 500:
        why = f"Cadence had a problem ({ans.error()})"
    else:
        why = "Cadence did not answer"
    conn.execute("UPDATE cadence_push SET state='retry_wait', claimed_at=NULL, reason=?"
                 " WHERE request_key=? AND state IN ('sending', 'retry_wait')",
                 (f"{why}; trying again at {_clock_time(_iso(due))}", req["request_key"]))
    for p in _request_people(conn, req["request_key"]):
        _event(conn, actor, p["crd"], p["person_key"], "retry", ans.error(), http=ans.status,
               key=req["request_key"], batch=req["batch_id"], detail={"retry": n, "after_s": delay})
    return "retry_wait"


def _fail(conn, req: dict, error: str, words: str, actor: str, http: int | None = None) -> None:
    now = _iso()
    conn.execute("UPDATE cadence_request SET state='failed', body=NULL, claimed_at=NULL,"
                 " last_error=?, finished_at=? WHERE request_key=?", (error, now, req["request_key"]))
    for p in _request_people(conn, req["request_key"]):
        if p["state"] not in ACTIVE:
            continue
        conn.execute("UPDATE cadence_push SET state='failed', reason=?, claimed_at=NULL WHERE id=?",
                     (words, p["id"]))
        _event(conn, actor, p["crd"], p["person_key"], "result", f"failed:{error}", http=http,
               key=req["request_key"], batch=req["batch_id"], detail={"reason": words})


def _apply_results(conn, req: dict, data: dict, actor: str) -> None:
    """One answer per contact (14.8), read the same whether replayed or not."""
    now = _iso()
    people = {contact_id(p["crd"], p["person_key"]): p for p in _request_people(conn, req["request_key"])}
    replayed = bool(data.get("replayed"))
    answered = set()
    for res in data.get("results") or []:
        if not isinstance(res, dict):
            continue
        cid = str(res.get("bellwetherContactId") or "")
        p = people.get(cid)
        if not p or p["state"] not in ACTIVE:
            continue
        answered.add(cid)
        r = res.get("result")
        detail = {k: res.get(k) for k in ("reason", "detail", "matchedOn", "cadenceContactId",
                                          "crmPersonId", "filledFields") if res.get(k)}
        warns = [w.get("code") for w in res.get("warnings") or [] if isinstance(w, dict)]
        if warns:
            detail["warnings"] = warns
        if replayed:
            detail["replayed"] = True
        if r in ("created", "duplicate_in_sandbox"):
            maybe = next((w.get("crmPersonId") for w in res.get("warnings") or []
                          if isinstance(w, dict) and w.get("code") == "possible_crm_match"), None)
            conn.execute("UPDATE cadence_push SET state='in_sandbox', cadence_id=?, cadence_owner=?,"
                         " cadence_owner_name=?, pushed_at=?, reason=NULL, claimed_at=NULL,"
                         " status_changed_at=?, cadence_url=COALESCE(?, cadence_url),"
                         " possible_crm_id=COALESCE(?, possible_crm_id) WHERE id=?",
                         (res.get("cadenceContactId"), norm_email(res.get("ownerEmail")),
                          res.get("ownerName"), now, now, _https(res.get("cadenceUrl")), maybe, p["id"]))
            outcome = r
        elif r == "already_in_crm":
            on = MATCHED_WORDS.get(res.get("matchedOn") or "", res.get("matchedOn") or "")
            conn.execute("UPDATE cadence_push SET state='in_crm', crm_id=?, crm_url=?, cadence_owner=?,"
                         " cadence_owner_name=?, reason=?, claimed_at=NULL, status_changed_at=?"
                         " WHERE id=?",
                         (res.get("crmPersonId"), _https(res.get("crmUrl")),
                          norm_email(res.get("ownerEmail")), res.get("ownerName"),
                          f"matched on {on}" if on else None, now, p["id"]))
            outcome = r
        elif r == "rejected" and res.get("reason") == "internal_error":
            # v1.1: Cadence failed on this one contact and finished the rest
            # (B012). Its own bug, so the contact may be sent again.
            conn.execute("UPDATE cadence_push SET state='failed', reason=?, claimed_at=NULL WHERE id=?",
                         (REASON_WORDS["internal_error"], p["id"]))
            outcome = "rejected:internal_error"
        elif r == "rejected":
            reason = res.get("reason") or ""
            words = REASON_WORDS.get(reason, reason or "no reason given")
            if reason == "invalid_field" and res.get("detail"):
                words = f"a required field is missing: {res['detail']}"
            conn.execute("UPDATE cadence_push SET state='rejected', reason=?, claimed_at=NULL,"
                         " status_changed_at=? WHERE id=?", (words[:300], now, p["id"]))
            if reason == "suppressed":
                suppress_person(conn, cid, reason="opted_out", source="cadence", by=actor,
                                note="Cadence refused the send: on its do-not-contact list",
                                emails=[p.get("email")], indvl_pk=p.get("indvl_pk"))
            outcome = f"rejected:{reason}"
        else:
            conn.execute("UPDATE cadence_push SET state='failed', reason=?, claimed_at=NULL WHERE id=?",
                         ("Cadence answered with a result Bellwether does not know", p["id"]))
            outcome = "failed:unknown_result"
        _event(conn, actor, p["crd"], p["person_key"], "result", outcome, http=200,
               key=req["request_key"], batch=req["batch_id"], detail=detail or None)
    for cid, p in people.items():
        if cid not in answered and p["state"] in ACTIVE:
            conn.execute("UPDATE cadence_push SET state='failed', reason=?, claimed_at=NULL WHERE id=?",
                         ("Cadence did not answer for this contact", p["id"]))
            _event(conn, actor, p["crd"], p["person_key"], "result", "failed:no_result", http=200,
                   key=req["request_key"], batch=req["batch_id"])
    conn.execute("UPDATE cadence_request SET state='done', body=NULL, claimed_at=NULL, finished_at=?,"
                 " last_error=NULL WHERE request_key=?", (now, req["request_key"]))


def _https(url) -> str | None:
    u = str(url or "")
    return u[:500] if u.lower().startswith("https://") else None


def claim_due(conn, key: str) -> bool:
    """Move one due request from retry_wait to sending. Exactly one sender
    wins (the UPDATE's own WHERE), so a request is never sent twice at once."""
    now = _iso()
    cur = conn.execute("UPDATE cadence_request SET state='sending', claimed_at=? WHERE request_key=?"
                       " AND state='retry_wait' AND next_retry_at <= ?", (now, key, now))
    if cur.rowcount != 1:
        conn.commit()
        return False
    conn.execute("UPDATE cadence_push SET state='sending', claimed_at=? WHERE request_key=?"
                 " AND state='retry_wait'", (now, key))
    conn.commit()
    return True


def drain(conn, *, batch_id: str | None = None, max_s: float | None = None,
          actor: str = "system") -> int:
    """Send every waiting request as it falls due, honouring Retry-After and
    the hold. Returns how many requests were sent."""
    started, sent, idle = _clock(), 0, 0
    while idle < 50:
        if held(conn):
            break
        sql = "SELECT request_key, next_retry_at FROM cadence_request WHERE state='retry_wait'"
        args: tuple = ()
        if batch_id:
            sql += " AND batch_id=?"
            args = (batch_id,)
        rows = _rows(conn, sql + " ORDER BY next_retry_at, request_key", args)
        conn.commit()
        if not rows:
            break
        due = min(_parse(r["next_retry_at"]) or _now() for r in rows)
        back = _parse(_state_get(conn, "backoff_until"))
        conn.commit()
        wait = max((due - _now()).total_seconds(), ((back - _now()).total_seconds() if back else 0), 0)
        if max_s is not None and _clock() - started + wait > max_s:
            break
        if wait > 0:
            _idle(conn)
            _sleep(wait)
        for r in rows:
            if (_parse(r["next_retry_at"]) or _now()) <= _now() and claim_due(conn, r["request_key"]):
                send_request(conn, r["request_key"], actor=actor)
                sent += 1
                idle = 0
                break
        else:
            # Another sender took them, or the clock has not quite reached
            # them: a short pause, never a hot loop.
            idle += 1
            _idle(conn)
            _sleep(0.5)
    return sent


def unstick(conn, actor: str = "system") -> dict:
    """Claims a dead process left behind, and keys too old to retry."""
    now = _now()
    stale = _iso(now - timedelta(seconds=STALE_CLAIM_S))
    n = conn.execute("UPDATE cadence_request SET state='retry_wait', next_retry_at=?, claimed_at=NULL"
                     " WHERE state='sending' AND claimed_at < ?", (_iso(now), stale)).rowcount
    conn.execute("UPDATE cadence_push SET state='retry_wait', claimed_at=NULL WHERE state='sending'"
                 " AND claimed_at < ? AND request_key IN (SELECT request_key FROM cadence_request"
                 " WHERE state='retry_wait')", (stale,))
    orphans = _rows(conn, "SELECT * FROM cadence_push WHERE state IN ('sending', 'retry_wait')"
                    " AND claimed_at < ? AND (request_key IS NULL OR request_key NOT IN"
                    " (SELECT request_key FROM cadence_request WHERE state IN ('sending', 'retry_wait')))",
                    (stale,))
    for p in orphans:
        conn.execute("UPDATE cadence_push SET state='failed', reason=?, claimed_at=NULL WHERE id=?",
                     ("The send did not finish; send again", p["id"]))
        _event(conn, actor, p["crd"], p["person_key"], "result", "failed:unfinished")
    expired = 0
    for req in _rows(conn, "SELECT * FROM cadence_request WHERE state IN ('sending', 'retry_wait')"
                     " AND first_sent_at < ?", (_iso(now - timedelta(days=KEY_DAYS)),)):
        _fail(conn, req, "expired", "Cadence keeps a request for 7 days; send again", actor)
        expired += 1
    conn.commit()
    return {"unstuck": n or 0, "orphans": len(orphans), "expired": expired}


def retry_failed(conn, actor: str, account: dict | None = None, background: bool = True,
                 confirm: bool = False) -> dict:
    """Settings, Cadence, Retry: failed contacts go again as new requests with
    new keys, still in the name of whoever sent them (an admin's retry does
    not make the admin their owner). The same checks run as on any send, the
    Disqualified question included, and the resend counts against nobody's
    daily cap (B217). The contract's dedupe means none becomes a second
    contact. Returns {"sent": n, "ask": words when the admin must tick first}."""
    from . import users
    rows = _rows(conn, "SELECT crd, person_key, product, owner_email, pushed_by FROM cadence_push"
                 " WHERE state='failed'")
    conn.commit()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["pushed_by"], r["product"], r["owner_email"]), []).append(
            contact_id(r["crd"], r["person_key"]))
    if not confirm:
        ids = [i for g in groups.values() for i in g]
        a = assess(conn, load_records(conn, ids), ids)
        if a["disqualified"] or a["again"]:
            asks = []
            if a["disqualified"]:
                asks.append("firms now marked Disqualified: " + ", ".join(sorted(a["disqualified"].values())))
            if a["again"]:
                asks.append(f"{len(a['again']):,} removed or called in Cadence since")
            return {"sent": 0, "ask": "Tick to retry anyway: " + "; ".join(asks) + "."}
    n = 0
    for (login, product, owner), ids in groups.items():
        acct = users.get(login) if login else None
        if not account_email(acct):
            acct = account or {}
        for chunk in _chunks(ids, max_per_push()):
            res = push(conn, chunk, account=acct, product=product, owner_email=owner,
                       confirm_disqualified=True, origin="retry", actor=actor, daily_cap=False,
                       inline=False, background=background)
            n += sum(1 for r in res.get("results") or [] if r.get("state") != "skipped") if res.get("ok") else 0
    return {"sent": n, "ask": None}


# ------------------------------------------------------------------ status (14.12)

def _erase_details(conn, row: dict) -> None:
    """Erased in Cadence (v1.1 erasedAt, N039): blank the person's emails and
    phones here too, at every firm they work at, and what was sent. The
    public SEC record and the do-not-contact stay."""
    keys = [(row["crd"], row["person_key"])]
    if row.get("indvl_pk"):
        keys += [(r["crd"], r["person_key"]) for r in _rows(
            conn, "SELECT DISTINCT crd, person_key FROM contact_point WHERE person_key=?",
            (f"i:{row['indvl_pk']}",))]
    # People reads its email and phone from the index, which only its next rebuild would clear: blank
    # them there now too, so the erased details never show meanwhile. The index's columns come from an
    # empty read inside a savepoint, so a missing index never undoes the rest of this sync's writes.
    try:
        conn.execute("SAVEPOINT erase_index")
        have = {d[0] for d in conn.execute("SELECT * FROM people_index WHERE 1=0").description or []}
        conn.execute("RELEASE SAVEPOINT erase_index")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT erase_index")
        have = set()
    blank = [f"{col}={val}" for col, val in (("email", "NULL"), ("email_status", "NULL"), ("email_source", "NULL"),
             ("phone", "NULL"), ("phone_label", "NULL"), ("has_email", "0"), ("has_direct", "0"),
             ("has_phone", "0")) if col in have]
    for crd, key in dict.fromkeys(keys):
        conn.execute("DELETE FROM contact_point WHERE crd=? AND person_key=? AND kind IN ('email', 'phone')",
                     (crd, key))
        if blank:
            conn.execute(f"UPDATE people_index SET {', '.join(blank)} WHERE crd=? AND person_key=?", (crd, key))
    conn.execute("UPDATE cadence_push SET email=NULL, phone=NULL, linkedin=NULL WHERE id=?", (row["id"],))


def _bounced(conn, row: dict, item: dict) -> None:
    """A bounce Cadence saw (v1.1 emailBounced, N040): that address is marked
    invalid here, so no send or export offers it again."""
    addr = item.get("emailBounced")
    addr = norm_email(addr) if isinstance(addr, str) else row.get("email")
    if not addr:
        return
    conn.execute("UPDATE contact_point SET verify_status='invalid', verify_detail=?, verified_at=?"
                 " WHERE kind='email' AND value=? AND verify_status != 'invalid'",
                 ("Bounced in Cadence", _iso(), addr))


V11 = {"cadenceUrl": "cadence_url", "removedBy": "removed_by", "removedAt": "removed_at",
       "removedReason": "removed_reason", "erasedAt": "erased_at", "emailBouncedAt": "email_bounced_at",
       "possibleCrmPersonId": "possible_crm_id", "lastOutcome": "last_outcome",
       "lastOutcomeAt": "last_outcome_at", "optOutLiftedAt": "opt_out_lifted_at"}


def apply_status(conn, item: dict, actor: str = "system", by_id: bool = False) -> str | None:
    """One status from Cadence. Returns what changed in words a person
    reads: the new state, or "owner" when only the owner changed (B066)."""
    cid = str(item.get("bellwetherContactId") or "")
    pair = parse_id(cid)
    status = item.get("status")
    if not pair:
        return None
    rows = _rows(conn, "SELECT * FROM cadence_push WHERE crd=? AND person_key=?", pair)
    row = rows[0] if rows else None
    if status == "opted_out":
        # Decision 23: an opt-out in Cadence shows here, whatever Bellwether
        # holds about the push, and it belongs to the person (N007).
        emails = [row.get("email")] if row else []
        phones = [row.get("phone")] if row else []
        if row:
            for p in _rows(conn, "SELECT kind, value FROM contact_point WHERE crd=? AND person_key=?"
                                 " AND kind IN ('email', 'phone')", pair):
                (emails if p["kind"] == "email" else phones).append(
                    p["value"] if p["kind"] == "email" else norm_phone(p["value"])[0])
        suppress_person(conn, cid, reason="erasure" if item.get("erasedAt") else "opted_out",
                        source="cadence", note="Erased in Cadence" if item.get("erasedAt")
                        else "Opted out in Cadence", by=actor, emails=emails, phones=phones,
                        indvl_pk=(row or {}).get("indvl_pk") or (pair[1][2:] if pair[1].startswith("i:") else None))
    elif item.get("optOutLiftedAt"):
        lift_from_cadence(conn, cid, item.get("optOutLiftedAt"), actor)
    if row is None:
        return None
    now = _iso()
    if status == "not_found":
        # A refusal, a CRM match or an opt-out never turns into a failure
        # because Cadence has no contact for it (B015). Only a contact
        # Bellwether believes is in Cold outreach can have been lost.
        if by_id and row["state"] == "in_sandbox":
            conn.execute("UPDATE cadence_push SET state='failed', reason=?, status_at=? WHERE id=?",
                         ("Cadence has no record of it", now, row["id"]))
            _event(conn, actor, pair[0], pair[1], "status", "not_found")
            return "failed"
        conn.execute("UPDATE cadence_push SET status_at=? WHERE id=?", (now, row["id"]))
        return None
    new = STATUS_STATE.get(status or "")
    if not new or row["state"] in ACTIVE:
        conn.execute("UPDATE cadence_push SET status_at=? WHERE id=?", (now, row["id"]))
        return None
    sets = {"state": new, "status_at": now,
            "status_changed_at": item.get("statusChangedAt") or now,
            "cadence_owner": norm_email(item.get("ownerEmail")),
            "cadence_owner_name": item.get("ownerName"),
            # The CRM fields follow the status row, so an Undo in Cadence
            # clears them here (N037).
            "crm_id": item.get("crmPersonId") if new in ("moved_to_crm", "in_crm") else None,
            "crm_url": _https(item.get("crmUrl")) if new in ("moved_to_crm", "in_crm") else None,
            "moved_at": item.get("movedToCrmAt") if new == "moved_to_crm" else None}
    if item.get("cadenceContactId"):
        sets["cadence_id"] = item["cadenceContactId"]
    if new == "rejected" and item.get("reason"):
        sets["reason"] = REASON_WORDS.get(item["reason"], item["reason"])[:300]
    # Contract v1.1 fields, each only when Cadence sent it: a v1 answer
    # leaves what Bellwether knew alone.
    for field, col in V11.items():
        if field in item:
            sets[col] = _https(item[field]) if field == "cadenceUrl" else item[field]
    if item.get("emailBounced") and not item.get("emailBouncedAt"):
        sets["email_bounced_at"] = row.get("email_bounced_at") or now
    if new != row["state"] and "reason" not in sets:
        sets["reason"] = None
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE cadence_push SET {cols} WHERE id=?", list(sets.values()) + [row["id"]])
    if item.get("erasedAt") and not row.get("erased_at"):
        _erase_details(conn, row)
    if item.get("emailBounced") and not row.get("email_bounced_at"):
        _bounced(conn, row, item)
    if new != row["state"]:
        undone = row["state"] == "moved_to_crm" and new == "in_sandbox"
        _event(conn, actor, pair[0], pair[1], "status", "move_undone" if undone else status,
               detail={"from": row["state"], "callCount": item.get("callCount")})
        return new
    if sets["cadence_owner"] != row.get("cadence_owner") and new == "in_sandbox":
        _event(conn, actor, pair[0], pair[1], "status", "owner",
               detail={"owner": sets["cadence_owner_name"] or sets["cadence_owner"]})
        return "owner"
    return None


def refresh(conn, ids: list[str], actor: str) -> dict:
    """Cadence's status for these contacts, by id (14.12), when a person opens
    a firm's People & contacts or presses Refresh. Contacts read in the last
    minute are not asked again, and the read gives up after 20 seconds: a
    page never waits on Cadence longer than that."""
    if problem() or held(conn):
        return {"ok": False, "message": problem() or _hold_words(held(conn)), "changed": 0}
    pairs = [p for p in dict.fromkeys(parse_id(i) for i in ids) if p][:CONTRACT_MAX]
    fresh = _iso(_now() - timedelta(seconds=BY_ID_FRESH_S))
    ask = []
    for chunk in _chunks(pairs, 100):
        where, args = _pairs_sql(chunk)
        for r in _rows(conn, f"SELECT crd, person_key, status_at FROM cadence_push WHERE {where}", args):
            if not r["status_at"] or r["status_at"] < fresh:
                ask.append(contact_id(r["crd"], r["person_key"]))
    conn.commit()
    changed, owners_changed = 0, 0
    for chunk in _chunks(ask, CONTRACT_MAX):
        ans = _call(conn, "POST", "/contacts/status",
                    body_bytes({"bellwetherContactIds": chunk}), kind="status", timeout=STATUS_TIMEOUT_S)
        if not ans.ok:
            if ans.status == 401:
                _hold(conn, "unauthorized", actor)
            elif ans.status == 503 and ans.code == "integration_disabled":
                _hold(conn, "integration_disabled", actor)
            conn.commit()
            return {"ok": False, "changed": changed,
                    "message": ("Cadence did not answer the status read within "
                                f"{STATUS_TIMEOUT_S} seconds." if ans.kind == "timeout" else
                                "Cadence did not answer the status read.")}
        for item in ans.data.get("results") or []:
            did = apply_status(conn, item, actor, by_id=True) if isinstance(item, dict) else None
            if did == "owner":
                owners_changed += 1
            elif did:
                changed += 1
        conn.commit()
    return {"ok": True, "changed": changed, "owners": owners_changed, "asked": len(ask)}


def sync(conn, *, actor: str = "system", max_s: float = 420) -> dict:
    """The cadence_sync job: ask ping which contract Cadence speaks, lift a
    hold Cadence no longer warrants, unstick and expire, resend what a dead
    process left, send Cadence the Do not contacts set here it has not had
    yet, then read every status change since the last run (14.12), from 15
    minutes before the last run's serverTime (B062), following nextCursor to
    the end."""
    out = {"held": None, "sent": 0, "pages": 0, "read": 0, "changed": 0, "opted_out": 0, "told": 0}
    if problem():
        out["skipped"] = problem()
        return out
    ok, info, _ = ping(conn)
    if held(conn):
        if ok and info.get("sandboxEnabled", True):
            lift_hold(conn, actor)
            conn.commit()
        else:
            out["held"] = held(conn)
    out.update(unstick(conn, actor))
    if not held(conn):
        out["sent"] = drain(conn, max_s=max_s, actor=actor)
    if held(conn):
        out["held"] = held(conn)
        return out
    out["told"] = tell_pending(conn, actor)
    since = _state_get(conn, "status_since")
    if not since:
        r = conn.execute("SELECT MIN(first_pushed_at) t FROM cadence_push").fetchone()
        first = _parse(r["t"] if r else None)
        conn.commit()
        if not first:
            _state_set(conn, "last_sync_ok", _iso())
            conn.commit()
            return out
        since = _z(first)
    else:
        since = _z((_parse(since) or _now()) - timedelta(seconds=STATUS_OVERLAP_S))
    cursor, server_time = None, None
    for _ in range(1000):
        ans = _call(conn, "POST", "/contacts/status",
                    body_bytes({"changedSince": since, "cursor": cursor, "limit": CONTRACT_MAX}),
                    kind="status")
        if not ans.ok:
            if ans.status == 401:
                _hold(conn, "unauthorized", actor)
            elif ans.status == 503 and ans.code == "integration_disabled":
                _hold(conn, "integration_disabled", actor)
            _state_set(conn, "last_sync_error", f"{_iso()} {ans.error()}")
            conn.commit()
            out["error"] = ans.error()
            return out
        out["pages"] += 1
        server_time = server_time or ans.data.get("serverTime")
        for item in ans.data.get("results") or []:
            if not isinstance(item, dict):
                continue
            out["read"] += 1
            if item.get("status") == "opted_out":
                out["opted_out"] += 1
            if apply_status(conn, item, actor):
                out["changed"] += 1
        conn.commit()
        cursor = ans.data.get("nextCursor")
        if not cursor:
            break
    if server_time:
        _state_set(conn, "status_since", server_time)
    _state_set(conn, "last_sync_ok", _iso())
    _state_set(conn, "last_sync_error", None)
    conn.commit()
    return out


# ------------------------------------------------------------------ Do not contact, to Cadence (B003)

def _told_people(conn, s: dict) -> list[dict]:
    """The pushes a Do not contact set here covers that Cadence has heard of:
    the person, or everyone sent from the firm."""
    if s["scope"] == "person":
        pair = parse_id(s["value"])
        rows = _rows(conn, "SELECT * FROM cadence_push WHERE crd=? AND person_key=?", pair) if pair else []
    elif s["scope"] == "firm":
        rows = _rows(conn, "SELECT * FROM cadence_push WHERE crd=?", (s["value"],))
    else:
        rows = []
    return [r for r in rows if r["state"] not in ("failed", "opted_out")]


def _keys_of(conn, row: dict) -> dict:
    """The person's keys, so Cadence also finds them as a CRM person or under
    another record (contract v1.1 suppress takes ids or keys)."""
    pts = _rows(conn, "SELECT kind, value FROM contact_point WHERE crd=? AND person_key=?"
                      " AND kind IN ('email', 'phone')", (row["crd"], row["person_key"]))
    emails = list(dict.fromkeys([e for e in [row.get("email")] + [p["value"] for p in pts
                                                                  if p["kind"] == "email"]
                                 if norm_email(e)]))
    phones = list(dict.fromkeys([p for p in [row.get("phone")] + [norm_phone(x["value"])[0] for x in pts
                                                                  if x["kind"] == "phone"] if p]))
    return {"bellwetherContactId": contact_id(row["crd"], row["person_key"]),
            "emails": [norm_email(e) for e in emails][:10], "phones": phones[:10],
            "individualCrd": norm_crd(row.get("indvl_pk"))}


def tell_cadence(conn, sid: int, actor: str) -> dict:
    """Send a Do not contact set here to Cadence (v1.1 POST /contacts/suppress,
    B003), for everyone it covers whom Cadence has heard of. Cadence opts out
    the cold contacts, moved contacts and CRM people that match. Returns
    {"state": "sent" | "pending" | "not_sent" | None, "words": ...}: None when
    nobody it covers was ever sent, not_sent when Cadence does not speak 1.1,
    pending when it did not answer (the sync job tries again)."""
    s = _rows(conn, "SELECT * FROM contact_suppression WHERE id=?", (sid,))
    if not s or s[0]["source"] != "manual" or s[0]["lifted_at"]:
        return {"state": None, "words": ""}
    s = s[0]
    people = _told_people(conn, s)
    if not people:
        conn.execute("UPDATE contact_suppression SET to_cadence=NULL WHERE id=?", (sid,))
        conn.commit()
        return {"state": None, "words": ""}
    if not configured() or not speaks_11(conn):
        state, words = "not_sent", ("Cadence has not been told: it does not take a Do not contact from "
                                    "Bellwether yet. Opt them out in Cadence too.")
    else:
        from . import users
        acct = users.get(actor) or {}
        body = {"contacts": [_keys_of(conn, r) for r in people[:CONTRACT_MAX]],
                "reason": s["reason"], "note": s["note"],
                "actor": {"email": account_email(acct) or actor, "name": acct.get("name") or None}}
        ans = _call(conn, "POST", "/contacts/suppress", body_bytes(body), kind="push")
        if ans.ok:
            got = {str(r.get("bellwetherContactId")): r for r in ans.data.get("results") or []
                   if isinstance(r, dict)}
            n = sum(1 for r in got.values() if (r.get("result") or r.get("status")) != "not_found")
            state, words = "sent", (f"Cadence opted out {n:,} of {len(people):,}." if n else
                                    "Cadence has nobody it covers any more.")
            for r in people:
                _event(conn, actor, r["crd"], r["person_key"], "told",
                       str((got.get(contact_id(r["crd"], r["person_key"])) or {}).get("result") or "sent"))
        elif ans.kind == "http" and ans.status == 404:
            _state_set(conn, "contracts", "1")
            state, words = "not_sent", ("Cadence has not been told: it does not take a Do not contact from "
                                        "Bellwether yet. Opt them out in Cadence too.")
        elif ans.kind == "http" and ans.status in (400, 401, 413, 415, 422):
            state, words = "pending", f"Cadence refused it ({ans.error()}); an admin checks Settings, Cadence."
        else:
            state, words = "pending", "Cadence did not answer; Bellwether tries again within 10 minutes."
    conn.execute("UPDATE contact_suppression SET to_cadence=?, to_cadence_at=?, to_cadence_note=? WHERE id=?",
                 (state, _iso(), words[:300], sid))
    conn.commit()
    return {"state": state, "words": words}


def tell_pending(conn, actor: str = "system") -> int:
    """The sync job's part of B003: every Do not contact Cadence has not had
    yet, now that it may speak 1.1 or answer again."""
    rows = _rows(conn, "SELECT id, set_by FROM contact_suppression WHERE lifted_at IS NULL AND"
                       " source='manual' AND to_cadence IN ('pending', 'not_sent') ORDER BY id LIMIT 50")
    conn.commit()
    if not rows or not speaks_11(conn):
        return 0
    return sum(1 for r in rows if tell_cadence(conn, r["id"], r["set_by"] or actor)["state"] == "sent")


# ------------------------------------------------------------------ what the screens show

# The words for each state, in the plan's cue table (7.1). The tone picks the
# chip's colour: stop (red, Do not contact), warn (amber), info (blue, in Cold
# outreach), sky (already in the CRM), ok (green, moved), muted (grey).
LABEL = {"sending": "Sending to Cold outreach", "retry_wait": "Waiting", "in_crm": "Already in CRM",
         "moved_to_crm": "Moved to CRM", "opted_out": "Do not contact · Cadence",
         "removed": "Removed from Cold outreach", "rejected": "Not sent", "failed": "Send failed"}
# In words, for the export and Settings, Cadence (B021).
STATE_WORDS = {"sending": "Sending", "retry_wait": "Waiting", "in_sandbox": "In Cold outreach",
               "in_crm": "Already in CRM", "moved_to_crm": "Moved to CRM",
               "opted_out": "Do not contact (Cadence)", "rejected": "Not sent: refused",
               "removed": "Removed from Cold outreach", "failed": "Send failed"}


def label(row: dict | None, supp: list[dict] | None = None, kind: dict | None = None) -> tuple[str, str] | None:
    """(words, tone) for where a person stands, or None when never sent.
    A state this build does not know reads "Other" (the plan, 7.2)."""
    if row and row.get("erased_at"):
        return "Erased in Cadence", "muted"
    if supp:
        return (kind or supp_kind(supp))["words"], "stop"
    if not row:
        return None
    st = row["state"]
    owner = row.get("cadence_owner_name") or row.get("cadence_owner")
    if st == "in_sandbox":
        return (f"In Cold outreach · {owner}", "info") if owner else ("Unclaimed in Cold outreach", "warn")
    if st == "in_crm":
        return f"Already in CRM{' · ' + owner if owner else ''}", "sky"
    if st == "moved_to_crm":
        when = _day(row.get("moved_at"))
        return f"Moved to CRM{' · ' + when if when else ''}", "ok"
    if st == "rejected":
        return f"Not sent: {row.get('reason') or 'Cadence refused it'}", "warn"
    if st == "failed":
        return "Send failed", "warn"
    if st == "retry_wait":
        why = row.get("reason") or ""
        if why.startswith("Waiting:"):
            return "Waiting · " + why.removeprefix("Waiting:").strip().split(".")[0], "muted"
        when = _clock_time(row.get("next_retry_at"))
        return (f"Waiting · sends at {when}" if when else "Waiting"), "muted"
    if st == "removed":
        if row.get("removed_by") or row.get("removed_reason"):
            by = f"by {row['removed_by']}" if row.get("removed_by") else ""
            why = row.get("removed_reason") or ""
            return "Removed · " + (f"{by}: {why}" if by and why else by or why), "muted"
        return LABEL["removed"], "muted"
    if st in LABEL:
        return LABEL[st], "stop" if st == "opted_out" else "muted"
    return "Other", "muted"


def _supp_tip(kind: dict, firm_name=None) -> str:
    """Who set a Do not contact, when, and what it does (N041, B074)."""
    r = kind["row"]
    when = _day(r.get("set_at"))
    if kind["source"] == "cadence":
        at = f" while at {firm_name(kind['other'])}" if kind.get("other") and firm_name else ""
        what = "Erased in Cadence" if r.get("reason") == "erasure" else "Opted out in Cadence"
        return f"{what}{at} on {when}. Only Cadence can lift it."
    who = _name_of(r.get("set_by")) if r.get("set_by") else "someone"
    scope = " for everyone at the firm" if r["scope"] == "firm" else ""
    told = {"sent": " Cadence blocks this person too.",
            "pending": " Cadence has not been told yet; Bellwether keeps trying.",
            "not_sent": " Cadence has not been told: opt them out there too."}.get(r.get("to_cadence") or "", "")
    return f"Set in Bellwether by {who} on {when}{scope}.{told}"


def status_map(conn, pairs: list[tuple[str, str]],
               emails: dict[str, str | None] | None = None) -> dict[str, dict]:
    """For a page of people: each one's push row, active suppressions (by
    person, SEC individual, firm, and by the email shown when `emails` gives
    it) and the words to show. A few reads of small tables, never a call to
    Cadence."""
    pairs = [p for p in dict.fromkeys(pairs) if p and p[0] and p[1]]
    if not pairs or not ready(conn):
        return {}
    rows = {}
    for chunk in _chunks(pairs, 100):
        where, args = _pairs_sql(chunk)
        for r in _rows(conn, f"SELECT * FROM cadence_push WHERE {where}", args):
            rows[contact_id(r["crd"], r["person_key"])] = r
    waits = sorted({r["request_key"] for r in rows.values() if r["state"] == "retry_wait" and r["request_key"]})
    if waits:
        due = {q["request_key"]: q["next_retry_at"] for q in _rows(
            conn, f"SELECT request_key, next_retry_at FROM cadence_request WHERE request_key IN"
                  f" ({','.join('?' * len(waits))})", waits)}
        for r in rows.values():
            r["next_retry_at"] = due.get(r["request_key"])
    ids = [contact_id(*p) for p in pairs]
    mail = {cid: norm_email(e) for cid, e in (emails or {}).items() if norm_email(e)}
    pk = {contact_id(*p): norm_crd((rows.get(contact_id(*p)) or {}).get("indvl_pk")
                                   or (p[1][2:] if p[1].startswith("i:") else None)) for p in pairs}
    want = ([("person", i) for i in ids] + [("firm", c) for c in sorted({p[0] for p in pairs})]
            + [("individual", v) for v in sorted(set(pk.values()) - {None})]
            + [("email", e) for e in sorted(set(mail.values()))]
            + [("domain", e.split("@")[1]) for e in sorted(set(mail.values()))])
    found: dict[tuple, list] = {}
    for chunk in _chunks(list(dict.fromkeys(want)), 200):
        where, args = _pairs_sql(chunk, "scope", "value")
        for r in _rows(conn, f"SELECT * FROM contact_suppression WHERE lifted_at IS NULL AND {where}",
                       args):
            found.setdefault((r["scope"], r["value"]), []).append(r)
    names: dict[str, str] = {}

    def firm_name(crd):
        if crd not in names:
            names[crd] = _firm_label(conn, crd)
        return names[crd]
    out = {}
    for crd, key in pairs:
        cid = contact_id(crd, key)
        row = rows.get(cid)
        e = mail.get(cid)
        s = (found.get(("person", cid), []) + found.get(("individual", pk[cid]), [])
             + found.get(("firm", crd), [])
             + (found.get(("email", e), []) + found.get(("domain", e.split("@")[1]), []) if e else []))
        s = list({r["id"]: r for r in s}.values())
        kind = supp_kind(s, crd, firm_name) if s else None
        lab = label(row, s, kind)
        info = {"row": row, "supp": s, "kind": kind, "text": lab[0] if lab else "",
                "tone": lab[1] if lab else "",
                "can_push": not s and (row is None or row["state"] in REPUSH) and not (row or {}).get("erased_at"),
                "pushed": row is not None, "opted_out": any(x["source"] == "cadence" for x in s),
                "stop": bool(s) or bool((row or {}).get("erased_at")),
                "tip": _supp_tip(kind, firm_name) if kind else "",
                "outcome": None, "again": again_words(row), "live": None}
        if row and row.get("last_outcome"):
            info["outcome"] = f"{outcome_words(row['last_outcome'])} · {_day(row.get('last_outcome_at'))}"
        if row and row["state"] == "failed":
            info["tip"] = row.get("reason") or "No answer from Cadence"
        # Interim until Cadence takes a Do not contact from Bellwether (B003):
        # say plainly that the person is still being worked there.
        if kind and kind["source"] == "manual" and row and row["state"] in ("in_sandbox", "in_crm",
                                                                            "moved_to_crm") \
                and kind["row"].get("to_cadence") != "sent":
            owner = row.get("cadence_owner_name") or row.get("cadence_owner")
            where_ = "in Cold outreach" if row["state"] == "in_sandbox" else "in the CRM"
            info["live"] = (f"Still live {where_}{' with ' + owner if owner else ''}: Cadence has not "
                            "been told yet")
        out[cid] = info
    conn.commit()
    return out


def batch_view(conn, batch_id: str) -> dict:
    """A batch's per-contact results so far, for the dialog and My sends."""
    b = _rows(conn, "SELECT * FROM cadence_batch WHERE id=?", (batch_id,))
    if not b:
        return {"ok": False, "message": "No such send."}
    b = b[0]
    results = []
    for p in _rows(conn, "SELECT * FROM cadence_push WHERE batch_id=? ORDER BY id", (batch_id,)):
        cid = contact_id(p["crd"], p["person_key"])
        ev = _rows(conn, "SELECT outcome, detail FROM cadence_push_event WHERE batch_id=? AND crd=? AND"
                   " person_key=? AND action='result' ORDER BY id DESC LIMIT 1",
                   (batch_id, p["crd"], p["person_key"]))
        detail = json.loads(ev[0]["detail"]) if ev and ev[0]["detail"] else {}
        line, tone = _result_line(p, ev[0]["outcome"] if ev else None, detail)
        lab = label(p)
        results.append({"id": cid, "name": p["name"], "firm": _firm_label(conn, p["crd"]),
                        "state": p["state"], "line": line, "tone": tone,
                        "warnings": [WARN_WORDS.get(w, w) for w in detail.get("warnings") or []],
                        "status": lab[0] if lab else "", "status_tone": lab[1] if lab else "",
                        "cadence_url": p.get("cadence_url"), "crm_url": p.get("crm_url"),
                        "retry": p["state"] in REPUSH})
    for e in _rows(conn, "SELECT crd, person_key, outcome, detail FROM cadence_push_event WHERE batch_id=?"
                   " AND action='skipped' ORDER BY id", (batch_id,)):
        cid = contact_id(e["crd"], e["person_key"]) if e["crd"] else ""
        detail = json.loads(e["detail"]) if e["detail"] else {}
        rec = _rows(conn, "SELECT name FROM people_index WHERE crd=? AND person_key=?",
                    (e["crd"], e["person_key"])) if e["crd"] else []
        results.append({"id": cid, "name": (rec[0]["name"] if rec else None) or cid,
                        "firm": _firm_label(conn, e["crd"]) if e["crd"] else "", "state": "skipped",
                        "line": f"Not sent: {detail.get('why') or (e['outcome'] or '').split(':')[-1]}",
                        "tone": "muted", "warnings": []})
    conn.commit()
    done = {"created": 0, "in_sandbox": 0, "in_crm": 0, "rejected": 0, "failed": 0, "waiting": 0,
            "skipped": 0}
    for r in results:
        if r["state"] == "in_sandbox":
            done["in_sandbox"] += 1
        elif r["state"] in done:
            done[r["state"]] += 1
        elif r["state"] in ACTIVE:
            done["waiting"] += 1
    still = pending(conn, batch_id)
    went = done["in_sandbox"] + done["in_crm"] + done["rejected"] + done["failed"] + done["waiting"]
    return {"ok": True, "batch_id": batch_id, "results": results, "pending": still,
            "finished": bool(b["finished_at"]) or not still, "counts": done,
            "created_by": b["created_by"], "created_at": b["created_at"], "summary": _summary(done),
            "title": _title(done, went), "tone": "ok" if done["in_sandbox"] and not (
                done["failed"] or done["rejected"]) else "warn" if went else "muted"}


def _title(n: dict, went: int) -> str:
    """The results dialog's title (B181): what happened, never "Pushed" when
    nothing went."""
    sent = n["in_sandbox"]
    if not went:
        return "Nothing sent"
    if n["waiting"] and not sent:
        return "Sending"
    if n["skipped"]:
        return f"Sent {sent:,}, skipped {n['skipped']:,}"
    return f"Sent {sent:,} to Cold outreach"


def _summary(n: dict) -> str:
    bits = []
    if n["in_sandbox"]:
        bits.append(f"{n['in_sandbox']:,} in Cold outreach")
    if n["in_crm"]:
        bits.append(f"{n['in_crm']:,} already in CRM")
    if n["rejected"]:
        bits.append(f"{n['rejected']:,} not sent: refused")
    if n["failed"]:
        bits.append(f"{n['failed']:,} failed")
    if n["waiting"]:
        bits.append(f"{n['waiting']:,} still going")
    if n["skipped"]:
        bits.append(f"{n['skipped']:,} skipped")
    return ", ".join(bits) or "Nothing sent"


def _result_line(p: dict, outcome: str | None, detail: dict) -> tuple[str, str]:
    st = p["state"]
    owner = p.get("cadence_owner_name") or p.get("cadence_owner")
    if st == "in_sandbox":
        if outcome == "duplicate_in_sandbox":
            return f"Already in Cold outreach ({'owner: ' + owner if owner else 'unclaimed'})", ""
        return (f"Added to Cold outreach (owner: {owner})" if owner else
                "Added to Cold outreach, unclaimed"), "ok"
    if st == "in_crm":
        on = MATCHED_WORDS.get(detail.get("matchedOn") or "", "")
        return (f"Already in CRM{' (owner: ' + owner + ')' if owner else ''}"
                f"{' · matched on ' + on if on else ''}"), ""
    if st == "rejected":
        return f"Not sent: {p.get('reason') or 'Cadence refused it'}", "bad"
    if st == "retry_wait":
        why = p.get("reason") or ""
        # A hold says what Cadence did (B085); a retry says when it goes.
        return (why if why.startswith("Waiting:") else
                f"Will retry: {why or 'Cadence did not answer'}"), "wait"
    if st == "sending":
        return "Sending", "wait"
    if st == "failed":
        return f"Send failed: {p.get('reason') or 'no answer'}", "bad"
    lab = label(p)
    return (lab[0], lab[1]) if lab else (st, "")


def _history_line(r: dict) -> str | None:
    who = "Bellwether" if r["actor"] == "system" else _name_of(r["actor"])
    out = r["outcome"] or ""
    words = {
        "push": f"Sent by {who}",
        "resend": f"Sent again by {who}",
        "retry": "Cadence did not answer; retried",
        "status": {"in_sandbox": "In Cold outreach again", "moved_to_crm": "Moved to CRM in Cadence",
                   "already_in_crm": "Already in CRM", "opted_out": "Opted out in Cadence",
                   "removed": "Removed from Cold outreach", "not_found": "Cadence has no record of it",
                   "refused": "Cadence refused it", "move_undone": "Move undone in Cadence",
                   "owner": "Owner changed in Cadence"}.get(out, f"Cadence status: {out}"),
        "suppress": f"Do not contact, set by {who}" if not out.startswith("cadence") else "Opted out in Cadence",
        "lift": "Opt-out lifted in Cadence" if out.startswith("cadence") else f"Do not contact lifted by {who}",
        "told": "Do not contact sent to Cadence",
    }.get(r["action"])
    if r["action"] == "result":
        kind, _, why = out.partition(":")
        words = {"created": "Added to Cold outreach", "duplicate_in_sandbox": "Already in Cold outreach",
                 "already_in_crm": "Already in CRM",
                 "rejected": f"Not sent: {REASON_WORDS.get(why, why)}",
                 "failed": "Send failed"}.get(kind, out)
    if not words:
        return None
    return f"{_day(r['at'])}: {words}"


def history(conn, crd: str) -> dict[str, list[str]]:
    """Each sent person's history at one firm, newest first, for the hover
    on their status line."""
    out: dict[str, list[str]] = {}
    for r in _rows(conn, "SELECT at, actor, person_key, action, outcome FROM cadence_push_event"
                   " WHERE crd=? AND action IN ('push', 'resend', 'retry', 'result', 'status', 'suppress',"
                   " 'lift', 'told') ORDER BY id DESC LIMIT 400", (crd,)):
        line = _history_line(r)
        lines = out.setdefault(r["person_key"], [])
        if line and len(lines) < 8 and (not lines or lines[-1] != line):
            lines.append(line)
    conn.commit()
    return out


def newer_details(row: dict | None, best_email: str | None, best_phone: str | None,
                  country: str | None = None) -> str | None:
    """What Bellwether now holds that it did not send (B081), in words, or None."""
    if not row or row["state"] != "in_sandbox":
        return None
    e = norm_email(best_email)
    p = norm_phone(best_phone, country)[0] if best_phone else None
    if e and e != row.get("email"):
        return (f"Bellwether found a newer email ({e}) after this person was sent. Cold outreach still "
                "has the old one: tell the owner, who can add it there.")
    if p and not row.get("phone"):
        return (f"Bellwether found a phone ({readable_phone(p)}) after this person was sent. Tell the "
                "owner, who can add it in Cold outreach.")
    return None


# ------------------------------------------------------------------ the name keys (B064)

# Tables that hold a person's key beside contact_point, keyed (crd, person_key).
KEYED = ("email_hunt", "ai_research", "contact_search_state", "email_attempt")


def rekey_names(conn) -> dict:
    """The one-off for B064, run once at startup (cadence_state names_rekeyed).
    People named on websites used to be keyed with every letter outside a to z
    dropped, so names in other scripts collided or keyed as nothing. Every
    contact row whose name now keys differently takes its new key, unless the
    person was ever sent: Cadence knows them by the old key, compared exactly,
    so a sent person keeps it. A row whose new key already holds the same
    value is a duplicate and goes. Returns counts."""
    if _state_get(conn, "names_rekeyed"):
        conn.commit()
        return {"done": False}
    from .contacts import name_key
    sent = {(r["crd"], r["person_key"]) for r in _rows(conn, "SELECT crd, person_key FROM cadence_push")}
    rows = [r for r in _rows(conn, "SELECT id, crd, kind, value, person_key, person_name FROM contact_point"
                                   " WHERE person_key LIKE 'n:%' AND person_name IS NOT NULL")
            if not str(r["person_name"]).isascii()]
    moved, dropped, kept, maps = 0, 0, 0, {}
    for r in rows:
        new = name_key(r["person_name"])
        if not new or new == r["person_key"]:
            continue
        if (r["crd"], r["person_key"]) in sent:
            kept += 1
            continue
        maps.setdefault((r["crd"], r["person_key"]), set()).add(new)
        twin = conn.execute("SELECT 1 FROM contact_point WHERE crd=? AND kind=? AND value=? AND person_key=?",
                            (r["crd"], r["kind"], r["value"], new)).fetchone()
        if twin:
            conn.execute("DELETE FROM contact_point WHERE id=?", (r["id"],))
            dropped += 1
        else:
            conn.execute("UPDATE contact_point SET person_key=? WHERE id=?", (new, r["id"]))
            moved += 1
    conn.commit()
    # The hunts and searches follow when the old key meant one person only.
    for table in KEYED:
        try:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchall()
        except Exception:
            conn.rollback()
            continue
        for (crd, old), news in maps.items():
            if len(news) != 1:
                continue
            new = next(iter(news))
            conn.execute(f"UPDATE {table} SET person_key=? WHERE crd=? AND person_key=? AND NOT EXISTS"
                         f" (SELECT 1 FROM {table} x WHERE x.crd=? AND x.person_key=?)",
                         (new, crd, old, crd, new))
        conn.commit()
    _state_set(conn, "names_rekeyed", json.dumps({"at": _iso(), "moved": moved, "dropped": dropped,
                                                  "kept_sent": kept}))
    conn.commit()
    return {"done": True, "moved": moved, "dropped": dropped, "kept_sent": kept}


def backfill(conn) -> dict:
    """The release's clean-up of what Bellwether already holds (the plan,
    9.7 step 12), run once at startup and safe to run again: every opt-out
    Cadence reported before now also covers the SEC individual and their
    other emails and phones (N007), each Cadence opt-out row knows the
    contact it came from (so a lift in Cadence finds it), and a contact no
    longer in the CRM loses the CRM link an Undo left behind (N037). It only
    adds rows or clears stale links, and records its counts in cadence_state."""
    if _state_get(conn, "backfill_cold_outreach"):
        conn.commit()
        return {"done": False}
    out = {"opt_outs": 0, "rows": 0, "linked": 0, "crm_cleared": 0}
    for r in _rows(conn, "SELECT * FROM cadence_push WHERE state='opted_out'"):
        cid = contact_id(r["crd"], r["person_key"])
        out["linked"] += conn.execute("UPDATE contact_suppression SET for_id=? WHERE source='cadence'"
                                      " AND for_id IS NULL AND ((scope='person' AND value=?)"
                                      " OR (scope='email' AND value=?))", (cid, cid, r.get("email") or "")).rowcount
        pts = _rows(conn, "SELECT kind, value FROM contact_point WHERE crd=? AND person_key=?"
                          " AND kind IN ('email', 'phone')", (r["crd"], r["person_key"]))
        out["rows"] += suppress_person(conn, cid, reason="opted_out", source="cadence", by="system",
                                       note="Opted out in Cadence",
                                       emails=[r.get("email")] + [p["value"] for p in pts if p["kind"] == "email"],
                                       phones=[r.get("phone")] + [norm_phone(p["value"])[0] for p in pts
                                                                  if p["kind"] == "phone"],
                                       indvl_pk=r.get("indvl_pk"))
        out["opt_outs"] += 1
    out["crm_cleared"] = conn.execute(
        "UPDATE cadence_push SET crm_id=NULL, crm_url=NULL, moved_at=NULL WHERE state NOT IN"
        " ('moved_to_crm', 'in_crm') AND (crm_id IS NOT NULL OR crm_url IS NOT NULL)").rowcount
    _state_set(conn, "backfill_cold_outreach", json.dumps(dict(out, at=_iso())))
    conn.commit()
    return dict(out, done=True)


# ------------------------------------------------------------------ health and diagnostics

def alerts(conn) -> dict[str, dict]:
    """What needs a person, for health.alerts(): a hold, failed pushes,
    pushes waiting over an hour, a status sync failing for two hours."""
    if not configured() or not ready(conn):
        return {}
    out = {}
    h = held(conn)
    if h:
        out["cadence"] = {"level": "bad",
                          "title": ("Cadence refused Bellwether's key" if h == "unauthorized" else
                                    "Cadence has the Bellwether integration switched off"),
                          "detail": _hold_words(h), "href": "/settings/cadence"}
        return out
    day = _iso(_now() - timedelta(hours=24))
    failed = _rows(conn, "SELECT last_error, COUNT(*) n FROM cadence_request WHERE state='failed'"
                   " AND finished_at >= ? GROUP BY last_error", (day,))
    if failed:
        n = sum(int(r["n"]) for r in failed)
        why = ", ".join(f"{r['last_error'] or 'unknown'} ({r['n']})" for r in failed[:4])
        out["cadence"] = {"level": "bad", "title": f"{n} send{'s' if n != 1 else ''} to Cold outreach failed",
                          "detail": f"In the last day: {why}. A contract error means the two apps "
                                    "disagree; Settings, Cadence can retry them.",
                          "href": "/settings/cadence"}
        return out
    late = _rows(conn, "SELECT COUNT(*) n FROM cadence_request WHERE state IN ('sending', 'retry_wait')"
                 " AND first_sent_at < ?", (_iso(_now() - timedelta(hours=WAIT_LATE_H)),))
    if late and late[0]["n"]:
        out["cadence"] = {"level": "warn", "title": "Sends to Cold outreach have waited over an hour",
                          "detail": f"{late[0]['n']} request(s) are still waiting for Cadence to answer.",
                          "href": "/settings/cadence"}
        return out
    if switched_on():
        first = _rows(conn, "SELECT MIN(first_pushed_at) t FROM cadence_push")
        ok = _parse(_state_get(conn, "last_sync_ok"))
        since = ok or _parse(first[0]["t"] if first else None)
        if since and _now() - since > timedelta(hours=SYNC_LATE_H):
            out["cadence"] = {"level": "warn", "title": "Cadence status sync has failed for two hours",
                              "detail": "Moves to the CRM and opt-outs in Cadence are not reaching "
                                        "Bellwether. " + (_state_get(conn, "last_sync_error") or ""),
                              "href": "/settings/cadence"}
    conn.commit()
    return out


def diagnostics(conn) -> dict:
    """Counts only: no contact values, never the key."""
    out = {"url_set": bool(api_url()), "key_set": bool(api_key()), "enabled": switched_on(),
           "ready": ready(conn)}
    if not out["ready"]:
        return out
    out["states"] = {r["state"]: int(r["n"]) for r in _rows(
        conn, "SELECT state, COUNT(*) n FROM cadence_push GROUP BY state")}
    out["requests"] = {r["state"]: int(r["n"]) for r in _rows(
        conn, "SELECT state, COUNT(*) n FROM cadence_request GROUP BY state")}
    out["outcomes_24h"] = {r["outcome"] or "": int(r["n"]) for r in _rows(
        conn, "SELECT outcome, COUNT(*) n FROM cadence_push_event WHERE action='result' AND at >= ?"
        " GROUP BY outcome", (_iso(_now() - timedelta(hours=24)),))}
    out["suppressions"] = {r["source"]: int(r["n"]) for r in _rows(
        conn, "SELECT source, COUNT(*) n FROM contact_suppression WHERE lifted_at IS NULL GROUP BY source")}
    out["hold"] = held(conn)
    out["last_sync_ok"] = _state_get(conn, "last_sync_ok")
    err = _state_get(conn, "last_sync_error")
    out["last_error_class"] = err.split(" ", 1)[1] if err and " " in err else err
    conn.commit()
    return out
