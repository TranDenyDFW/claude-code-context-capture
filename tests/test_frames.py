"""c4x.frames: a missing value is None on any pandas, and the JSON the CLI prints is JSON.

These do not need a store. The defect they pin was found on CI and not locally because the two
ran different pandas majors, so each case builds its own frame with an explicit missing value
rather than trusting whichever representation the installed pandas happens to use.
"""
import json
import math

import pandas as pd
import pytest

from c4x.frames import has_nan, jsonable, records


def strict_loads(text):
    """json.loads that REJECTS NaN and Infinity, the way JSON.parse and jq do. Python's default
    parser accepts them, which is exactly how the literal NaN reached the CLI's output unnoticed."""
    def refuse(constant):
        raise ValueError(f"non-JSON constant in output: {constant}")
    return json.loads(text, parse_constant=refuse)


def test_records_turns_a_missing_text_cell_into_none():
    df = pd.DataFrame({"tool": ["Read", "Bash"], "target": ["x.txt", None]})
    rows = records(df)
    assert rows[0]["target"] == "x.txt"
    assert rows[1]["target"] is None, f"missing text cell came back as {rows[1]['target']!r}"


def test_records_turns_a_missing_number_into_none_not_nan():
    df = pd.DataFrame({"n": [1.5, float("nan")]})
    rows = records(df)
    assert rows[0]["n"] == 1.5
    assert rows[1]["n"] is None


def test_records_on_an_empty_frame_is_an_empty_list():
    assert records(pd.DataFrame({"a": []})) == []


def test_records_passes_a_plain_list_through():
    assert records([{"a": 1}]) == [{"a": 1}]


def test_raw_to_dict_is_the_thing_being_fixed():
    """Documents WHY the helper exists: on the installed pandas, a raw to_dict of a missing text
    cell is either None or NaN. Either way the helper must give None. If this pandas gives None
    natively the helper is a no-op here and the CI pandas is where it earns its keep."""
    df = pd.DataFrame({"target": ["x", None]})
    raw = df.to_dict("records")[1]["target"]
    assert raw is None or (isinstance(raw, float) and math.isnan(raw))
    assert records(df)[1]["target"] is None


def test_jsonable_writes_null_for_nan_and_strict_parse_accepts_it():
    payload = {"rows": [{"n": float("nan"), "t": None}], "extent": float("nan")}
    text = json.dumps(jsonable(payload))
    assert "NaN" not in text
    parsed = strict_loads(text)
    assert parsed["rows"][0]["n"] is None and parsed["extent"] is None


def test_the_cli_json_is_strictly_parseable_even_with_nan_inside():
    from c4x.cli.render import as_json
    payload = {"tab": "t", "session": None, "scope": "main", "cohort": None,
               "tables": [{"id": "x", "columns": ["target"], "rows": [{"target": float("nan")}],
                           "tooltips": {}, "sorts": False}],
               "figures": [], "text": []}
    text = as_json(payload)
    assert "NaN" not in text, "the CLI wrote the literal NaN, which is not JSON"
    strict_loads(text)


def test_has_nan_finds_a_nested_nan_and_nothing_else():
    assert has_nan({"a": [1, {"b": float("nan")}]})
    assert not has_nan({"a": [1, {"b": None, "c": "nan"}]})
    assert not has_nan(2.5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_strict_loads_really_refuses_non_json_constants(bad):
    """The negative control for strict_loads itself: a checker that cannot fail proves nothing."""
    with pytest.raises(ValueError):
        strict_loads(json.dumps({"x": bad}))
