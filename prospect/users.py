"""Who can use Bellwether, and what each person may change.

Three seats:

  admin          everything, including Settings, Enrichment sources, users and
                 every product's scoring
  owner          a product owner: edits the scoring of the products they own
                 (PHH, AcuBooth or Glynac) and manages enrichment sources;
                 no Settings
  user           works the lists: statuses, notes, manual levels on a firm,
                 saved lists, Bellwether AI; changes no configuration

Accounts live in the database (app_user), not in a YAML file, because Microsoft
sign-in creates them on first visit and an admin edits roles from the screen.
The old config/users.yml (or BELLWETHER_USERS) is imported once at startup so
the password accounts that already exist keep working unchanged.

Anyone listed in the auth.admins setting is an admin whatever their stored
role. That is the bootstrap: rahul.gopan@acumen-strategy.com is an admin the
first time he signs in with Microsoft, with no row to edit by hand.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import settings

ROLES = ("admin", "owner", "user")
ROLE_LABEL = {"admin": "Admin", "owner": "Product owner", "user": "User"}
FAMILIES = ("PHH", "AcuBooth", "Glynac")

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE,     -- lowercase email, or a legacy username
    email         TEXT,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    products      TEXT NOT NULL DEFAULT '', -- families a product owner owns
    password_hash TEXT,
    ms_oid        TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
"""

_CACHE: dict = {"t": 0.0, "by_login": {}}
_LOCK = threading.Lock()
TTL_S = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    """Create the table and bring over any legacy YAML accounts."""
    conn.executescript(SCHEMA)
    conn.commit()
    from . import auth
    legacy = auth.load_users()
    if legacy:
        have = {r["login"] for r in conn.execute("SELECT login FROM app_user")}
        for login, rec in legacy.items():
            login = login.strip().lower()
            if login in have or not rec.get("password_hash"):
                continue
            conn.execute(
                "INSERT INTO app_user (login, email, name, role, password_hash,"
                " created_at) VALUES (?,?,?,?,?,?) ON CONFLICT (login) DO NOTHING",
                (login, rec.get("email"), rec.get("name") or login,
                 "admin" if _listed_admin(login, rec.get("email")) else "user",
                 rec["password_hash"], _now()))
        conn.commit()
        # Somebody has to be able to reach Settings to set up Microsoft
        # sign-in. Before seats existed every account could reach everything,
        # so if the import produced no admin, Rahul Gopan's account becomes
        # one; failing that, every imported account keeps the full access it
        # had, and an admin trims it back from Settings, Users.
        n_admin = conn.execute("SELECT COUNT(*) n FROM app_user WHERE role='admin'"
                               ).fetchone()["n"]
        if not n_admin:
            r = conn.execute("UPDATE app_user SET role='admin' WHERE password_hash IS NOT NULL"
                             " AND (name ILIKE 'rahul%gopan%' OR login IN ('rahul','rahul.gopan'))")
            if not r.rowcount:
                conn.execute("UPDATE app_user SET role='admin' WHERE password_hash IS NOT NULL")
            conn.commit()
    invalidate()


def invalidate() -> None:
    with _LOCK:
        _CACHE["t"] = 0.0


def _load() -> dict[str, dict]:
    with _LOCK:
        if time.monotonic() - _CACHE["t"] < TTL_S:
            return _CACHE["by_login"]
    from . import db
    out: dict[str, dict] = {}
    try:
        c = db.connect()
        try:
            for r in c.execute("SELECT * FROM app_user"):
                out[r["login"]] = dict(r)
        finally:
            c.close()
    except Exception:
        out = dict(_CACHE["by_login"])
    with _LOCK:
        _CACHE.update(t=time.monotonic(), by_login=out)
    return out


def _listed_admin(login: str, email: str | None = None) -> bool:
    admins = settings.get_list("auth.admins")
    return (login or "").lower() in admins or (email or "").lower() in admins


def effective(u: dict | None) -> dict | None:
    """The user record with the admin bootstrap applied."""
    if not u:
        return None
    u = dict(u)
    if _listed_admin(u["login"], u.get("email")):
        u["role"] = "admin"
    u["families"] = [f for f in (u.get("products") or "").split(",") if f]
    return u


_MISS: dict = {"t": 0.0}


def _lookup(login: str) -> dict | None:
    """A cached account, re-reading the table once on a miss so an account
    created a moment ago (by an admin, or by another process) works at once.
    Misses re-read at most every two seconds, so a stale cookie for a deleted
    account cannot turn into a query per request."""
    key = login.strip().lower()
    u = _load().get(key)
    if u is None and time.monotonic() - _MISS["t"] > 2.0:
        _MISS["t"] = time.monotonic()
        invalidate()
        u = _load().get(key)
    return u


def get(login: str | None) -> dict | None:
    if not login:
        return None
    return effective(_lookup(login))


def all_users() -> list[dict]:
    return sorted((effective(u) for u in _load().values()),
                  key=lambda u: (ROLES.index(u["role"]), u["name"].lower()))


def count() -> int:
    return len(_load())


def display_name(login: str | None) -> str:
    u = get(login)
    return (u or {}).get("name") or (login or "")


# ------------------------------------------------------------ permissions

def is_admin(u: dict | None) -> bool:
    return bool(u and u.get("role") == "admin")


def can_edit_product(u: dict | None, product_family: str) -> bool:
    if not u:
        return False
    if u["role"] == "admin":
        return True
    return u["role"] == "owner" and product_family in u.get("families", [])


def can_manage_enrichment(u: dict | None) -> bool:
    return bool(u and u.get("role") in ("admin", "owner"))


# ------------------------------------------------------------ sign-in

def check_password(login: str, password: str) -> dict | None:
    """The account on success. Hashes even for an unknown login, so a wrong
    username and a wrong password take the same time."""
    from . import auth
    u = _lookup(login or "")
    stored = (u or {}).get("password_hash") or auth.hash_password("x")
    ok = auth.verify_password(password or "", stored)
    if not (ok and u and u.get("active")):
        return None
    _touch(u["login"])
    return effective(u)


def from_microsoft(email: str, name: str, oid: str | None) -> dict:
    """Find or create the account for a Microsoft sign-in, by email."""
    from . import db
    login = email.strip().lower()
    c = db.connect()
    try:
        r = c.execute("SELECT * FROM app_user WHERE login=? OR LOWER(email)=?"
                      " ORDER BY (login=?) DESC LIMIT 1", (login, login, login)).fetchone()
        if r is None:
            c.execute("INSERT INTO app_user (login, email, name, role, ms_oid, created_at,"
                      " last_login_at) VALUES (?,?,?,?,?,?,?)",
                      (login, login, name or login,
                       "admin" if _listed_admin(login) else "user", oid, _now(), _now()))
        else:
            c.execute("UPDATE app_user SET email=?, name=?, ms_oid=COALESCE(?, ms_oid),"
                      " last_login_at=? WHERE id=?",
                      (login, name or r["name"], oid, _now(), r["id"]))
        c.commit()
        r = c.execute("SELECT * FROM app_user WHERE login=? OR LOWER(email)=?"
                      " ORDER BY (login=?) DESC LIMIT 1", (login, login, login)).fetchone()
    finally:
        c.close()
    invalidate()
    return effective(dict(r))


def _touch(login: str) -> None:
    from . import db
    try:
        c = db.connect()
        try:
            c.execute("UPDATE app_user SET last_login_at=? WHERE login=?", (_now(), login))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


# ------------------------------------------------------------ admin edits

def save(login: str, *, name: str | None = None, role: str | None = None,
         products: list[str] | None = None, active: bool | None = None,
         email: str | None = None, password_hash: str | None = None) -> None:
    from . import db
    sets, args = [], []
    if name is not None:
        sets.append("name=?")
        args.append(name.strip() or login)
    if role is not None:
        if role not in ROLES:
            raise ValueError("unknown role")
        sets.append("role=?")
        args.append(role)
    if products is not None:
        sets.append("products=?")
        args.append(",".join(f for f in products if f in FAMILIES))
    if active is not None:
        sets.append("active=?")
        args.append(1 if active else 0)
    if email is not None:
        sets.append("email=?")
        args.append(email.strip().lower() or None)
    if password_hash is not None:
        sets.append("password_hash=?")
        args.append(password_hash)
    if not sets:
        return
    c = db.connect()
    try:
        c.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE login=?",
                  (*args, login.strip().lower()))
        c.commit()
    finally:
        c.close()
    invalidate()


def create(login: str, name: str, role: str = "user", password_hash: str | None = None,
           email: str | None = None, products: list[str] | None = None) -> None:
    from . import db
    if role not in ROLES:
        raise ValueError("unknown role")
    c = db.connect()
    try:
        c.execute("INSERT INTO app_user (login, email, name, role, products, password_hash,"
                  " created_at) VALUES (?,?,?,?,?,?,?)",
                  (login.strip().lower(), (email or "").strip().lower() or None,
                   name.strip() or login, role,
                   ",".join(f for f in (products or []) if f in FAMILIES),
                   password_hash, _now()))
        c.commit()
    finally:
        c.close()
    invalidate()


def delete(login: str) -> None:
    from . import db
    c = db.connect()
    try:
        c.execute("DELETE FROM app_user WHERE login=?", (login.strip().lower(),))
        c.commit()
    finally:
        c.close()
    invalidate()
