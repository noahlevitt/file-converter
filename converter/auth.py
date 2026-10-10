"""Login gate for the Streamlit app (per-user accounts stored in SQLite with bcrypt hashes).

Overview
--------
``app.py`` calls :func:`require_login` near the top of every script run. Streamlit re-runs the whole script on
every interaction, so the gate is cheap when someone is already signed in (one ``st.session_state`` lookup) and
otherwise draws the sign-in form and stops the script there, so no page content is shown to anonymous users.

Login flow
    1. ``AUTH_MODE=none`` → no login at all; everyone is user "local" with admin rights (single-user setups only).
    2. Already signed in during this browser session (``st.session_state["user"]``) → return that user.
    3. :func:`ensure_bootstrap_admin` → if the user table is empty and ``ADMIN_USERNAME``/``ADMIN_PASSWORD`` are
       set, create that admin account (so a fresh Docker deployment is usable without a setup screen).
    4. Still no users → **first-run setup** form: whoever opens the app first creates the administrator account
       (password typed twice, 8+ characters) and is signed in immediately.
    5. Otherwise → **sign-in** form. ``Store.verify_user`` checks the password against the bcrypt hash (and
       records ``last_login``); on success the user dict is put in the session and the page re-runs.

Sessions live only in Streamlit's per-browser-tab session state: there are no cookies or tokens, so reloading or
closing the tab, or restarting the server, signs the user out. Passwords are never stored or logged in plain
text; hashing and the 8-character minimum are enforced by ``storage.Store``. More accounts are added by an admin
in the app or with ``scripts/manage_users.py``.
"""

from __future__ import annotations

import streamlit as st

from .config import Settings
from .storage import Store


def ensure_bootstrap_admin(store: Store, settings: Settings) -> None:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD when the user table is empty.

    Does nothing once any user exists (so changing the variables later has no effect) or when either variable is
    empty. Side effect: may INSERT one admin row into ``users``. ``Store.add_user`` raises ``ValueError`` if the
    configured password is shorter than 8 characters.
    """
    if store.user_count() == 0 and settings.admin_username and settings.admin_password:
        store.add_user(settings.admin_username, settings.admin_password, is_admin=True)


def require_login(store: Store, settings: Settings, title: str) -> dict:
    """Render the login (or first-run setup) form until a user is signed in. Returns the user dict.

    Args:
        store: Shared storage holding the ``users`` table.
        settings: Runtime settings (``auth_mode``, bootstrap admin credentials).
        title: App title shown above the form.

    Returns:
        ``{"username": str, "is_admin": bool}`` for the signed-in user (usernames are lower-cased).

    This function only *returns* when a user is signed in. Otherwise it draws the setup or sign-in form and calls
    ``st.stop()``, which ends the current script run, so nothing below the gate in ``app.py`` is rendered. After a
    successful setup or sign-in it calls ``st.rerun()``, and on the next run step 2 of the flow returns the user.

    Side effects: may create the bootstrap admin, may create the first admin from the setup form, and updates
    ``last_login`` on a successful sign-in; writes ``st.session_state["user"]``.
    """
    if settings.auth_mode == "none":
        return {"username": "local", "is_admin": True}
    if st.session_state.get("user"):
        return st.session_state["user"]

    ensure_bootstrap_admin(store, settings)
    _, col, _ = st.columns([1, 1.2, 1])  # narrow centred column for the form
    with col:
        st.markdown(f"### {title}")
        if store.user_count() == 0:
            # First run with no ADMIN_* variables: let the first visitor create the administrator.
            st.info("First run: create the administrator account. You can add the rest of the team afterwards.")
            with st.form("setup"):
                u = st.text_input("Admin username")
                p1 = st.text_input("Password (8+ characters)", type="password")
                p2 = st.text_input("Confirm password", type="password")
                if st.form_submit_button("Create admin account", type="primary", width="stretch"):
                    if p1 != p2:
                        st.error("Passwords don't match.")
                    else:
                        try:
                            store.add_user(u, p1, is_admin=True)
                            # Same normalization as Store.add_user, so the session name matches the stored one.
                            st.session_state["user"] = {"username": u.strip().lower(), "is_admin": True}
                            st.rerun()
                        except Exception as exc:
                            # e.g. empty username or password shorter than 8 characters (ValueError from the store).
                            st.error(str(exc))
            st.stop()

        with st.form("login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                user = store.verify_user(u, p)
                if user:
                    st.session_state["user"] = user
                    st.rerun()
                else:
                    # Same message for unknown user and wrong password, so usernames can't be probed.
                    st.error("Wrong username or password.")
    st.stop()


def logout() -> None:
    """Sign the current browser session out by clearing all of its session state.

    Everything is cleared, not just the user: an in-progress upload, mapping or notes from this session are
    discarded too, so the next person at the same browser starts clean. The caller re-runs the page, which then
    shows the sign-in form. Other sessions and running imports are not affected.
    """
    for key in list(st.session_state.keys()):
        del st.session_state[key]
