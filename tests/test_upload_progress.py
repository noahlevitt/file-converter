"""Reading a big file on New import shows what it is doing, and takes less time (0.24.0).

Before: after a big file was uploaded, the page looked idle for several seconds (a 150,000-row CSV: about 6 s,
longer with Claude) while the file was read, its columns profiled and matched; Streamlit only showed a small
"Running" in the corner. Now a box under the uploader shows each step as it runs (⏳), what is done (✓, with its
time) and which column the profiling is on; it goes away when the page below shows the file. The two slowest
steps also got faster: each distinct value of a column is cleaned once when profiling (most cells repeat), and
read_file no longer cleans every cell twice.

The box itself is only on screen while the script runs, which AppTest doesn't capture mid-run: the page was checked
in a browser (screenshots during a 150,000-row upload). Here: the progress calls, the faster code giving exactly
the same results as before, and the pages that read a file still drawing with nothing left behind.
"""

import os

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from converter import normalizers as N
from converter.mapping import ColumnProfile, profile_columns
from converter.reader import build_frame, read_file
from converter.storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _profile_every_cell(df: pd.DataFrame, sample_size: int = 300, n_samples: int = 15) -> list[ColumnProfile]:
    """profile_columns as it was before 0.24.0 (cleaning every cell), to compare with."""
    out = []
    for col in df.columns:
        series = [N.clean_text(v) for v in df[col].tolist()]
        non_empty = [v for v in series if v]
        uniq = list(dict.fromkeys(non_empty))
        sample = uniq[:sample_size]
        shares = {}
        if sample:
            n = len(sample)
            shares = {"phone": sum(N.looks_like_phones(v) for v in sample) / n,
                      "email": sum(N.looks_like_email(v) for v in sample) / n,
                      "date": sum(N.looks_like_date(v) for v in sample) / n,
                      "zip": sum(N.looks_like_zip(v) for v in sample) / n,
                      "state": sum(N.looks_like_state(v) for v in sample) / n}
        out.append(ColumnProfile(name=col, non_empty=len(non_empty), total=len(df), unique=len(uniq),
                                 samples=[v[:120] for v in uniq[:n_samples]],
                                 shares={k: round(v, 3) for k, v in shares.items()},
                                 max_length=max((len(v) for v in non_empty), default=0)))
    return out


def test_profiling_reports_each_column():
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Phone": ["5165550101", ""], "Email": ["a@x.com", "b@x.com"]})
    calls = []
    profile_columns(df, progress=lambda i, n, name: calls.append((i, n, name)))
    assert calls == [(1, 3, "Name"), (2, 3, "Phone"), (3, 3, "Email")]


def test_profiling_each_distinct_value_once_gives_the_same_profiles():
    df = pd.DataFrame({
        "Phone": ["(516) 555-0101", "516-555-0101 ", "n/a", "", "-", "5165550101", "(516) 555-0101"] * 50,
        "Notes": [" a ", "a", "b c", None, "NULL", "x" * 200, "a"] * 50,
        "When": ["10/01/2026", "2026-10-01", " 10/01/2026", "never", "", "Oct 1", "10/01/2026"] * 50,
    })
    assert ([p.model_dump() for p in profile_columns(df, n_samples=20)]
            == [p.model_dump() for p in _profile_every_cell(df, n_samples=20)])


def test_read_file_cleans_once_with_the_same_result():
    grid = [["﻿Name", "Phone ", ""], [" Ann ", "516 555 0101", ""], ["", "", ""]]
    once = build_frame([[c.replace(" ", " ").replace("﻿", "").strip() for c in r] for r in grid], 0,
                       cleaned=True)
    assert once.equals(build_frame(grid, 0))
    rr = read_file("x.csv", "Name,Phone\n Ann ,516-555-0101\n".encode())
    assert list(rr.df.columns) == ["Name", "Phone"] and rr.df["Name"].tolist() == ["Ann"]


def test_reading_a_file_on_the_page_leaves_no_progress_box(tmp_path):
    """A draft without saved state is read and mapped like an upload (load_and_map), one with state is restored
    (restore_draft): both show the progress box while they work and remove it after."""
    old = {k: os.environ.get(k) for k in ("DATA_DIR", "AUTH_MODE", "ANTHROPIC_API_KEY")}
    os.environ.update(DATA_DIR=str(tmp_path), ANTHROPIC_API_KEY="")
    os.environ.pop("AUTH_MODE", None)
    st.cache_resource.clear()
    try:
        data = b"Name,Phone,Email\n" + b"".join(f"P{i},516-555-{i:04d},p{i}@x.com\n".encode() for i in range(2000))
        did = Store(tmp_path / "converter.db").create_draft("local", "people.csv", data)
        for _ in range(2):  # first: read and mapped (no state yet); then: restored from the state just saved
            at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
            at.query_params["draft"] = did
            at.run()
            assert not at.exception, [e.value for e in at.exception]
            assert any("2,000 rows" in m.value for m in at.markdown)
            texts = [e.value for e in [*at.markdown, *at.caption] if isinstance(e.value, str)]
            assert not any("large files take a moment" in t or "⏳" in t for t in texts)
            assert at.session_state["mapping"].get("phone_number").sources == ["Phone"]
    finally:
        st.cache_resource.clear()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
