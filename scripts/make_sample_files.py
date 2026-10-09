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


# ---------------------------------------------------------------------------
# Samples shaped like real source files for the generic import format
# ---------------------------------------------------------------------------

def restaurant_crm_csv(OUT: Path) -> None:
    """CRM export: full names, an Opt-In column that must NOT reach `consent`, mixed date formats,
    birthdays without a year, an exact duplicate row and a same-phone/different-visit row."""
    header = ("Customer Name,Mobile,Email,Opt-In Status,Last Visit,Last Order Total,Total Visits,Last Call,"
              "Last Text Received,Last Campaign Sent,Last Reward Redeemed,Birthday,Anniversary,Created")
    rows = [
        'Maria Lopez,(516) 555-0100,maria@example.com,Yes,10/05/2026 7:42 PM,$38.50,12,10/01/2026 12:15 PM,'
        '10/04/2026 6:01 PM,10/03/2026 11:00 AM,09/28/2026 8:30 PM,09/12,06/20/2015,2024-01-15',
        'Tom Becker,+1 631-555-0101,tom@example.com,No,2026-10-03 18:15:00,$12.00,3,,,2026-10-03 11:00:00,,'
        'Mar 3,,2025-05-02',
        'Aisha Khan,917.555.0123,aisha@example.com,true,Oct 2 2026 1:05pm,$64.20,25,,10/02/2026 1:30 PM,'
        '10/03/2026 11:00 AM,10/02/2026 1:10 PM,1985-07-04,,2023-11-30',
        'Maria Lopez,(516) 555-0100,maria@example.com,Yes,10/05/2026 7:42 PM,$38.50,12,10/01/2026 12:15 PM,'
        '10/04/2026 6:01 PM,10/03/2026 11:00 AM,09/28/2026 8:30 PM,09/12,06/20/2015,2024-01-15',
        'Maria Lopez,516-555-0100,maria@example.com,Yes,10/07/2026 8:10 PM,$22.00,13,10/01/2026 12:15 PM,'
        '10/04/2026 6:01 PM,10/03/2026 11:00 AM,09/28/2026 8:30 PM,09/12,06/20/2015,2024-01-15',
        'No Phone Guest,,nophone@example.com,Yes,10/06/2026 9:00 PM,$15.00,1,,,,,,,2026-10-06',
        'Short Number,555-0199,short@example.com,,10/06/2026 9:15 PM,$9.99,1,,,,,02/30,,2026-10-06',
        'Daniel Cho,6315550188,,No,0000-00-00 00:00:00,$0.00,0,,,,,September 12th,12/31,2026-10-07',
    ]
    (OUT / "restaurant_crm_export.csv").write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def loyalty_activity_xlsx(OUT: Path) -> None:
    """Loyalty platform export: Excel date-time cells, numeric phones, DOB as Excel dates or text."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Activity"
    ws.append(["Loyalty Activity Report"])
    ws.append([])
    ws.append(["Phone", "Last Spin", "Last Redeemed", "DOB", "Points"])
    ws.append([5165550100, dt.datetime(2026, 10, 4, 18, 30), dt.datetime(2026, 9, 30, 12, 5), dt.datetime(1990, 2, 28), 140])
    ws.append([2015550111, dt.datetime(2026, 10, 1, 9, 5), None, "12-Sep", 20])
    ws.append([9175550123.0, None, dt.datetime(2026, 10, 2, 13, 10), "02/29", 75])
    ws.append(["N/A", dt.datetime(2026, 10, 3, 20, 0), None, None, 0])
    wb.save(OUT / "loyalty_activity.xlsx")


def make_all(out: Path = DEFAULT_OUT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for fn in (pos_export_csv, loyalty_xlsx, eu_style_csv, no_header_tsv, restaurant_crm_csv, loyalty_activity_xlsx):
        fn(out)
    return out


if __name__ == "__main__":
    target = make_all(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
    print(f"Sample files written to {target}")
