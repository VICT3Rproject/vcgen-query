"""Focused SEND-semantics parsing tests (defends the dependency-free port)."""

from __future__ import annotations

from vcgen_query.send_semantics import (
    parse_result,
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


def test_parse_result_adopts_original_unit_on_identity_conversion():
    # Curated SEND rows frequently leave the standardized unit empty when the
    # original unit already carries the standard scale; the unit label must
    # survive provenance resolution.
    parsed = parse_result(
        "OM",
        {
            "OMSTRESN": "7.8066",
            "OMSTRESU": "",
            "OMORRES": "7.8066",
            "OMORRESU": "g",
        },
        ("OMSTRESN", "OMSTRESU", "OMORRES", "OMORRESU"),
    )
    assert parsed.state == "numeric_observed"
    assert parsed.numeric_value == 7.8066
    assert parsed.value_column == "OMSTRESN"
    assert parsed.unit == "g"
    assert parsed.unit_column == "OMORRESU"


def test_parse_result_does_not_adopt_unit_across_converted_values():
    # A standardized value that differs from the original proves a unit
    # conversion; the original unit label must NOT be applied to it.
    parsed = parse_result(
        "OM",
        {
            "OMSTRESN": "7.8066",
            "OMSTRESU": "",
            "OMORRES": "7806.6",
            "OMORRESU": "MG",
        },
        ("OMSTRESN", "OMSTRESU", "OMORRES", "OMORRESU"),
    )
    assert parsed.state == "numeric_observed"
    assert parsed.numeric_value == 7.8066
    assert parsed.unit is None
