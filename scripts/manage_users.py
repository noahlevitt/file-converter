"""Manage user accounts from the server shell.

  python scripts/manage_users.py add <username> [--admin]
  python scripts/manage_users.py password <username>
  python scripts/manage_users.py delete <username>
  python scripts/manage_users.py list

In Docker:  docker compose exec app python scripts/manage_users.py add jane

Overview
--------
A small admin tool for the per-user accounts used by the web app's login (``converter.auth``). It works directly
on the same SQLite database as the app (``DATA_DIR/converter.db``, from the same ``.env`` / environment
settings), so changes apply immediately, without restarting the app. Useful when nobody can sign in (e.g. the
only admin forgot their password) or for scripting account setup.

Commands:
    add <username> [--admin]   create an account; prompts twice for the password (8+ characters). --admin gives
                               access to the Users page and lets the user delete anyone's templates.
    password <username>        set a new password (prompts twice). Existing sign-ins are not affected.
    delete <username>          remove the account (no confirmation; an unknown name is silently ignored).
    list                       show every user, whether they are an admin and their last sign-in time (UTC).

Usernames are stored trimmed and lower-cased; passwords are only ever stored as bcrypt hashes. Passwords are
read with ``getpass`` so they are not echoed or saved in the shell history.

Exit status: 0 on success; 1 with "Passwords don't match." if the two prompts differ; 2 for invalid usage
(argparse). A too-short password, an existing username (add) or an unknown username (password) end with a
Python error from ``converter.storage.Store`` and a non-zero status.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Make the project root importable so "converter" resolves when the script is run as scripts/manage_users.py
# (the imports below must therefore come after this line, hence the noqa: E402 markers).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter.config import get_settings  # noqa: E402
from converter.storage import Store  # noqa: E402


def main() -> None:
    """Parse the command line and run one user-management command against the app's database.

    Side effects: reads/writes the ``users`` table; prompts on the terminal for passwords (add / password).
    """
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true")
    sub.add_parser("password").add_argument("username")
    sub.add_parser("delete").add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()

    # Same database the web app uses (created if it doesn't exist yet).
    store = Store(get_settings().db_path)
    if args.cmd == "list":
        for u in store.list_users():
            print(f"{u['username']:<25} {'admin' if u['is_admin'] else '':<6} last sign-in: {u['last_login'] or '—'}")
        return
    if args.cmd == "delete":
        store.delete_user(args.username)
        print(f"Deleted {args.username}.")
        return
    # Remaining commands (add, password) need a new password: ask twice, without echo, to catch typos.
    pw = getpass.getpass("Password (8+ characters): ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("Passwords don't match.")
    if args.cmd == "add":
        store.add_user(args.username, pw, is_admin=args.admin)
        print(f"Added {args.username}{' (admin)' if args.admin else ''}.")
    else:  # "password"
        store.set_password(args.username, pw)
        print(f"Password changed for {args.username}.")


if __name__ == "__main__":
    main()
