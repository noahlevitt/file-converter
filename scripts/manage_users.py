"""Manage user accounts from the server shell.

  python scripts/manage_users.py add <username> [--admin]
  python scripts/manage_users.py password <username>
  python scripts/manage_users.py delete <username>
  python scripts/manage_users.py list

In Docker:  docker compose exec app python scripts/manage_users.py add jane
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter.config import get_settings  # noqa: E402
from converter.storage import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true")
    sub.add_parser("password").add_argument("username")
    sub.add_parser("delete").add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()

    store = Store(get_settings().db_path)
    if args.cmd == "list":
        for u in store.list_users():
            print(f"{u['username']:<25} {'admin' if u['is_admin'] else '':<6} last sign-in: {u['last_login'] or '—'}")
        return
    if args.cmd == "delete":
        store.delete_user(args.username)
        print(f"Deleted {args.username}.")
        return
    pw = getpass.getpass("Password (8+ characters): ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("Passwords don't match.")
    if args.cmd == "add":
        store.add_user(args.username, pw, is_admin=args.admin)
        print(f"Added {args.username}{' (admin)' if args.admin else ''}.")
    else:
        store.set_password(args.username, pw)
        print(f"Password changed for {args.username}.")


if __name__ == "__main__":
    main()
