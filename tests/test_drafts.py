"""A browser refresh doesn't lose a mapping in progress on New import (drafts, since 0.15.0).

A refresh starts an empty Streamlit session. The file being mapped is therefore kept on disk as a draft, its random
id is in the page URL (?draft=<id>) and the page state is saved there on every change. After the refresh the page
restores the file, how it was read and the mapping. Drafts are deleted on Start import / Start over / another
upload, after 24 hours without changes, and are only restored for the user who uploaded the file.

The app-level tests drive the real app.py with Streamlit's AppTest; they share one data folder because the app
caches its Store for the whole process.
"""

import json
import os
import time

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from converter.mapping import FieldMapping
from converter.storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = (b"Phone,SMS Opt In,Last Campaign Sent,Last Visit\n5165550100,Y,09/01/2026,10/01/2026\n"
       b"9175551234,N,09/02/2026,10/02/2026\n")


# ---------------------------------------------------------------- storage
def test_draft_round_trip(tmp_path):
    """File, owner and state come back; each save replaces the state; delete removes everything."""
    s = Store(tmp_path / "c.db")
    did = s.create_draft("noah", "guests.csv", CSV)
    assert len(did) >= 20 and s.load_draft(did, "noah").state == {}
    assert s.save_draft_state(did, {"a": 1}) and s.save_draft_state(did, {"a": 2})
    d = s.load_draft(did, "noah")
    assert (d.filename, d.data, d.state) == ("guests.csv", CSV, {"a": 2})
    s.delete_draft(did)
    assert s.load_draft(did, "noah") is None and not s.save_draft_state(did, {"a": 3})


def test_drafts_are_private_and_ids_checked(tmp_path):
    """Another user can't open a draft, and an id that isn't a well-formed token never becomes a path."""
    s = Store(tmp_path / "c.db")
    did = s.create_draft("noah", "guests.csv", CSV)
    assert s.load_draft(did, "someone") is None
    for bad in ["../imports", "..", "", None, "a/b" * 10, did + "/../x"]:
        assert s.load_draft(bad, "noah") is None
        s.delete_draft(bad)  # no-op, never deletes outside the drafts folder
    assert s.load_draft(did, "noah") is not None


def test_abandoned_drafts_are_deleted(tmp_path):
    """Drafts unchanged for 24 hours are purged (they hold a client's file); recent ones stay."""
    s = Store(tmp_path / "c.db")
    old, new = s.create_draft("noah", "a.csv", CSV), s.create_draft("noah", "b.csv", CSV)
    past = time.time() - 25 * 3600
    for p in (s.drafts_root / old).iterdir():
        os.utime(p, (past, past))
    assert s.load_draft(old, "noah") is None  # expired drafts aren't restored even before the purge runs
    assert s.purge_drafts() == 1
    assert not (s.drafts_root / old).exists() and s.load_draft(new, "noah") is not None


# ---------------------------------------------------------------- app
@pytest.fixture(scope="module")
def app_store(tmp_path_factory):
    """The data folder the app uses in these tests (logins off, no Claude)."""
    data = tmp_path_factory.mktemp("appdata")
    old = {k: os.environ.get(k) for k in ("DATA_DIR", "AUTH_MODE", "ANTHROPIC_API_KEY")}
    os.environ.update(DATA_DIR=str(data), ANTHROPIC_API_KEY="")
    os.environ.pop("AUTH_MODE", None)
    st.cache_resource.clear()
    yield Store(data / "converter.db")
    st.cache_resource.clear()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def run_app(draft_id: str | None) -> AppTest:
    """Open New import as a reloaded browser tab would: a fresh session, with ?draft=<id> in the URL."""
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
    if draft_id:
        at.query_params["draft"] = draft_id
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_refresh_restores_the_mapping_in_progress(app_store, real_schema):
    """A person's edits, the typed template name, time zone and 'Remember' box all survive the reload."""
    did = app_store.create_draft("local", "optin.csv", CSV)
    first = run_app(did)  # the first visit maps the file and saves its state
    state = app_store.load_draft(did, "local").state
    mapping = state["mapping"]
    mapping["fields"]["consent"] = FieldMapping(sources=["SMS Opt In"], transform="copy", origin="user").model_dump(
        mode="json")
    state.update(mapping=mapping, template_name="Client X", timezone="America/Chicago", remember=False)
    app_store.save_draft_state(did, state)

    at = run_app(did)
    assert at.session_state["file_name"] == "optin.csv" and at.session_state["draft_id"] == did
    assert "Picked up where you left off" in at.info[0].value
    m = at.session_state["mapping"]
    assert m.get("consent").sources == ["SMS Opt In"] and m.get("consent").origin == "user"
    assert at.selectbox[0].value == "Central"
    assert at.checkbox(key="remember_mapping").value is False
    assert [t.value for t in at.text_input if t.label == "Template name"] == ["Client X"]
    assert at.query_params["draft"] == did
    assert first.session_state["mapping"].get("consent").transform == "none"  # before the edit


def test_changes_are_saved_for_the_next_refresh(app_store):
    """Typing a template name updates the saved state, so a later refresh shows it."""
    did = app_store.create_draft("local", "optin.csv", CSV)
    at = run_app(did)
    [box] = [t for t in at.text_input if t.label == "Template name"]
    box.input("Weekly export").run()
    assert app_store.load_draft(did, "local").state["template_name"] == "Weekly export"


def test_start_over_deletes_the_draft(app_store):
    """'Start over' removes the draft and its id from the URL; a later reload starts empty."""
    did = app_store.create_draft("local", "optin.csv", CSV)
    at = run_app(did)
    [start_over] = [b for b in at.button if b.label == "Start over"]
    start_over.click().run()
    assert "file_name" not in at.session_state and "draft" not in at.query_params
    assert app_store.load_draft(did, "local") is None


def test_missing_or_foreign_draft_is_not_restored(app_store):
    """A draft that is gone, or someone else's, isn't restored: the page says so once and drops the id."""
    other = app_store.create_draft("someone", "theirs.csv", CSV)
    for did in (other, "x" * 24):
        at = run_app(did)
        assert "file_name" not in at.session_state and "draft" not in at.query_params
        assert any("isn't kept any more" in c.value for c in at.caption)
    assert app_store.load_draft(other, "someone") is not None  # not deleted by someone else's visit


def test_saved_state_is_plain_json(app_store):
    """The state on disk is plain JSON (no pickles), so an edited file can't run code when it's restored."""
    did = app_store.create_draft("local", "optin.csv", CSV)
    run_app(did)
    raw = (app_store.drafts_root / did / "state.json").read_text()
    assert set(json.loads(raw)) == {"read_args", "mapping", "map_meta", "template_name", "timezone", "ai_used",
                                    "remember"}
