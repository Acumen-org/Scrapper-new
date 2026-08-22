"""Add, list, remove and re-password Bellwether accounts.

Passwords are typed at a prompt and never appear in a command line, shell
history, or the users file. Only the PBKDF2 hash is stored.

    python -m scripts.manage_users list
    python -m scripts.manage_users add alisa --name "Alisa Chen"
    python -m scripts.manage_users passwd alisa
    python -m scripts.manage_users remove alisa
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import auth  # noqa: E402

MIN_LEN = 10


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
    p = sub.add_parser("passwd")
    p.add_argument("username")
    r = sub.add_parser("remove")
    r.add_argument("username")
    args = ap.parse_args()

    users = auth.load_users()

    if args.cmd == "list":
        if not users:
            print("No accounts yet. Create one with:\n"
                  "  python -m scripts.manage_users add <username> --name \"Full Name\"")
            return 0
        for u, rec in sorted(users.items()):
            print(f"  {u:<16} {rec.get('name') or ''}")
        return 0

    uname = args.username.strip().lower()

    if args.cmd == "remove":
        if uname not in users:
            print(f"No such account: {uname}", file=sys.stderr)
            return 1
        if len(users) == 1:
            print("Refusing to remove the only account; you would lock "
                  "yourself out.", file=sys.stderr)
            return 1
        users.pop(uname)
        auth.save_users(users)
        print(f"removed {uname}")
        return 0

    if args.cmd == "add" and uname in users:
        print(f"{uname} already exists; use passwd to change it.", file=sys.stderr)
        return 1
    if args.cmd == "passwd" and uname not in users:
        print(f"No such account: {uname}", file=sys.stderr)
        return 1

    pw = ask_password(uname)
    if pw is None:
        return 1
    rec = users.get(uname, {})
    rec["password_hash"] = auth.hash_password(pw)
    if args.cmd == "add":
        rec["name"] = args.name or uname.title()
    users[uname] = rec
    auth.save_users(users)
    print(f"{'created' if args.cmd == 'add' else 'updated'} {uname} "
          f"({rec.get('name')})")
    print(f"accounts file: {auth.USERS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
