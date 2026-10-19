"""The two mapping tables on New import keep their place when edited (KNOWN_ISSUES #42, fixed in 0.23.1).

Before: both tables (the field grid and Columns in your file) showed at most ten rows and scrolled inside
themselves. Every edit gives a table a new key (so the edit isn't applied twice), which rebuilt it scrolled back to
its first row: in a file with 30 columns, the row just edited disappeared. Now both are as tall as their content
(``height="content"``) and the page scrolls instead, so the edited row stays where it was. Customer Records fixed
the same page this way in its 1.0.2.

Streamlit's AppTest doesn't report an element's height, so the setting is checked in app.py's source; the page is
also opened with a wide file to check it still draws. (Checked by hand in a browser: editing row 25 of 31 keeps it
on screen.)
"""

import ast
import os

import streamlit as st
from streamlit.testing.v1 import AppTest

from converter.storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_editor_calls() -> list[ast.Call]:
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "data_editor"]


def test_both_mapping_tables_are_as_tall_as_their_content():
    calls = _data_editor_calls()
    assert len(calls) == 2  # the field grid and Columns in your file
    for call in calls:
        height = {k.arg: k.value for k in call.keywords}.get("height")
        assert isinstance(height, ast.Constant) and height.value == "content", ast.unparse(call)[:80]


def test_a_wide_file_draws(tmp_path):
    old = {k: os.environ.get(k) for k in ("DATA_DIR", "AUTH_MODE", "ANTHROPIC_API_KEY")}
    os.environ.update(DATA_DIR=str(tmp_path), ANTHROPIC_API_KEY="")
    os.environ.pop("AUTH_MODE", None)
    st.cache_resource.clear()
    try:
        cols = ["Name", "Phone"] + [f"Extra Field {i}" for i in range(1, 30)]
        rows = [",".join(cols), ",".join(["Ann", "516-555-0101"] + ["x"] * 29)]
        did = Store(tmp_path / "converter.db").create_draft("local", "wide.csv", "\n".join(rows).encode())
        at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
        at.query_params["draft"] = did
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        editors = [d for d in at.dataframe if d.proto.editing_mode]
        assert len(editors) == 2
    finally:
        st.cache_resource.clear()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
