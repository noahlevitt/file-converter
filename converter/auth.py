"""Login gate for the Streamlit app (per-user accounts stored in SQLite with bcrypt hashes)."""

from __future__ import annotations

import streamlit as st

from .config import Settings
from .storage import Store


def ensure_bootstrap_admin(store: Store, settings: Settings) -> None:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD when the user table is empty."""
    if store.user_count() == 0 and settings.admin_username and settings.admin_password:
        store.add_user(settings.admin_username, settings.admin_password, is_admin=True)


def require_login(store: Store, settings: Settings, title: str) -> dict:
    """Render the login (or first-run setup) form until a user is signed in. Returns the user dict."""
    if settings.auth_mode == "none":
        return {"username": "local", "is_admin": True}
    if st.session_state.get("user"):
        return st.session_state["user"]

    ensure_bootstrap_admin(store, settings)
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown(f"### {title}")
        if store.user_count() == 0:
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
                            st.session_state["user"] = {"username": u.strip().lower(), "is_admin": True}
                            st.rerun()
                        except Exception as exc:
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
                    st.error("Wrong username or password.")
    st.stop()


def logout() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
