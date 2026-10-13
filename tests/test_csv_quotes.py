"""A quote mark that is never closed costs one row, not the rest of the file; cells of any size read (0.16.0).

Before: `x,5165550002,"VIP - comps dessert` (a quote typed at the start of a note, never closed) made the csv
reader join every later line into that one cell, so a 1,000-row file came out as 3 rows, with no warning. A cell
over 128 KB stopped the file with "field larger than field limit". Now the quote is read as a plain character,
every row after it is read normally and a warning names the row. Real multi-line cells (addresses, long notes) are
kept as before, and a quote inside a cell (5" tall) is an ordinary character, as in Excel.
"""

import time

import pytest

from converter.reader import read_file


def customers(n: int, notes: dict | None = None, header: str = "Name,Phone,Notes") -> str:
    """A CSV of n customers; `notes` replaces whole lines by index (0 = header)."""
    lines = [header] + [f"n{i},51655{i:05d},ok" for i in range(n)]
    for k, line in (notes or {}).items():
        lines[k] = line
    return "\n".join(lines) + "\n"


def test_unclosed_quote_costs_only_its_row():
    t = time.time()
    rr = read_file("t.csv", customers(1000, {3: 'x,5165550002,"VIP - comps dessert'}).encode())
    assert time.time() - t < 2
    assert len(rr.df) == 1000
    assert rr.df["Notes"].iloc[2] == '"VIP - comps dessert'   # the quote is kept as typed
    assert rr.df["Phone"].iloc[3] == "5165500003"
    assert any(w.startswith('Row 4: a quote mark (") is never closed') for w in rr.warnings)


def test_unclosed_quote_with_windows_line_ends():
    text = customers(50, {10: 'x,5165550009,"call after 5'}).replace("\n", "\r\n")
    rr = read_file("t.csv", text.encode())
    assert len(rr.df) == 50 and rr.df["Phone"].iloc[10] == "5165500010"
    assert any("Row 11" in w for w in rr.warnings)


def test_unclosed_quote_in_a_two_column_file():
    """Two columns: a note line easily has one comma, so a still-open cell takes 20 row-like lines to decide."""
    lines = ["Phone,Notes"] + [f"51655{i:05d},ok" for i in range(100)]
    lines[5] = '5165500004,"likes the patio'
    rr = read_file("t.csv", ("\n".join(lines) + "\n").encode())
    assert len(rr.df) == 100 and rr.df["Phone"].iloc[99] == "5165500099"
    assert any("Row 6" in w for w in rr.warnings)


def test_multi_line_cells_are_kept():
    """A long quoted note and a multi-line address stay one cell each; no warning."""
    note = "\n".join(f"line {i} of the note" for i in range(60))
    address = "12 Main St\nApt 4\nGarden City, NY 11530"
    text = customers(20, {2: f'b,5165550001,"{note}"', 5: f'e,5165550004,"{address}"'})
    rr = read_file("t.csv", text.encode())
    assert len(rr.df) == 20 and not rr.warnings
    assert rr.df["Notes"].iloc[1] == note and rr.df["Notes"].iloc[4] == address


def test_unclosed_quote_near_the_end_of_the_file():
    """Fewer lines left than it takes to look like rows: the quote is still open at the end of the file."""
    lines = ["Phone,Notes"] + [f"51655{i:05d},ok" for i in range(30)]
    lines[28] = '5165500027,"wants a call back'
    rr = read_file("t.csv", ("\n".join(lines) + "\n").encode())
    assert len(rr.df) == 30 and rr.df["Phone"].iloc[-1] == "5165500029"
    assert any("Row 29" in w for w in rr.warnings)


def test_quote_inside_a_cell_is_not_a_stray_quote():
    """A quote in the middle of a cell (5" tall) is an ordinary character, as in Excel: no warning, nothing moves,
    also when the file is read the second way because its last row has a multi-line cell."""
    text = customers(10, {2: 'b,5165550001,5" tall', 5: 'e,5165550004,6" wide',
                          10: 'j,5165550009,"12 Main St\nApt 4\nGarden City NY 11530"'})
    rr = read_file("t.csv", text.encode())
    assert not rr.warnings and len(rr.df) == 10
    assert rr.df["Notes"].iloc[1] == '5" tall' and rr.df["Notes"].iloc[9] == "12 Main St\nApt 4\nGarden City NY 11530"


@pytest.mark.parametrize("second", [11, 12, 150])
def test_two_stray_quotes(second):
    """The first stray quote "closes" at the second one: that is told apart from a real cell, also on the next line."""
    rr = read_file("t.csv", customers(200, {10: 'x,5165550009,"VIP', second: 'y,5165550149,"no calls'}).encode())
    assert len(rr.df) == 200 and rr.df["Phone"].iloc[199] == "5165500199"
    assert any(w.startswith(f"Rows 11, {second + 1}: a quote mark") for w in rr.warnings)


def test_stray_quote_closed_by_a_later_quoted_cell():
    rr = read_file("t.csv", customers(50, {10: 'x,5165550009,"VIP', 14: 'n13,5165500013,"Smith, John"'}).encode())
    assert len(rr.df) == 50 and rr.df["Notes"].iloc[13] == "Smith, John"
    assert any(w.startswith("Row 11: a quote mark") for w in rr.warnings)


def test_note_whose_lines_look_like_rows_is_kept():
    """A properly closed note is kept even when each of its lines has as many commas as a row (up to 20 lines)."""
    note = "\n".join(f"called, left message, {i}" for i in range(12))
    rr = read_file("t.csv", customers(10, {2: f'b,5165550001,"{note}"'}).encode())
    assert len(rr.df) == 10 and not rr.warnings and rr.df["Notes"].iloc[1] == note


def test_stray_quote_closed_by_a_ditto_mark_is_flagged():
    """A stray quote that a `"` ditto mark (or `55"`) closes 3 lines later can't be told from a real note with many
    commas: the rows stay joined as the csv module reads them, but a warning says to check that row."""
    rr = read_file("t.csv", customers(50, {10: 'x,5165550009,"VIP', 13: 'n12,5165500012,"'}).encode())
    assert len(rr.df) == 47
    assert any(w.startswith("Row 11: one cell holds several lines that each look like a row") for w in rr.warnings)


def test_cell_over_128_kb_reads():
    big = "x" * 200_000
    rr = read_file("t.csv", f'Phone,Notes\n5165550100,"{big}"\n9175551234,short\n'.encode())
    assert rr.df["Notes"].iloc[0] == big and rr.df["Phone"].tolist() == ["5165550100", "9175551234"]
