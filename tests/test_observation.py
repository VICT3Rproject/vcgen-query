"""Observation extraction remains separate from the v3 subject predicate."""

from __future__ import annotations

import pytest

from vcgen_query import (
    EndpointSpec,
    endpoint_catalogue_identity,
    execute_query,
    parse_query,
    to_daily_frame,
)
from vcgen_query.endpoint_rows import endpoint_matches_row


def _query(*, requested_endpoints: list[str] | None = None):
    raw: dict[str, object] = {
        "contract": "vcgen.cohort_query.v5",
        "query_id": "synthetic-observation",
        "requested_endpoints": requested_endpoints or ["BW"],
        "endpoint_catalogue": endpoint_catalogue_identity(),
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
    query = _query(requested_endpoints=["BW"])

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
                "unit": "mmol/L",
                "day": 1,
                "value": 3,
            }
        ],
    )
    query = _query(requested_endpoints=["LB_HGB"])

    result = execute_query(con, query)
    daily = to_daily_frame(result)

    assert len(result.observations) == 1
    assert query.resolved_endpoints[0].applies_to_sex("ｍ")
    assert daily.values.loc[0, "LB_HGB"] == pytest.approx(3.0)
    assert daily.receipt["unsupported_unit_count"] == 0


def test_source_unit_filter_matches_unit_identity_not_label_casing() -> None:
    endpoint = EndpointSpec("MASS", "LB", ("MASS",), source_units=("g",), target_unit="g")
    columns = ("LBTESTCD", "LBSTRESN", "LBSTRESU")
    assert endpoint_matches_row(
        endpoint, {"LBTESTCD": "MASS", "LBSTRESN": 1, "LBSTRESU": "g"}, columns
    )
    # Sponsors spell the same unit with inconsistent casing.
    assert endpoint_matches_row(
        endpoint, {"LBTESTCD": "MASS", "LBSTRESN": 1, "LBSTRESU": "G"}, columns
    )
    # A different unit (different dimension scale) must still be rejected.
    assert not endpoint_matches_row(
        endpoint, {"LBTESTCD": "MASS", "LBSTRESN": 1, "LBSTRESU": "MG"}, columns
    )
    # A missing unit can never be assumed to match.
    assert not endpoint_matches_row(
        endpoint, {"LBTESTCD": "MASS", "LBSTRESN": 1, "LBSTRESU": ""}, columns
    )


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
                "unit": "%",
                "specimen": "BLOOD",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HCT",
                "day": 253,
                "value": 0.49,
                "unit": "%",
                "specimen": "BLOOD",
            },
        ],
    )
    query = _query(requested_endpoints=["LB_HCT"])

    result = execute_query(con, query)

    assert sorted(result.observations["study_day"].tolist()) == [29, 253]


def test_required_requested_endpoint_is_always_extracted(
    make_connection,
) -> None:
    result = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}]),
        _query(),
    )

    assert len(result.subjects) == 1
    assert result.endpoint_names == ("BW",)
