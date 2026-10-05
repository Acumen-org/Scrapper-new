"""Small JSON endpoints the pages call without reloading.

The finder (Ctrl K), Bellwether AI, the brief on a firm page, checking one
email or every email at a firm, re-reading a firm's website on demand, and
adding a contact by hand. Each answers quickly: anything slow (a firm's whole
website, a dozen SMTP conversations) is handed to a background process and
the answer says when to look.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import ai, assistant, config, contacts, procs, products
from .webapp import conn, current_owner, current_user, esc, money, nice_name

router = APIRouter()
CRD_RE = re.compile(r"^[0-9]{1,12}$")


def _j(ok: bool = True, **kw) -> JSONResponse:
    return JSONResponse({"ok": ok, **kw}, status_code=200 if ok else kw.pop("status", 200))


@router.get("/api/search")
def api_search(q: str = ""):
    """Firms and people for the finder. Read-only, tiny payload."""
    q = q.strip()
    if len(q) < 2:
        return JSONResponse([])
    c = conn()
    out = []
    rows = c.execute("""
        SELECT f.crd, f.legal_name, f.city, f.state, f.raum, s.best_product, s.best_score
        FROM firm_current f LEFT JOIN firm_scope s ON s.crd=f.crd
        WHERE (f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd = ?)
        ORDER BY (s.crd IS NULL), s.priority DESC, f.raum DESC NULLS LAST LIMIT 7""",
                     (f"%{q}%", f"%{q}%", q)).fetchall()
    for r in rows:
        best = ""
        if r["best_product"]:
            best = f" . {products.product(r['best_product'])['name']} {r['best_score']:.0f}"
        place = " ".join(x for x in (nice_name(r["city"] or ""), r["state"] or "") if x)
        bits = [x for x in (place, money(r["raum"]) if r["raum"] else "", best.lstrip(" .")) if x]
        out.append({"name": nice_name(r["legal_name"]), "href": f"/firm/{r['crd']}",
                    "meta": " . ".join(bits) or f"CRD {r['crd']}"})
    if len(q) >= 3:
        try:
            for r in c.execute("""
                SELECT p.indvl_pk, p.name, e.org_pk, f.legal_name FROM person p
                JOIN person_employment e ON e.indvl_pk = p.indvl_pk AND e.kind = 'current'
                JOIN firm_current f ON f.crd = e.org_pk
                WHERE p.name ILIKE ? ORDER BY f.raum DESC NULLS LAST LIMIT 5""",
                               (f"%{q}%",)):
                out.append({"name": r["name"], "href": f"/firm/{r['org_pk']}#people",
                            "meta": f"Person . {nice_name(r['legal_name'])}"})
        except Exception:
            c.rollback()
    c.close()
    return JSONResponse(out)


@router.post("/api/ai/ask")
async def api_ask(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    q = str(body.get("q") or "")
    scope = str(body.get("scope") or "global")
    history = body.get("history") if isinstance(body.get("history"), list) else []
    if scope.startswith("firm:") and not CRD_RE.match(scope[5:]):
        scope = "global"

    def run():
        c = conn()
        try:
            return assistant.ask(c, q, scope, history, who=current_user() or "",
                                 me=current_owner())
        finally:
            c.close()

    from starlette.concurrency import run_in_threadpool
    try:
        res = await run_in_threadpool(run)
    except ai.AIError as e:
        return _j(False, html=f'<p class="bad">{esc(str(e))}</p>', error=str(e))
    except Exception as e:  # never show a stack trace in a chat bubble
        return _j(False, html='<p class="bad">Something went wrong answering that.</p>',
                  error=type(e).__name__)
    return _j(True, html=res["html"], text=res.get("text", ""))


@router.post("/api/firm/{crd}/brief")
def api_brief(crd: str):
    if not CRD_RE.match(crd):
        return _j(False, message="Not a firm")
    c = conn()
    try:
        b = assistant.brief(c, crd, force=True, who=current_user() or "")
    except ai.AIError as e:
        return _j(False, message=str(e))
    finally:
        c.close()
    if not b:
        return _j(False, message="Bellwether AI is not connected.")
    return _j(True, message="Brief written", replace=brief_html(b))


def brief_html(b: dict) -> str:
    when = (b.get("created_at") or "")[:10]
    return (f'<div class="brief">{ai.md_to_html(b["content"])}</div>'
            f'<p class="meta"><span class="chip ai">AI</span> Written {esc(when)} by '
            f'{esc(b.get("model") or "the AI provider")} from the facts on this page. '
            f'Check before you rely on it.</p>')


def _verify_status_html(status: str) -> str:
    return (f'<span class="chip v-{esc(status)}">'
            f'{esc(contacts.VERIFY_LABEL.get(status, status))}</span>')


@router.post("/api/contact/{cid}/verify")
def api_verify_one(cid: int):
    try:
        from . import verify
    except ImportError:
        return _j(False, message="Email verification is not installed.")
    c = conn()
    r = c.execute("SELECT id, value, kind FROM contact_point WHERE id=?", (cid,)).fetchone()
    c.commit()
    if not r or r["kind"] != "email":
        c.close()
        return _j(False, message="Not an email address")
    try:
        counts = verify.verify_contacts(c, [cid])
        row = c.execute("SELECT verify_status, verify_detail FROM contact_point WHERE id=?",
                        (cid,)).fetchone()
    finally:
        c.close()
    status = row["verify_status"] if row else "unknown"
    reason = ""
    try:
        reason = json.loads(row["verify_detail"] or "{}").get("reason", "")
    except (ValueError, TypeError):
        pass
    return _j(True, message=f"{r['value']}: {contacts.VERIFY_LABEL.get(status, status)}"
              + (f". {reason}" if reason else ""), replace=_verify_status_html(status),
              counts=counts)


def _bg(cmd: list[str], log: str) -> None:
    f = open(config.DATA_DIR / log, "ab")
    subprocess.Popen(cmd, cwd=str(config.ROOT), stdout=f, stderr=f,
                     creationflags=procs.SPAWN_FLAGS)


@router.post("/api/firm/{crd}/verify")
def api_verify_firm(crd: str):
    if not CRD_RE.match(crd):
        return _j(False, message="Not a firm")
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM contact_point WHERE crd=? AND kind='email'",
                  (crd,)).fetchone()["n"]
    c.close()
    if not n:
        return _j(False, message="No email addresses at this firm to check yet.")
    _bg([sys.executable, "-m", "scripts.verify_emails", "--crd", crd], "verify.log")
    return _j(True, message=f"Checking {n} address{'es' if n != 1 else ''} with their mail "
                            f"servers. Results appear here within a minute or two.")


@router.post("/api/firm/{crd}/crawl")
def api_crawl(crd: str, url: str = Form("")):
    if not CRD_RE.match(crd):
        return _j(False, message="Not a firm")
    cmd = [sys.executable, "-m", "scripts.web_enrich", "--crd", crd]
    url = url.strip()
    if url:
        if not re.match(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$", url):
            return _j(False, message="That does not look like a web address.")
        cmd += ["--url", url]
    _bg(cmd, "web_enrich.log")
    return _j(True, message="Reading the firm's website now. People and contacts appear "
                            "here in a few minutes.")


@router.post("/api/firm/{crd}/contact")
def api_add_contact(crd: str, name: str = Form(""), title: str = Form(""),
                    email: str = Form(""), phone: str = Form("")):
    """A contact someone knows first-hand. Stored as manual, attributed."""
    if not CRD_RE.match(crd):
        return _j(False, message="Not a firm")
    name, email, phone = name.strip()[:120], email.strip()[:200], phone.strip()[:40]
    if not (email or phone):
        return _j(False, message="Add an email or a phone number.")
    key = contacts.name_key(name) if name else ""
    c = conn()
    try:
        try:
            from . import people
            pk = people.match_person(c, crd, name) if name else None
            if pk:
                key = f"i:{pk}"
        except Exception:
            c.rollback()
        added = 0
        ref = f"added by {current_owner() or 'someone'}"
        if email:
            added += contacts.upsert(c, crd, "email", email, "manual", person_key=key,
                                     person_name=name or None, title=title or None,
                                     source_ref=ref)
        if phone:
            added += contacts.upsert(c, crd, "phone", phone, "manual", person_key=key,
                                     person_name=name or None, title=title or None,
                                     source_ref=ref)
        c.commit()
    finally:
        c.close()
    return _j(True, message="Saved" if added else "Already on file", reload=True)


_JOB_LOCK = threading.Lock()


@router.get("/api/jobs")
def api_jobs():
    """Live job states for the activity strip on Home."""
    from . import jobs
    c = conn()
    try:
        rows = jobs.overview(c)
    finally:
        c.close()
    return JSONResponse([{"kind": r["job"].kind, "label": r["job"].label, "state": r["state"],
                          "done": r["done"], "total": r["total"]} for r in rows])
