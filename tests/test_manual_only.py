"""Consent and last_contacted are filled only when a person maps a column to them, for that file.

Nothing automatic may fill them: not the rules, not Claude, not a default - and not a saved template, because a
template is applied to the next file without anyone choosing the column again. A person can still map them on the
mapping page; consent values then become true / false.
"""

import pandas as pd
import pytest
from pydantic import ValidationError

from converter.editor import apply_column_edit
from converter.engine import convert
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.schema import FieldSpec
from converter.services import initial_mapping, template_mapping
from converter.storage import Store

COLUMNS = ["Phone", "SMS Opt In", "Last Campaign Sent", "Last Visit"]


def file_df():
    """A file with a phone, an opt-in column, the date we last texted them and a last visit."""
    return pd.DataFrame({"Phone": ["5165550100", "9175551234", "6315550188"], "SMS Opt In": ["Y", "N", "Y"],
                         "Last Campaign Sent": ["09/01/2026"] * 3, "Last Visit": ["10/01/2026"] * 3})


def hand_mapped(real_schema):
    """The mapping after a person sent 'SMS Opt In' to consent and 'Last Campaign Sent' to last_contacted."""
    df = file_df()
    m = initial_mapping(real_schema, df, profile_columns(df), None, None, use_ai=False).mapping
    m, _ = apply_column_edit(real_schema, m, "SMS Opt In", targets=["consent"], settings={"consent": "both"})
    m, _ = apply_column_edit(real_schema, m, "Last Campaign Sent", targets=["last_contacted"])
    return m


def test_nothing_automatic_fills_them(real_schema):
    """The automatic mapping leaves consent and last_contacted empty, even with obvious columns in the file."""
    df = file_df()
    m = initial_mapping(real_schema, df, profile_columns(df), None, None, use_ai=False).mapping
    assert m.get("consent").transform == "none" and m.get("last_contacted").transform == "none"
    out = convert(df, real_schema, m).output
    assert set(out["consent"]) == {""} and set(out["last_contacted"]) == {""}


def test_a_person_can_map_them(real_schema):
    """Mapped by hand, consent becomes true / false and last_contacted the formatted date."""
    out = convert(file_df(), real_schema, hand_mapped(real_schema)).output
    assert out["consent"].tolist() == ["true", "false", "true"]
    assert out["last_contacted"].tolist() == ["09/01/2026 12:00 AM"] * 3


def test_templates_never_carry_them(real_schema, tmp_path):
    """Saving leaves them out; a template that still has them (older one) doesn't fill them on the next file."""
    store = Store(tmp_path / "t.db")
    m = hand_mapped(real_schema)
    saved = store.save_template("Client", COLUMNS, m.without_manual(real_schema), {}, real_schema.name, "noah")
    assert saved.mapping.get("consent").transform == "none" and saved.mapping.get("last_contacted").transform == "none"
    assert saved.mapping.get("transactions_last").sources == ["Last Visit"]  # everything else is kept

    # A template stored with them anyway (e.g. edited by hand): exact match, CLI --template and similar layout.
    old = store.save_template("Old", COLUMNS, m, {}, real_schema.name, "noah")
    applied = template_mapping(real_schema, old, COLUMNS)
    assert applied.get("consent").transform == "none" and applied.get("last_contacted").transform == "none"
    df = file_df()
    sug = initial_mapping(real_schema, df, profile_columns(df), store, None, use_ai=False)
    assert sug.mapping.get("consent").transform == "none" and sug.mapping.get("last_contacted").transform == "none"
    similar = df.assign(Extra=["a", "b", "c"])  # 4 of 5 columns in common: the similar-template path
    sug2 = initial_mapping(real_schema, similar, profile_columns(similar), store, None, use_ai=False)
    assert sug2.similarity < 1.0 and sug2.mapping.get("consent").transform == "none"


def test_rerunning_an_import_keeps_the_persons_choice(real_schema):
    """The mapping stored with an import (a person's choice) still fills them when the import is re-run."""
    m = hand_mapped(real_schema)
    stored = Mapping.model_validate_json(m.model_dump_json())  # as saved on the import record
    assert convert(file_df(), real_schema, stored).output["consent"].tolist() == ["true", "false", "true"]


@pytest.mark.parametrize("bad", [{"default": "true"}, {"required": True}, {"fixed_blank": True}])
def test_manual_only_fields_cannot_be_filled_another_way(bad):
    """A hand-mapped-only field can't have a default, be required or also be fixed blank."""
    with pytest.raises(ValidationError):
        FieldSpec(name="consent", type="boolean", manual_only=True, **bad)


def test_unknown_consent_values_stay_blank(real_schema):
    """Values that aren't clearly yes or no leave consent blank, which changes nobody's consent."""
    df = pd.DataFrame({"Phone": ["5165550100", "9175551234", "6315550188"],
                       "Consent": ["Subscribed", "pending", "Opted Out"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "consent": FieldMapping(sources=["Consent"], transform="copy", origin="user",
                                                params={"modes": {"Consent": "both"}})})
    res = convert(df, real_schema, m)
    assert res.output["consent"].tolist() == ["true", "", "false"] and res.imported == 3
