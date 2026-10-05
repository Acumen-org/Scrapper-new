"""Add, list, remove, re-password and re-seat Bellwether accounts.

Accounts live in the database (app_user). Most people never need this: they
sign in with Microsoft and their account is created then, and an admin changes
seats from Settings, Users. This is for the first admin on a new server, for a
password account that has no Microsoft sign-in, and for getting back in if
Microsoft sign-in is ever misconfigured.

Passwords are typed at a prompt and never appear in a command line or shell
history. Only the PBKDF2 hash is stored.

    python -m scripts.manage_users list
    python -m scripts.manage_users add alisa --name "Alisa Chen" [--role admin|owner|user]
    python -m scripts.manage_users passwd alisa
    python -m scripts.manage_users role alisa owner --products PHH,Glynac
    python -m scripts.manage_users remove alisa
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import auth, db, settings, users  # noqa: E402

MIN_LEN = 12


def ask_password(username: str) -> str | None:
    first = getpass.getpass(f"New password for {username}: ")
    if len(first) < MIN_LEN:
        print(f"Too short: use at least {MIN_LEN} characters.", file=sys.stderr)
        return None
    if first != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return None
    return first


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--name", default="")
    a.add_argument("--role", default="user", choices=users.ROLES)
    a.add_argument("--products", default="", help="for owners: PHH,AcuBooth,Glynac")
    a.add_argument("--no-password", action="store_true",
                   help="for someone who will sign in with Microsoft")
    p = sub.add_parser("passwd")
    p.add_argument("username")
    r = sub.add_parser("remove")
    r.add_argument("username")
    ro = sub.add_parser("role")
    ro.add_argument("username")
    ro.add_argument("role", choices=users.ROLES)
    ro.add_argument("--products", default="")
    args = ap.parse_args()

    c = db.connect()
    settings.init(c)
    users.init(c)
    c.close()
    have = {u["login"]: u for u in users.all_users()}

    if args.cmd == "list":
        if not have:
            print("No accounts yet. Create the first admin with:\n"
                  "  python -m scripts.manage_users add <username> --name \"Full Name\" --role admin")
            return 0
        for login, u in sorted(have.items()):
            fam = f" ({', '.join(u['families'])})" if u["families"] else ""
            print(f"  {login:<36} {users.ROLE_LABEL[u['role']]:<14}{fam:<18} {u.get('name') or ''}"
                  f"{'' if u.get('active') else '  [off]'}")
        return 0

    uname = args.username.strip().lower()

    if args.cmd == "remove":
        if uname not in have:
            print(f"No such account: {uname}", file=sys.stderr)
            return 1
        admins = [u for u in have.values() if u["role"] == "admin" and u["login"] != uname]
        if have[uname]["role"] == "admin" and not admins:
            print("Refusing to remove the last admin; nobody could reach Settings.",
                  file=sys.stderr)
            return 1
        users.delete(uname)
        print(f"removed {uname}")
        return 0

    if args.cmd == "role":
        if uname not in have:
            print(f"No such account: {uname}", file=sys.stderr)
            return 1
        fams = [f.strip() for f in args.products.split(",") if f.strip()]
        users.save(uname, role=args.role, products=fams)
        print(f"{uname} is now {users.ROLE_LABEL[args.role]}"
              + (f" for {', '.join(fams)}" if fams else ""))
        return 0

    if args.cmd == "add" and uname in have:
        print(f"{uname} already exists; use passwd or role to change it.", file=sys.stderr)
        return 1
    if args.cmd == "passwd" and uname not in have:
        print(f"No such account: {uname}", file=sys.stderr)
        return 1

    pw_hash = None
    if not (args.cmd == "add" and args.no_password):
        pw = ask_password(uname)
        if pw is None:
            return 1
        pw_hash = auth.hash_password(pw)
    if args.cmd == "add":
        fams = [f.strip() for f in args.products.split(",") if f.strip()]
        users.create(uname, args.name or uname.split("@")[0].title(), role=args.role,
                     password_hash=pw_hash, email=uname if "@" in uname else None,
                     products=fams)
        print(f"created {uname} as {users.ROLE_LABEL[args.role]}")
    else:
        users.save(uname, password_hash=pw_hash)
        print(f"updated the password for {uname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
