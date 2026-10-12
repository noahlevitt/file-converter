"""Logins are switched off by default for now (AUTH_MODE=none) and come back with AUTH_MODE=password.

Switching them off must not delete anything: accounts stay in the database and work again when logins are on.
"""

from converter.auth import require_login
from converter.config import Settings
from converter.storage import Store


def test_logins_off_by_default(monkeypatch):
    """Without AUTH_MODE the app has no login; everyone is the local admin."""
    monkeypatch.delenv("AUTH_MODE", raising=False)
    settings = Settings()
    assert settings.auth_mode == "none"
    assert require_login(None, settings, "x") == {"username": "local", "is_admin": True}


def test_logins_back_on_with_password_mode(monkeypatch, tmp_path):
    """AUTH_MODE=password turns logins back on, and accounts created earlier still work."""
    monkeypatch.setenv("AUTH_MODE", " Password ")
    assert Settings().auth_mode == "password"
    store = Store(tmp_path / "a.db")
    store.add_user("noah", "long-enough-pw", is_admin=True)
    assert store.verify_user("noah", "long-enough-pw")["username"] == "noah"
