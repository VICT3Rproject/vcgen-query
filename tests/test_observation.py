"""Observation extraction remains separate from the v3 subject predicate."""

from __future__ import annotations

import pytest

from vcgen_query import EndpointSpec, execute_query, parse_query, to_daily_frame
from vcgen_query.endpoint_rows import endpoint_matches_row


def _query(*, endpoints: list[dict[str, object]] | None = None):
    raw: dict[str, object] = {
        "contract": "vcgen.cohort_query.v4",
        "query_id": "synthetic-observation",
        "predicate": {
            "field": {
                "kind": "send_column",
                "domain": "DM",
                "column": "SEX",
                "value_type": "string",
            },
            "operator": "eq",
            "value": "M",
        },
    }
    if endpoints is not None:
        raw["observation_contract"] = {"endpoints": endpoints}
    return parse_query(raw)


def test_observation_contract_extracts_only_after_subject_membership_is_resolved(
    make_connection,
) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "1", "sex": "M"},
            {"study_id": "S", "subject_id": "2", "sex": "F"},
        ]
    )
    query = _query(endpoints=[{"name": "BW", "domain": "BW", "test_codes": ["BW"]}])

    result = execute_query(con, query)

    assert {(row["study_id"], row["subject_id"]) for row in result.subjects.to_dict("records")} == {
        ("S", "S-1")
    }
    assert result.endpoint_names == ("BW",)
    assert {record["subject_id"] for record in result.observations.to_dict("records")} == {"S-1"}


@pytest.mark.parametrize(
    ("mode", "configured", "actual", "expected"),
    [
        ("exact", ("BLOOD",), "blood", True),
        ("exact", ("BLOOD",), "SERUM", False),
        ("contains", ("BLOOD",), "whole blood", True),
        ("contains", ("BLOOD",), "SERUM", False),
        ("not", ("URINE",), "blood", True),
        ("not", ("URINE",), "urine", False),
        ("not", ("URINE",), None, False),
    ],
)
def test_specimen_modes_preserve_fail_closed_missing_semantics(
    mode: str,
    configured: tuple[str, ...],
    actual: str | None,
    expected: bool,
) -> None:
    endpoint = EndpointSpec("HCT", "LB", ("hct",), specimens=configured, specimen_match=mode)
    row = {"LBTESTCD": "ｈｃｔ"}
    if actual is not None:
        row["LBSPEC"] = actual

    assert endpoint_matches_row(endpoint, row, ("LBTESTCD", "LBSPEC")) is expected


def test_normalized_selectors_and_complete_conversion_produce_supported_daily_value(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "M"}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "ｈｇｂ",
                "test_name": "hemoglobin",
                "specimen": "blood",
                "unit": "g/L",
                "day": 1,
                "value": 3,
            }
        ],
    )
    query = _query(
        endpoints=[
            {
                "name": "LB_HGB",
                "domain": "LB",
                "test_codes": ["hgb"],
                "test_names": ["HEMOGLOBIN"],
                "source_units": ["g/L"],
                "specimens": ["BLOOD"],
                "sexes": ["m"],
                "day_window": {"min": 1, "max": 1},
                "target_unit": "g/dL",
            }
        ]
    )

    result = execute_query(con, query)
    daily = to_daily_frame(result)

    assert len(result.observations) == 1
    assert query.observation_contract.endpoints[0].applies_to_sex("ｍ")
    assert daily.values.loc[0, "LB_HGB"] == pytest.approx(0.3)
    assert daily.receipt["unsupported_unit_count"] == 0


def test_source_unit_filter_is_exact_and_case_sensitive(make_connection) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "M"}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "MASS",
                "unit": unit,
                "day": index,
                "value": index,
            }
            for index, unit in enumerate(("g", "G"), start=1)
        ],
    )
    query = _query(
        endpoints=[
            {
                "name": "MASS",
                "domain": "LB",
                "test_codes": ["MASS"],
                "source_units": ["g"],
                "target_unit": "g",
            }
        ]
    )

    result = execute_query(con, query)

    assert result.observations["unit"].tolist() == ["g"]


def test_observation_day_window_excludes_records_above_declared_maximum(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "M"}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HCT",
                "day": 29,
                "value": 0.48,
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HCT",
                "day": 253,
                "value": 0.49,
            },
        ],
    )
    query = _query(
        endpoints=[
            {
                "name": "LB_HCT",
                "domain": "LB",
                "test_codes": ["HCT"],
                "day_window": {"max": 35},
            }
        ]
    )

    result = execute_query(con, query)

    assert result.observations["study_day"].tolist() == [29]


def test_predicate_without_observation_contract_has_an_empty_observation_output(
    make_connection,
) -> None:
    result = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}]),
        _query(),
    )

    assert len(result.subjects) == 1
    assert result.observations.empty
