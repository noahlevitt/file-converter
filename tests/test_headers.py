"""Finding the header row: titles and report lines above it, files with no header at all, and unsure cases.

A header row mistaken for data only costs a bad row ("Phone" isn't a number). A customer row mistaken for the
header is worse: that customer disappears and the columns get their values as names. So a row whose phone number,
email or date matches its column must never become the header, wherever the real header is (row 1, below a title
and report lines, or nowhere). Files without a header row must never be matched to another file's template either:
their stand-in column names (Column 1, Column 2…) say nothing about the layout, so such files don't use
templates at all.
"""

import io

import openpyxl
import pandas as pd
import pytest

from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, has_generic_columns, profile_columns
from converter.reader import detect_header, read_file
from converter.services import initial_mapping
from converter.storage import Store


def read(text: str):
    """Read CSV text the way an upload is read."""
    return read_file("f.csv", text.encode())


@pytest.mark.parametrize("text", [
    "Ana Lee,(516) 555-0100\nBen Cruz,(917) 555-1234\nCy Diaz,(516) 555-0111\n",
    "Ana Lee,516-555-0100\nBen Cruz,917-555-1234\n",
    "Ana Lee,5165550100\nBen Cruz,9175551234\n",
    "(516) 555-0100\n(917) 555-1234\n",
    "516-555-0100,ana@x.com\n917-555-1234,ben@x.com\n",
    "Ana,516-555-0100,03/15\nBen,917-555-1234,07/04\n",
    "Ana,Lee,ana@x.com,10/01/2026 3:15 PM\nBen,Cruz,ben@x.com,10/02/2026 1:00 PM\n",
    # the first customer has no phone ("none"): still not a header, because nothing in it is a label over a column
    "Ana,none,10/01/2026\nBen,917-555-1234,10/02/2026\nCy,516-555-0111,10/03/2026\n",
    # a single customer: a valid phone number is never a column label
    "Ana,516-555-0100\n",
    # a junk row between customers can't become the header: nothing below a customer can
    "718.849.4250\nno phone\n516-483-1912\n",
    # an unquoted comma shifted a customer's values right
    "Ana Lee,Jr,5165550100,10/01/2026\nBen Cruz,9175551234,10/02/2026\n",
])
def test_headerless_files_keep_every_customer(text):
    """Every row of a file without a header row is data: none is used as column names."""
    rr = read(text)
    assert rr.header_row is None and rr.header_sure
    assert len(rr.df) == text.count("\n") and has_generic_columns(list(rr.df.columns))


@pytest.mark.parametrize("text,row,columns", [
    ("Name,Phone\nAna,516-555-0100\nBen,917-555-1234\n", 0, ["Name", "Phone"]),
    # title, store and report-date lines and a blank row above the header
    ("Guest export\nStore,Kravit Bayside\nGenerated,10/12/2026\n\nName,Phone,Last Visit\n"
     "Ana,516-555-0100,10/01/2026\nBen,917-555-1234,10/02/2026\n", 4, ["Name", "Phone", "Last Visit"]),
    # a report line in the phone column above the header
    ("Exported 10/12/2026\nPhone,Name\n516-555-0100,Ana\n917-555-1234,Ben\n", 1, ["Phone", "Name"]),
    # labels with numbers and dates in them are still labels
    ("Name,Phone,Visits 2024,Total Spend (2024-01-01 - 2024-12-31)\nAna,516-555-0100,3,$45.00\n"
     "Ben,917-555-1234,1,$12.50\n", 0, ["Name", "Phone", "Visits 2024", "Total Spend (2024-01-01 - 2024-12-31)"]),
    # the first customer has no phone: the header is still the header, and that customer stays a data row
    ("Name,Phone,Visit\nAna,none,10/01/2026\nBen,917-555-1234,10/02/2026\nCy,516-555-0111,10/03/2026\n", 0,
     ["Name", "Phone", "Visit"]),
    # blank header cells
    ("Name,,Phone\nAna,x,516-555-0100\nBen,y,917-555-1234\n", 0, ["Name", "Column 2", "Phone"]),
    # a file with a single customer
    ("Name,Phone\nAna,516-555-0100\n", 0, ["Name", "Phone"]),
])
def test_header_found_wherever_it_is(text, row, columns):
    """The header is the label row just above the customers, below any titles and report lines."""
    rr = read(text)
    assert rr.header_row == row and rr.header_sure and list(rr.df.columns) == columns
    assert not any("customer-looking" in w for w in rr.warnings)


def test_report_line_directly_above_headerless_data_loses_no_customer():
    """A store line right above headerless customers becomes the column names; every customer is kept."""
    rr = read("Store,Kravit Bayside\nAna,516-555-0100\nBen,917-555-1234\n")
    assert rr.header_row == 0 and rr.df.iloc[:, 1].tolist() == ["516-555-0100", "917-555-1234"]


@pytest.mark.parametrize("text,row", [
    ("Ana,Lee\nBen,Cruz\n", 0),   # names only: nothing tells labels from values
    ("Name,Phone\n", 0),          # a header and no customers
])
def test_unsure_cases_ask_the_reviewer(text, row):
    """When nothing in the file separates labels from values, the reviewer is asked to check."""
    rr = read(text)
    assert rr.header_row == row and not rr.header_sure
    assert any("Couldn't tell for sure" in w for w in rr.warnings)


def test_misaligned_customer_below_the_header_is_kept():
    """'Smith, John' unquoted pushes the phone one column right; Smith is still a customer, not the header."""
    rr = read("Name,Phone\nSmith, John,5165550100\nAmy,5165550131\n")
    assert rr.header_row == 0 and len(rr.df) == 2


def test_customer_row_picked_as_header_by_hand_is_flagged():
    """Picking a customer row as the header (by hand) warns that its values will be lost as column names."""
    rr = read_file("f.csv", b"Ana,516-555-0100\nBen,917-555-1234\n", header_row=0)
    assert rr.header_row == 0 and any("customer-looking values ('516-555-0100')" in w for w in rr.warnings)


def test_excel_without_header():
    """An Excel sheet of numeric phone cells and real date cells, no header: all customers kept."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Ana", 5165550100, pd.Timestamp("2026-10-01").to_pydatetime()])
    ws.append(["Ben", 9175551234, pd.Timestamp("2026-10-02").to_pydatetime()])
    ws.append(["Cy", 5165550111, pd.Timestamp("2026-10-03").to_pydatetime()])
    buf = io.BytesIO()
    wb.save(buf)
    rr = read_file("x.xlsx", buf.getvalue())
    assert rr.header_row is None and len(rr.df) == 3 and rr.df.iloc[0, 1] == "5165550100"


def test_detect_header_examples():
    """The examples in detect_header's docstring."""
    assert detect_header([["Customer export"], [], ["Name", "Phone"], ["Ana", "516-555-0100"],
                          ["Ben", "917-555-1234"]]) == (2, True)
    assert detect_header([["Ana Lee", "(516) 555-0100"], ["Ben Cruz", "(917) 555-1234"]]) == (None, True)
    assert detect_header([["Store", "Kravit"], ["Name", "Phone", "Visit"],
                          ["Ana", "516-555-0100", "10/01/2026"]]) == (1, True)


def test_headerless_file_converts_every_customer(real_schema):
    """End to end: a headerless file maps its phone column from the values and imports every customer."""
    rr = read("Ana Lee,(516) 555-0100,10/01/2026\nBen Cruz,(917) 555-1234,10/02/2026\n"
              "Cy Diaz,(516) 555-0111,10/03/2026\n")
    m = auto_map(real_schema, profile_columns(rr.df))
    assert m.get("phone_number").sources == ["Column 2"]
    res = convert(rr.df, real_schema, m)
    assert res.imported == 3 and res.output["phone_number"].tolist() == ["5165550100", "9175551234", "5165550111"]


def test_headerless_phone_column_with_several_numbers_in_a_cell(real_schema):
    """A cell holding two numbers still counts as phone values, so the phone column of a headerless file is found."""
    rr = read("Ana Lee,(516) 555-0100,10/01/2026\nBen Cruz,(917) 555-1234 / 516-555-0199,10/02/2026\n"
              "Cy Diaz,(516) 555-0111,10/03/2026\n")
    m = auto_map(real_schema, profile_columns(rr.df))
    assert m.get("phone_number").sources == ["Column 2"]
    res = convert(rr.df, real_schema, m)
    assert res.imported == 4 and res.extra_rows == 1


def test_headerless_files_never_use_templates(real_schema, tmp_path):
    """Templates are neither matched to nor saved from files without a header row.

    Every headerless 3-column file has the same stand-in names, so a template would be applied to another client's
    file. One saved before this rule (as an older version allowed) is never matched either. Files with real
    headers match as before.
    """
    store = Store(tmp_path / "t.db")
    cols = ["Column 1", "Column 2", "Column 3"]
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Column 1"], transform="copy")})
    with pytest.raises(ValueError, match="without a header row"):
        store.save_template("Client A", cols, m, {}, real_schema.name, "noah")
    # A template saved by an older version: inserted directly, still never matched.
    store.save_template("Old", ["Phone"], m, {}, real_schema.name, "noah")
    with store._conn() as con:
        from converter.mapping import header_fingerprint
        con.execute("UPDATE templates SET fingerprint=?, headers_json=? WHERE name='Old'",
                    (header_fingerprint(cols), '["Column 1", "Column 2", "Column 3"]'))
    assert store.find_template(cols, real_schema.name) == (None, 0.0)
    df = pd.DataFrame({"Column 1": ["5165550100"], "Column 2": ["Ana"], "Column 3": ["03/15"]})
    sug = initial_mapping(real_schema, df, profile_columns(df), store, None, use_ai=False)
    assert sug.source == "rules" and sug.template is None and any("templates aren't used" in n for n in sug.notes)
    # Files with real headers still match as before.
    store.save_template("Named", ["Phone", "Name"], Mapping(fields={"phone_number": FieldMapping(
        sources=["Phone"], transform="copy")}), {}, real_schema.name, "noah")
    assert store.find_template(["name", "PHONE"], real_schema.name)[1] == 1.0


@pytest.mark.parametrize("columns,generic", [
    (["Column 1", "Column 2"], True), (["Column 1", "Phone"], False), ([], False), (["Column"], False),
])
def test_generic_column_names(columns, generic):
    """Only the reader's stand-in names (all of them) count as 'no header row'."""
    assert has_generic_columns(columns) is generic


# ---------------------------------------------------------------------------
# Generated files: the guarantee holds across many layouts, not just the hand-written cases above
# ---------------------------------------------------------------------------

def _generated_files(seed: int, count: int):
    """Yield (grid, true header row or None) for random customer exports, with and without a header row.

    Varied columns and phone/date formats, title and report lines above the header, and messy rows: values shifted
    by an unquoted comma, and "N/A" / "no phone" instead of a number.
    """
    import random
    from converter.reader import _clean_cell
    rnd = random.Random(seed)

    def phone():
        a, b, c = rnd.choice([516, 917, 631, 212, 201, 718]), rnd.randint(200, 999), rnd.randint(0, 9999)
        return rnd.choice([f"{a}{b}{c:04d}", f"({a}) {b}-{c:04d}", f"{a}-{b}-{c:04d}", f"{a}.{b}.{c:04d}",
                           f"+1 {a} {b} {c:04d}", f"1{a}{b}{c:04d}"])

    def date():
        m, d, y = rnd.randint(1, 12), rnd.randint(1, 28), rnd.randint(2023, 2026)
        return rnd.choice([f"{m:02d}/{d:02d}/{y}", f"{y}-{m:02d}-{d:02d}", f"{m:02d}/{d:02d}/{y} 3:15 PM", f"Oct {d}, {y}"])

    cols = {"Name": lambda: rnd.choice(["Ana Lee", "Ben Cruz", "José Martínez", "Di Ng"]),
            "Email": lambda: f"guest{rnd.randint(1, 999)}@example.com", "Last Visit": date,
            "Birthday": lambda: rnd.choice([f"{rnd.randint(1, 12):02d}/{rnd.randint(1, 28):02d}", ""]),
            "Total Spend": lambda: f"${rnd.randint(1, 500)}.{rnd.randint(0, 99):02d}",
            "Opt In": lambda: rnd.choice(["Y", "N"]), "City": lambda: rnd.choice(["Bayside", "Freeport", "Denver"]),
            "Address": lambda: f"{rnd.randint(1, 999)} Main St", "Visits": lambda: str(rnd.randint(0, 40))}
    titles = [[], [["Guest export"]], [["Store", "Kravit Bayside"]], [["Date range", "09/01/2026 - 09/30/2026"]],
              [["Customer List"], ["Store", "Kravit"], ["Generated", "10/12/2026"], []], [[]]]
    for i in range(count):
        with_header = i % 2 == 0
        names = rnd.sample(list(cols), rnd.randint(0, 5))
        names.insert(rnd.randint(0, len(names)), rnd.choice(["Phone", "Mobile", "Cell Phone"]))
        rows = [[phone() if n in ("Phone", "Mobile", "Cell Phone") else cols[n]() for n in names]
                for _ in range(rnd.choice([1, 2, 3, 5, 10, 30]))]
        for row in rows:
            x = rnd.random()
            if x < 0.12:
                row.insert(rnd.randrange(len(row)), rnd.choice(["Jr", "Inc", "Apt 2"]))
            elif x < 0.2:
                for j, n in enumerate(names):
                    if n in ("Phone", "Mobile", "Cell Phone"):
                        row[j] = rnd.choice(["", "N/A", "no phone"])
        top = rnd.choice(titles) if with_header else rnd.choice([[], [["Guest export"]]])
        grid = [list(t) for t in top] + ([names] if with_header else []) + rows
        yield [[_clean_cell(c) for c in r] for r in grid], (len(top) if with_header else None)


def test_no_customer_row_is_ever_dropped():
    """Across 1,000 generated files: no row with a valid phone or email is ever used as the header or skipped
    above it, and the header row is found in at least 97% of the files that have one.

    The misses are tiny one-column files with shifted or junk rows; in them the header is read as a data row
    (rejected as a bad row) or a report line becomes the column name - never a lost customer.
    """
    from converter.normalizers import looks_like_email, looks_like_phone
    found = with_header = 0
    for grid, truth in _generated_files(seed=12, count=1000):
        h = detect_header(grid)[0]
        dropped = grid[: h + 1] if h is not None else []
        assert not any(c and (looks_like_phone(c) or looks_like_email(c)) for r in dropped for c in r), grid[:6]
        if truth is not None:
            with_header += 1
            found += h == truth
    assert found / with_header >= 0.97
