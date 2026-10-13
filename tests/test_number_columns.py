"""A column of IDs is never taken for the phone column; WhatsApp columns are phone columns (0.16.0).

Before: "Account Number", "Card Number", "Customer Number", "Invoice Number" and "Ticket Number" look a lot like
"contact number" / "phone number" to the fuzzy header comparison, and ten-digit IDs are often valid phone numbers by
chance. In a file whose real phone column couldn't be used (7-digit numbers with a separate area code), the ID column
became the phone column: every customer would have been texted at a stranger's number. Such headers now give no
header evidence for the phone field; a column of them is only used when nearly all its values are phone numbers
(some loyalty systems use the phone number as the member number). "WhatsApp" was read as the words "whats" + "app"
and wasn't recognised as a phone header.
"""

import random

import pandas as pd
import pytest

from converter.engine import convert
from converter.heuristics import is_phone_header, names_other_number
from converter.mapping import profile_columns
from converter.services import initial_mapping


def phone_sources(schema, df):
    """The columns the automatic mapping (no Claude) uses for the phone number."""
    m = initial_mapping(schema, df, profile_columns(df), None, None, use_ai=False).mapping
    return m.get("phone_number").sources


def seven_digit_file(id_header: str) -> pd.DataFrame:
    """40 customers with a 7-digit phone, a separate area code and random 10-digit IDs."""
    rnd = random.Random(1)
    phones = [f"{rnd.randint(2, 9)}{rnd.randint(10, 99)}-{rnd.randint(1000, 9999)}" for _ in range(40)]
    return pd.DataFrame({"Name": [f"n{i}" for i in range(40)], "Area Code": ["516"] * 40, "Phone": phones,
                         id_header: [str(rnd.randint(2_000_000_000, 9_999_999_999)) for _ in range(40)]})


@pytest.mark.parametrize("header", ["Account Number", "Card Number", "Customer Number", "Invoice Number",
                                    "Ticket Number", "Order #", "Member No", "Loyalty ID"])
def test_id_column_is_never_the_phone(real_schema, header):
    """No usable phone column: the phone stays unmapped (and the area code is never added), the IDs are not used."""
    df = seven_digit_file(header)
    assert phone_sources(real_schema, df) == []


def test_number_column_full_of_phone_numbers_is_still_used(real_schema):
    good = ["5165550100", "9175551234", "2125550142", "6315550188", "7185550142", "2015550111"]
    df = pd.DataFrame({"Name": list("abcdef"), "Member Number": good})
    assert phone_sources(real_schema, df) == ["Member Number"]
    assert convert(df, real_schema, initial_mapping(real_schema, df, profile_columns(df), None, None,
                                                    use_ai=False).mapping).imported == 6


@pytest.mark.parametrize("header,other", [
    ("Account Number", True), ("Order #", True), ("Loyalty ID", True), ("Customer Number", True),
    ("Customer Phone", False), ("Guest Mobile", False), ("Phone", False), ("Contact Number", False),
])
def test_names_other_number(header, other):
    assert names_other_number(header) is other


@pytest.mark.parametrize("header", ["WhatsApp", "WhatsApp Number", "Whats App", "whatsappNumber", "WHATSAPP"])
def test_whatsapp_is_a_phone_header(real_schema, header):
    assert is_phone_header(header)
    # Empty in this file: still kept next to the phone column, so a template saved from it covers files where it's
    # filled.
    df = pd.DataFrame({"Phone": ["5165550101", "9175551235", "2125550142"], header: ["", "", ""]})
    assert phone_sources(real_schema, df) == ["Phone", header]
