"""Light/dark mode: the per-user preference store and the dark colours of the side-by-side comparison.

The switch itself happens in the browser (app.py, "Light / dark mode"); here we check what the server side owns:
the choice is remembered per user, removed with the user, and the comparison table (our own HTML, not a Streamlit
element) is drawn with matching dark colours that keep each colour's meaning.
"""

import pandas as pd

from converter.compare import CSS, DARK_CSS, build_compare_html, column_plan, legend_html
from converter.engine import convert
from converter.mapping import FieldMapping, Mapping
from converter.storage import Store


def test_theme_preference_is_saved_per_user(tmp_path):
    """Unset → None; saved values are read back, overwritten, kept per user and removed with the user."""
    st = Store(tmp_path / "t.db")
    st.add_user("ana", "password1")
    st.add_user("ben", "password2")
    assert st.get_user_pref("ana", "theme") is None
    st.set_user_pref("Ana", "theme", "dark")  # usernames are case-insensitive, like sign-in
    assert st.get_user_pref("ana", "theme") == "dark"
    st.set_user_pref("ana", "theme", "light")
    assert st.get_user_pref("ana", "theme") == "light" and st.get_user_pref("ben", "theme") is None
    st.set_user_pref("local", "theme", "dark")  # AUTH_MODE=none user, not in the users table
    assert st.get_user_pref("local", "theme") == "dark"
    st.delete_user("ana")
    assert st.get_user_pref("ana", "theme") is None


def test_preferences_table_is_added_to_an_existing_database(tmp_path):
    """A database created before the preferences table existed gets it on the next start (no migration step)."""
    db = tmp_path / "old.db"
    Store(db)
    import sqlite3
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE user_prefs")
    Store(db).set_user_pref("ana", "theme", "dark")
    assert Store(db).get_user_pref("ana", "theme") == "dark"


def test_comparison_in_dark_mode(real_schema):
    """dark=True adds the dark colours and dark legend swatches; light mode is unchanged."""
    df = pd.DataFrame({"Phone": ["(516) 555-0100", "123"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m)
    plan = column_plan(real_schema, m, list(df.columns))
    light = build_compare_html(df, res.audit, [0, 1], real_schema, plan)
    dark = build_compare_html(df, res.audit, [0, 1], real_schema, plan, dark=True)
    assert CSS in light and DARK_CSS not in light
    assert CSS in dark and DARK_CSS in dark and dark.index(CSS) < dark.index(DARK_CSS)  # dark rules win
    assert "#ffe2c6" in legend_html() and "#4a2a0f" in legend_html(dark=True)  # repaired: orange in both modes
    # Every status colour class has a dark version.
    for cls in ("c-fmt", "c-drv", "c-def", "c-fix", "c-blk", "c-rpr", "c-rej"):
        assert f"td.{cls}" in DARK_CSS
