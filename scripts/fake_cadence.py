"""An offline Cadence, for testing Bellwether's push without the real one.

A small HTTP server on localhost built from the shared contract (sections
14.3 to 14.14 of docs/outreach-sandbox-plan.md): the key header, a UUID v4
Idempotency-Key, the envelope rules, the 500-contact and 2 MB limits, the
content type, the dedupe rules in their order (14.6, including the wider CRM
phone rule), ownership (14.7), the push and check answers, replay and
conflict on a key, and the status endpoint by id and by changedSince with
nextCursor. It keeps nothing on disk.

The tests drive it directly: seed CRM people, sandbox contacts, suppressions,
users and statuses, force a result for one contact, or script the next
answers (any error code of 14.9, a slow answer that times out, a dropped
connection). It counts requests by kind on whatever clock it is given, so the
pace test can run on a fake one.

Run on its own, for a local Bellwether pointed at it:

    CADENCE_API_KEY=<any test value> python -m scripts.fake_cadence --port 8799
    (then the app with CADENCE_API_URL=http://127.0.0.1:8799 and the same key)

It never sends anything anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "/api/integrations/bellwether/v1"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
OWNING = {"admin", "junior_fo", "senior_fo", "sales_leader", "pod_manager"}
SHARED_TYPES = {"main", "toll_free"}
PHONE_TYPES = {"direct", "mobile", "office", "main", "toll_free", "unknown"}
PRODUCTS = {"PHH", "TOLLBOOTH", "ACUBOOTH", "GLYNAC"}


def _z(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _email(v) -> str | None:
    e = str(v or "").strip().lower()
    return e if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) and len(e) <= 254 else None


def _phone(v) -> str | None:
    d = re.sub(r"[^\d+]", "", str(v or ""))
    if not d:
        return None
    if not d.startswith("+"):
        d = "+1" + d[-10:] if len(re.sub(r"\D", "", d)) >= 10 else d
    return d if 8 <= len(d) <= 16 else None


def _linkedin(v) -> str | None:
    s = re.sub(r"^[a-z]+://", "", str(v or "").strip().lower()).split("?")[0].split("#")[0]
    s = re.sub(r"^(?:www\.|[a-z]{2,3}\.)", "", s).rstrip("/")
    m = re.match(r"^linkedin\.com/in/([^/\s]+)", s)
    return f"linkedin.com/in/{m.group(1)}" if m else None


def _domain(v) -> str | None:
    s = re.sub(r"^[a-z]+://", "", str(v or "").strip().lower()).split("/")[0]
    s = re.sub(r"^www\.", "", s.rsplit("@", 1)[-1])
    return s or None


class FakeCadence:
    def __init__(self, key: str | None, clock=time.time, port: int = 0):
        self.key = key
        self.clock = clock
        self.lock = threading.Lock()
        self.idem: dict[str, tuple[str, dict]] = {}
        self.in_progress: set[str] = set()
        self.sandbox: dict[str, dict] = {}        # bellwetherContactId -> contact
        self.crm: list[dict] = []                 # {id, emails, phones, linkedin, owner, name, company_phone}
        self.suppressed: set[str] = set()         # normalised emails, phones, LinkedIn URLs, ids
        self.internal_domains = {"acumen-strategy.com"}
        self.blocked_domains: set[str] = set()
        self.users: dict[str, tuple[str, str]] = {}   # email -> (name, role)
        self.statuses: dict[str, dict] = {}       # id -> status item
        self.forced: dict[str, dict] = {}         # id -> result fields a push answers with
        self.script: list[dict] = []              # answers for the next requests, by kind
        self.log: list[dict] = []                 # every request: t, kind, key header, idem key, body
        self.page_size = 500
        self.sandbox_enabled = True
        self.max_batch = 500
        self.slow_s = 1.5
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True

    # ------------------------------------------------------------ lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> "FakeCadence":
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-cadence").start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------ seeding
    def add_user(self, email: str, name: str, role: str = "senior_fo") -> None:
        self.users[email.lower()] = (name, role)

    def add_crm(self, *, emails=(), phones=(), linkedin=None, owner=None, name="CRM Person",
                company_phone=None) -> str:
        pid = str(uuid.uuid4())
        self.crm.append({"id": pid, "emails": {_email(e) for e in emails if _email(e)},
                         "phones": {_phone(p) for p in phones if _phone(p)},
                         "linkedin": _linkedin(linkedin), "owner": owner, "name": name,
                         "company_phone": _phone(company_phone)})
        return pid

    def suppress(self, value: str) -> None:
        self.suppressed.add(_email(value) or _phone(value) or _linkedin(value) or value)

    def set_status(self, cid: str, status: str, **fields) -> None:
        item = self.statuses.get(cid) or self._status_item(cid, status)
        item.update(status=status, statusChangedAt=_z(self.clock()), **fields)
        self.statuses[cid] = item

    def then(self, kind: str = "any", status: int | None = None, code: str | None = None,
             retry_after: float | None = None, action: str | None = None, times: int = 1) -> None:
        """Script the next answer(s) to a request of this kind: push, check,
        status, ping or any. action is 'timeout' (answer too late), 'drop'
        (close the connection without an answer) or 'lost' (do the push, then
        close the connection before answering)."""
        for _ in range(times):
            self.script.append({"kind": kind, "status": status, "code": code,
                                "retry_after": retry_after, "action": action})

    def requests_of(self, kind: str) -> list[dict]:
        return [r for r in self.log if r["kind"] == kind]

    def max_per_minute(self, kinds: tuple[str, ...]) -> int:
        times = sorted(r["t"] for r in self.log if r["kind"] in kinds)
        best, j = 0, 0
        for i, t in enumerate(times):
            while times[j] <= t - 60:
                j += 1
            best = max(best, i - j + 1)
        return best

    # ------------------------------------------------------------ the server
    def _send(self, h, status: int, data: dict | None, headers: dict | None = None) -> None:
        raw = json.dumps(data or {}).encode("utf-8")
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json; charset=utf-8")
            h.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(raw)
        except OSError:
            pass    # the client gave up first (a scripted timeout): nothing to answer

    def _drop(self, h) -> None:
        h.close_connection = True
        try:
            h.connection.shutdown(2)
        except OSError:
            pass

    def _error(self, h, status: int, code: str, message: str = "", fields=None, headers=None):
        err = {"code": code, "message": message or code}
        if fields is not None:
            err["fields"] = fields
        self._send(h, status, {"error": err}, headers)

    def _handle(self, h, method: str) -> None:
        path = h.path.split("?")[0]
        kind = {f"{API}/ping": "ping", f"{API}/contacts": "push", f"{API}/contacts/check": "check",
                f"{API}/contacts/status": "status"}.get(path)
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        with self.lock:
            self.log.append({"t": self.clock(), "kind": kind, "method": method,
                             "key": h.headers.get("X-Cadence-Api-Key"),
                             "idem": h.headers.get("Idempotency-Key"),
                             "ctype": h.headers.get("Content-Type"), "body": raw})
            step = None
            for i, s in enumerate(self.script):
                if s["kind"] in ("any", kind):
                    step = self.script.pop(i)
                    break
        if kind is None or (kind == "ping") != (method == "GET"):
            return self._error(h, 404, "not_found")
        if step and step["action"] == "drop":
            return self._drop(h)
        if step and step["action"] == "timeout":
            time.sleep(self.slow_s)
            return self._error(h, 503, "unavailable", "too late")
        if not self.key:
            return self._error(h, 503, "integration_disabled")
        if h.headers.get("X-Cadence-Api-Key") != self.key:
            return self._error(h, 401, "unauthorized")
        if step and step["status"]:
            headers = {"Retry-After": str(step["retry_after"])} if step["retry_after"] is not None else None
            fields = [{"path": "contacts", "problem": "scripted"}] if step["status"] == 422 else None
            return self._error(h, step["status"], step["code"] or "scripted", fields=fields, headers=headers)
        if kind == "ping":
            return self._send(h, 200, {"ok": True, "version": "1", "sandboxEnabled": self.sandbox_enabled,
                                       "maxBatch": self.max_batch, "serverTime": _z(self.clock())})
        if not self.sandbox_enabled:
            return self._error(h, 503, "integration_disabled", "the sandbox is switched off")
        if not (h.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._error(h, 415, "unsupported_media_type")
        if len(raw) > 2 * 1024 * 1024:
            return self._error(h, 413, "payload_too_large")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._error(h, 400, "invalid_json")
        if kind == "status":
            return self._status(h, body)
        if kind == "push":
            key = h.headers.get("Idempotency-Key") or ""
            if not UUID4.match(key):
                return self._error(h, 422, "invalid_request", fields=[
                    {"path": "Idempotency-Key", "problem": "a UUID v4 is required"}])
            sha = hashlib.sha256(raw).hexdigest()
            with self.lock:
                if key in self.in_progress:
                    return self._error(h, 409, "idempotency_in_progress", headers={"Retry-After": "5"})
                have = self.idem.get(key)
                if have and have[0] != sha:
                    return self._error(h, 409, "idempotency_conflict")
                if have:
                    return self._send(h, 200, dict(have[1], replayed=True))
                self.in_progress.add(key)
            try:
                status, data = self._contacts(body, write=True)
                if status == 200:
                    with self.lock:
                        self.idem[key] = (sha, data)
            finally:
                with self.lock:
                    self.in_progress.discard(key)
            if step and step["action"] == "lost":
                # Done, but the answer never arrives: the retry must get the replay.
                return self._drop(h)
            return self._send(h, status, data) if status == 200 else self._error(h, status, *data)
        status, data = self._contacts(body, write=False)
        return self._send(h, status, data) if status == 200 else self._error(h, status, *data)

    # ------------------------------------------------------------ push and check
    def _contacts(self, body: dict, write: bool):
        fields = []
        pb = body.get("pushedBy") if isinstance(body, dict) else None
        if not isinstance(pb, dict) or not (3 <= len(str(pb.get("email") or "")) <= 254):
            fields.append({"path": "pushedBy.email", "problem": "required"})
        at = body.get("assignTo") if isinstance(body, dict) else None
        if at is not None and not (isinstance(at, dict) and at.get("email")):
            fields.append({"path": "assignTo.email", "problem": "required"})
        contacts = body.get("contacts") if isinstance(body, dict) else None
        if not isinstance(contacts, list) or not contacts:
            fields.append({"path": "contacts", "problem": "1 to 500 required"})
        if fields:
            return 422, ("invalid_request", "envelope", fields)
        if len(contacts) > 500:
            return 413, ("payload_too_large", "more than 500 contacts")
        owner_email = str((at or pb).get("email")).lower()
        name, role = self.users.get(owner_email, (None, None))
        owner = {"email": owner_email, "name": name} if role in OWNING else None
        results, seen_ids, seen_mail = [], set(), set()
        summary = {"created": 0, "duplicate_in_sandbox": 0, "already_in_crm": 0, "rejected": 0}
        for c in contacts:
            r = self._one(c if isinstance(c, dict) else {}, owner, seen_ids, seen_mail, write)
            summary[r["result"]] += 1
            results.append(r)
        return 200, {"pushId": str(uuid.uuid4()) if write else None, "replayed": False,
                     "receivedAt": _z(self.clock()), "owner": owner, "summary": summary,
                     "results": results}

    def _result(self, cid, result, **kw) -> dict:
        r = {"bellwetherContactId": cid, "result": result, "cadenceContactId": None, "reason": None,
             "detail": None, "matchedOn": None, "crmPersonId": None, "crmUrl": None,
             "ownerEmail": None, "ownerName": None, "filledFields": [], "warnings": []}
        r.update(kw)
        return r

    def _one(self, c: dict, owner, seen_ids: set, seen_mail: set, write: bool) -> dict:
        cid = str(c.get("bellwetherContactId") or "")
        warnings = []
        email = _email(c.get("email"))
        if c.get("email") and not email:
            warnings.append({"code": "email_dropped", "field": "email", "crmPersonId": None, "detail": "bad"})
        phones = []
        if c.get("phone"):
            p = _phone(c["phone"])
            if p:
                phones.append((p, c.get("phoneType") if c.get("phoneType") in PHONE_TYPES else "unknown"))
            else:
                warnings.append({"code": "phone_dropped", "field": "phone", "crmPersonId": None, "detail": "bad"})
        for x in c.get("additionalPhones") or []:
            p = _phone((x or {}).get("number"))
            if p:
                phones.append((p, x.get("type") if x.get("type") in PHONE_TYPES else "unknown"))
        li = _linkedin(c.get("linkedinUrl"))
        if c.get("linkedinUrl") and not li:
            warnings.append({"code": "linkedin_dropped", "field": "linkedinUrl", "crmPersonId": None, "detail": "bad"})
        for p in c.get("productInterest") or []:
            if p not in PRODUCTS:
                warnings.append({"code": "product_dropped", "field": "productInterest", "crmPersonId": None, "detail": p})
        firm = c.get("firm") if isinstance(c.get("firm"), dict) else {}
        firm_phone = _phone(firm.get("phone"))
        if cid in self.forced and write:
            # Forced answers are for pushes; the check answers by the rules.
            return self._result(cid, **self.forced[cid])
        for f, lo, hi in (("bellwetherContactId", 1, 200), ("firstName", 1, 100), ("lastName", 1, 100)):
            if not (lo <= len(str(c.get(f) or "").strip()) <= hi):
                return self._result(cid, "rejected", reason="invalid_field", detail=f"{f} is empty")
        if not (1 <= len(str(firm.get("name") or "").strip()) <= 200):
            return self._result(cid, "rejected", reason="invalid_field", detail="firm.name is empty")
        # 1. within the request
        if cid in seen_ids or (email and email in seen_mail):
            return self._result(cid, "rejected", reason="duplicate_in_request")
        seen_ids.add(cid)
        if email:
            seen_mail.add(email)
        # 2. suppression
        direct = {p for p, t in phones if t in ("direct", "mobile")}
        if ({email, li, cid} | direct) & self.suppressed:
            return self._result(cid, "rejected", reason="suppressed")
        # 3. internal or blocked
        fdom = _domain(firm.get("domain") or firm.get("website"))
        if (email and email.split("@")[1] in self.internal_domains) or fdom in self.internal_domains:
            return self._result(cid, "rejected", reason="internal_contact")
        if fdom and fdom in self.blocked_domains:
            return self._result(cid, "rejected", reason="blocked_account")
        # 4. the CRM, with the wider phone rule
        for person in self.crm:
            on = None
            if email and email in person["emails"]:
                on = "email"
            elif li and li == person["linkedin"]:
                on = "linkedin_url"
            else:
                for p, t in phones:
                    if t in SHARED_TYPES or p not in person["phones"]:
                        continue
                    if t in ("direct", "mobile") or p not in (firm_phone, person["company_phone"]):
                        on = "phone"
                        break
            if on:
                return self._result(cid, "already_in_crm", matchedOn=on, crmPersonId=person["id"],
                                    crmUrl=f"https://twenty.example.test/object/person/{person['id']}",
                                    ownerEmail=person["owner"], ownerName=person["owner"],
                                    warnings=warnings)
        # 5. the sandbox
        icrd = str(c.get("individualCrd") or "") or None
        for sid, s in self.sandbox.items():
            on = ("bellwether_contact_id" if sid == cid else
                  "individual_crd" if icrd and icrd == s.get("individualCrd") else
                  "email" if email and email == s.get("email") else
                  "linkedin_url" if li and li == s.get("linkedinUrl") else
                  "phone" if direct & s.get("direct", set()) else None)
            if on:
                filled = [k for k in ("email", "phone", "linkedinUrl", "jobTitle")
                          if not s.get(k) and c.get(k)]
                if write:
                    for k in filled:
                        s[k] = c[k]
                return self._result(cid, "duplicate_in_sandbox", cadenceContactId=s["cadenceContactId"],
                                    matchedOn=on, ownerEmail=s["ownerEmail"], ownerName=s["ownerName"],
                                    filledFields=filled, warnings=warnings)
        # 6. created
        if not write:
            return self._result(cid, "created", ownerEmail=(owner or {}).get("email"),
                                ownerName=(owner or {}).get("name"), warnings=warnings)
        new = {"cadenceContactId": str(uuid.uuid4()), "email": email, "linkedinUrl": li,
               "phone": c.get("phone"), "jobTitle": c.get("jobTitle"), "individualCrd": icrd,
               "direct": direct, "ownerEmail": (owner or {}).get("email"),
               "ownerName": (owner or {}).get("name"), "payload": c}
        self.sandbox[cid] = new
        self.statuses[cid] = self._status_item(cid, "in_sandbox")
        return self._result(cid, "created", cadenceContactId=new["cadenceContactId"],
                            ownerEmail=new["ownerEmail"], ownerName=new["ownerName"], warnings=warnings)

    # ------------------------------------------------------------ status
    def _status_item(self, cid: str, status: str) -> dict:
        s = self.sandbox.get(cid) or {}
        return {"bellwetherContactId": cid, "status": status,
                "cadenceContactId": s.get("cadenceContactId"), "ownerEmail": s.get("ownerEmail"),
                "ownerName": s.get("ownerName"), "campaignName": None, "callCount": 0,
                "lastActivityAt": None, "movedToCrmAt": None, "crmPersonId": None, "crmUrl": None,
                "statusChangedAt": _z(self.clock())}

    def _status(self, h, body: dict):
        if not isinstance(body, dict):
            return self._error(h, 422, "invalid_request", fields=[{"path": "", "problem": "object"}])
        ids = body.get("bellwetherContactIds")
        if ids is not None:
            if not isinstance(ids, list) or not ids:
                return self._error(h, 422, "invalid_request", fields=[{"path": "bellwetherContactIds",
                                                                       "problem": "1 to 500"}])
            if len(ids) > 500:
                return self._error(h, 413, "payload_too_large")
            res = [self.statuses.get(i) or dict(self._status_item(i, "not_found"), statusChangedAt=None)
                   for i in ids]
            return self._send(h, 200, {"serverTime": _z(self.clock()), "nextCursor": None, "results": res})
        since = body.get("changedSince")
        if not since:
            return self._error(h, 422, "invalid_request", fields=[{"path": "changedSince", "problem": "required"}])
        limit = min(int(body.get("limit") or 500), 500, self.page_size)
        start = int(body.get("cursor") or 0)
        items = sorted((s for s in self.statuses.values() if (s["statusChangedAt"] or "") >= since),
                       key=lambda s: (s["statusChangedAt"], s["bellwetherContactId"]))
        page = items[start:start + limit]
        nxt = str(start + limit) if start + limit < len(items) else None
        return self._send(h, 200, {"serverTime": _z(self.clock()), "nextCursor": nxt, "results": page})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--key-env", default="CADENCE_API_KEY",
                    help="the environment variable holding the key it accepts")
    args = ap.parse_args()
    key = os.environ.get(args.key_env, "")
    if not key:
        print(f"Set {args.key_env} first: the fake accepts the same key the app sends.")
        return 2
    fake = FakeCadence(key, port=args.port)
    fake.add_user("admin@acumen-strategy.com", "Admin Example", "admin")
    print(f"fake Cadence on {fake.url} (key from {args.key_env})", flush=True)
    try:
        fake.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
