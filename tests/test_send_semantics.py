"""Focused SEND-semantics parsing tests (defends the dependency-free port)."""

from __future__ import annotations

from vcgen_query.send_semantics import (
    baseline_flag_columns,
    day_columns,
    endpoint_key,
    is_explicit_baseline,
    measurement_identity,
    normalize_flag,
    normalize_method,
    normalize_unit,
    parse_result,
    value_columns,
)
from vcgen_query.send_semantics import (
    test_columns as send_test_columns,
)

BW_COLUMNS = (
    "STUDYID",
    "USUBJID",
    "BWTESTCD",
    "BWTEST",
    "BWDY",
    "BWSTRESN",
    "BWSTRESU",
    "BWORRES",
    "BWORRESU",
)


def test_column_priority_helpers():
    assert send_test_columns(("LBTESTCD", "LBTEST", "X")) == ("LBTESTCD", "LBTEST")
    assert day_columns("BW", BW_COLUMNS)[:1] == ("BWDY",)
    assert value_columns("BW", BW_COLUMNS)[:1] == ("BWSTRESN",)


def test_explicit_baseline_flag_is_projectable_without_host_row_parsing():
    columns = (*BW_COLUMNS, "BWBLFL")
    assert baseline_flag_columns("BW", columns) == ("BWBLFL",)
    assert is_explicit_baseline({"BWBLFL": "yes"}, ("BWBLFL",))
    assert not is_explicit_baseline({"BWBLFL": "N"}, ("BWBLFL",))


def test_parse_result_numeric_pairs_value_and_unit():
    parsed = parse_result(
        "BW",
        {"BWSTRESN": "250.5", "BWSTRESU": "g"},
        BW_COLUMNS,
    )
    assert parsed.state == "numeric_observed"
    assert parsed.numeric_value == 250.5
    assert parsed.unit == "g"
    assert parsed.value_column == "BWSTRESN"


def test_parse_result_censored_and_failed_states():
    censored = parse_result("LB", {"LBORRES": "<0.1", "LBORRESU": "MG/DL"}, ("LBORRES", "LBORRESU"))
    assert censored.state == "censored_observed"
    assert censored.numeric_value is None
    failed = parse_result("LB", {"LBSTAT": "NOT DONE"}, ("LBSTAT",))
    assert failed.state == "explicitly_failed"
    absent = parse_result("LB", {}, ("LBORRES",))
    assert absent.state == "silently_absent"


def test_parse_result_excluded_flag_blocks_numeric_usability():
    parsed = parse_result(
        "BW",
        {"BWSTRESN": "250", "BWSTRESU": "G", "BWEXCLFL": "Y"},
        (*BW_COLUMNS, "BWEXCLFL"),
    )
    assert parsed.numeric_value == 250.0
    assert parsed.excluded is True


def test_measurement_identity_and_endpoint_key():
    row = {"LBTESTCD": "HCT", "LBSPEC": "BLOOD", "LBSTRESU": "L/L"}
    identity = measurement_identity("LB", row, ("LBTESTCD", "LBSPEC", "LBSTRESU"))
    assert identity == "domain=LB|test_code=HCT|specimen=BLOOD|unit=L/L|method=UNKNOWN"
    key = endpoint_key("LB", {"LBTESTCD": "ALT"}, ("LBTESTCD",), ())
    assert key == "LB:ALT"


def test_normalizers():
    assert normalize_unit(" µg ") == "µg"
    assert normalize_unit("g / L") == "g / L"
    assert normalize_unit("g") != normalize_unit("G")
    assert normalize_unit("s") != normalize_unit("S")
    assert normalize_unit("10^12/L") == "10^12/L"
    assert normalize_unit("") is None
    assert normalize_flag("yes") == "Y"
    assert normalize_flag("0") == "N"
    assert normalize_method("colourimetric") == "COLORIMETRIC"
