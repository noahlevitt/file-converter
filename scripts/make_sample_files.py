"""Generate deliberately messy sample files for testing (python scripts/make_sample_files.py).

Covers: title rows above the header, Windows-1252 encoding, semicolon/tab delimiters, no header row,
Excel numeric phones and ZIPs, Excel date cells, day-first dates, full names and full addresses in
one column, duplicates (same phone + same date) and non-duplicates (same phone, different date),
blank rows, and invalid values.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import openpyxl

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "sample_data"


def pos_export_csv(OUT: Path) -> None:
    rows = [
        "Daily Guest Export,,,,,,",
        "Generated 10/08/2026 11:42 PM,,,,,,",
        ",,,,,,",
        "Guest Name,Mobile #,Home Phone,E-Mail,Birthday,Join Date,Address",
        'José Martínez,(516) 555-0100,,JOSE.M@GMAIL.COM,03/14/1988,10/01/2026,"12 Main St Apt 4B, Hicksville, NY 11801"',
        '"SMITH, JOHN",516.555.0142,516-555-0199,jsmith@yahoo,7/4/1975,10/02/2026,"455 Old Country Rd, Westbury, NY 11590"',
        'Mary O\'Neil,,9175550123,mary@oneil.com,,10/02/2026,"9 Ocean Ave, Long Beach, New York 11561"',
        'José Martínez,(516) 555-0100,,JOSE.M@GMAIL.COM,03/14/1988,10/01/2026,"12 Main St Apt 4B, Hicksville, NY 11801"',
        'José Martínez,516-555-0100,,jose.m@gmail.com,03/14/1988,10/05/2026,"12 Main St Apt 4B, Hicksville, NY 11801"',
        ",,,,,,",
        'Bad Phone Guy,555-1234,,bad@example.com,01/01/1990,10/03/2026,"1 Fake St, Nowhere, NY 11111"',
        'Dr. Priya Patel MD,+1 (631) 555-0101 x22,,PRIYA@PATELMD.COM,1990-02-28,Oct 4 2026,"77 Jericho Tpke Suite 200, Jericho, NY 11753"',
        'Kevin Lee,6315550188,,,13/45/1999,10/06/2026,',
        'No Phone Person,,,nophone@example.com,,10/06/2026,"5 Elm St, Freeport, NY 11520"',
    ]
    (OUT / "pos_guest_export.csv").write_bytes(("\r\n".join(rows) + "\r\n").encode("cp1252"))


def loyalty_xlsx(OUT: Path) -> None:
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "Summary"
    ws0.append(["Loyalty Program Summary"])
    ws0.append(["Members", 6])
    ws = wb.create_sheet("Members")
    ws.append(["FIRST NAME", "LAST NAME", "PHONE NUMBER", "EMAIL ADDRESS", "SIGNUP", "CITY", "STATE", "ZIP CODE",
               "POINTS"])
    ws.append(["JOHN", "MCDONALD", 5165550100, "john@mcd.com", dt.datetime(2026, 9, 1), "HICKSVILLE", "New York", 11801, 120])
    ws.append(["ana", "garcia", 2015550111, "ana@garcia.net", dt.datetime(2026, 9, 2), "hoboken", "new jersey", 7030, 40])
    ws.append(["Li", "Wei", "201-555-0177", "LI.WEI@EXAMPLE.ORG", dt.datetime(2026, 9, 2), "Jersey City", "NJ", "07302", 0])
    ws.append(["Sam", "Rivera", 5165550100, "sam@rivera.com", dt.datetime(2026, 9, 3), "Hicksville", "NY", 11801, 15])
    ws.append(["Sam", "Rivera", 5165550100, "sam@rivera.com", dt.datetime(2026, 9, 3), "Hicksville", "NY", 11801, 15])
    ws.append(["Tara", "Mor", 2125550198.0, "tara@example.com", 46270, "New York", "N.Y.", 10001, 300])
    ws.append([None, None, None, None, None, None, None, None, None])
    ws.append(["Bad", "Zip", 7185550145, "bad.zip@example.com", dt.datetime(2026, 9, 4), "Brooklyn", "NY", "ABCDE", 5])
    wb.save(OUT / "loyalty_members.xlsx")


def eu_style_csv(OUT: Path) -> None:
    rows = [
        "Name;Surname;Cell;Email;Registered;Postcode",
        "Giulia;Rossi;+1 516 555 0123;giulia@rossi.it;15/03/2026;11801",
        "Marco;Bianchi;516 555 0124;marco@bianchi.it;01/04/2026;11590",
        "Elena;Verdi;516-555-0125;elena@verdi.it;28/02/2026;11753",
    ]
    (OUT / "eu_style_semicolon.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def no_header_tsv(OUT: Path) -> None:
    rows = [
        "5165550131\tamy@example.com\t2026-08-01",
        "5165550132\tbob@example.com\t2026-08-02",
        "5165550133\tcat@example.com\t2026-08-03",
    ]
    (OUT / "no_header.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def make_all(out: Path = DEFAULT_OUT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for fn in (pos_export_csv, loyalty_xlsx, eu_style_csv, no_header_tsv):
        fn(out)
    return out


if __name__ == "__main__":
    target = make_all(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
    print(f"Sample files written to {target}")
