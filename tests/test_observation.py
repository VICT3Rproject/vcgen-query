"""Observation extraction remains separate from the v3 subject predicate."""

from __future__ import annotations

import pytest

from conftest import run_query
from vcgen_query import (
    EndpointSpec,
    CohortQuery,
    parse_subject_query,
)
from vcgen_query.contract_helpers import QueryError
from vcgen_query.endpoint_rows import endpoint_matches_row


def _query(*, requested_endpoints: list[str] | None = None):
    raw: dict[str, object] = {
        "contract": "vcgen.subject_query.v1",
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
    return CohortQuery.compose(parse_subject_query(raw), requested_endpoints or ["BW_BW"])


def test_observation_contract_extracts_only_after_subject_membership_is_resolved(
    make_connection,
) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "1", "sex": "M"},
            {"study_id": "S", "subject_id": "2", "sex": "F"},
        ]
    )
    query = _query(requested_endpoints=["BW_BW"])

    result = run_query(con, query)

    assert {(row["study_id"], row["subject_id"]) for row in result.subjects.to_dict("records")} == {
        ("S", "S-1")
    }
    assert result.endpoint_names == ("BW_BW",)
    assert {record["subject_id"] for record in result.observations.to_dict("records")} == {"S-1"}


@pytest.mark.parametrize(
    ("mode", "configured", "actual", "expected"),
    [
        ("exact", ("BLOOD",), "blood", True),
        ("exact", ("BLOOD",), "SERUM", False),
        ("contains", ("BLOOD",), "whole blood", True),
        ("contains", ("BLOOD",), "SERUM", False),
        ("class", ("SERUM", "PLASMA"), "Serum", True),
        ("class", ("SERUM", "PLASMA"), "plasma", True),
        ("class", ("SERUM", "PLASMA"), "WHOLE BlOOD", False),
        ("class", ("SERUM", "PLASMA"), "no data", False),
        ("class", ("SERUM", "PLASMA"), "urine", False),
        ("class", ("SERUM", "PLASMA"), None, False),
        ("class", ("WHOLE_BLOOD",), "WHOLE BlOOD", True),
    ],
)
def test_specimen_modes_preserve_fail_closed_missing_semantics(
    mode: str,
    configured: tuple[str, ...],
    actual: str | None,
    expected: bool,
) -> None:
    # Failure modes for class rules: a WHOLE BLOOD, NO DATA, urine, or blank row
    # enters a SERUM/PLASMA endpoint; a case or spelling variant of a class
    # ("Serum", "WHOLE BlOOD") is not classified.
    endpoint = EndpointSpec(
        "HCT", "LB", ("hct",), specimens=configured, specimen_match=mode,
        specimen_panel="panel" if mode == "class" else None,
    )
    row = {"LBTESTCD": "ｈｃｔ"}
    if actual is not None:
        row["LBSPEC"] = actual

    assert endpoint_matches_row(endpoint, row, ("LBTESTCD", "LBSPEC")) is expected


def test_specimen_rule_never_reads_the_category_as_the_specimen() -> None:
    # Failure mode: with LBSPEC blank, LBCAT ("URINALYSIS", "CLINICAL CHEMISTRY")
    # was read as the specimen, so a urinalysis row passed a blood rule.
    endpoint = EndpointSpec("GLUC", "LB", ("GLUC",), specimens=("URINALYSIS",), specimen_match="exact")
    row = {"LBTESTCD": "GLUC", "LBSPEC": "", "LBCAT": "URINALYSIS"}
    assert endpoint_matches_row(endpoint, row, ("LBTESTCD", "LBSPEC", "LBCAT")) is False


def test_a_resolved_specimen_class_replaces_the_source_label() -> None:
    # Failure mode: a specimen class from a data decision does not reach matching.
    endpoint = EndpointSpec(
        "ALT", "LB", ("ALT",), specimens=("SERUM", "PLASMA"), specimen_match="class",
        specimen_panel="blood_chemistry",
    )
    row = {"LBTESTCD": "ALT", "LBSPEC": "no data"}
    columns = ("LBTESTCD", "LBSPEC")
    assert endpoint_matches_row(endpoint, row, columns) is False
    assert endpoint_matches_row(endpoint, row, columns, specimen_class="SERUM") is True
    assert endpoint_matches_row(endpoint, row, columns, specimen_class="WHOLE_BLOOD") is False


def test_class_rules_need_a_panel_and_known_classes() -> None:
    with pytest.raises(QueryError, match="specimen_panel"):
        EndpointSpec("ALT", "LB", ("ALT",), specimens=("SERUM",), specimen_match="class")
    with pytest.raises(QueryError, match="specimen class"):
        EndpointSpec("ALT", "LB", ("ALT",), specimens=("BLOOD",), specimen_match="class", specimen_panel="p")
    with pytest.raises(QueryError, match="specimen_match"):
        EndpointSpec("ALT", "LB", ("ALT",), specimens=("URINE",), specimen_match="not")


def test_normalized_selectors_and_complete_conversion_produce_supported_value(
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
    query = _query(requested_endpoints=["LB_HGB_BLOOD"])

    result = run_query(con, query)

    assert len(result.observations) == 1
    assert query.resolved_endpoints[0].applies_to_sex("ｍ")
    assert result.observations.loc[0, "numeric_value"] == pytest.approx(3.0)


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


def test_observation_keeps_records_on_every_study_day(
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
    query = _query(requested_endpoints=["LB_HCT_BLOOD"])

    result = run_query(con, query)

    assert sorted(result.observations["study_day"].tolist()) == [29, 253]


def test_required_requested_endpoint_is_always_extracted(
    make_connection,
) -> None:
    result = run_query(make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}]),
    _query(),)

    assert len(result.subjects) == 1
    assert result.endpoint_names == ("BW_BW",)


# Failure modes for implicit units: a row without a unit is silently assumed to
# have one for an endpoint that declares none; a row with a different explicit
# unit is accepted because the endpoint declares an implicit unit; a failed
# result (no value, so no unit) is dropped by the unit rule or matches another
# test code; the record loses the evidence that its unit was inferred, not read.
def test_implicit_unit_applies_only_to_rows_without_a_unit() -> None:
    endpoint = EndpointSpec(
        "LB_PH_URINE",
        "LB",
        ("PH",),
        source_units=("1{pH}",),
        target_unit="1{pH}",
        implicit_unit="1{pH}",
    )
    columns = ("LBTESTCD", "LBSTRESN", "LBSTRESU")
    assert endpoint_matches_row(endpoint, {"LBTESTCD": "PH", "LBSTRESN": 7, "LBSTRESU": ""}, columns)
    assert endpoint_matches_row(endpoint, {"LBTESTCD": "PH", "LBSTRESN": 7, "LBSTRESU": "1{pH}"}, columns)
    assert not endpoint_matches_row(
        endpoint, {"LBTESTCD": "PH", "LBSTRESN": 7, "LBSTRESU": "mmol/L"}, columns
    )
    # A failed result has no value, so its unit is unknown: the test code (and
    # specimen and method) identify the endpoint, with or without an implicit unit.
    failed = ("LBTESTCD", "LBSTRESN", "LBSTRESU", "LBSTAT")
    not_done = {"LBTESTCD": "PH", "LBSTRESN": None, "LBSTRESU": "", "LBSTAT": "NOT DONE"}
    assert endpoint_matches_row(endpoint, not_done, failed)
    plain = EndpointSpec("LB_PH", "LB", ("PH",), source_units=("1{pH}",), target_unit="1{pH}")
    assert endpoint_matches_row(plain, not_done, failed)
    assert not endpoint_matches_row(plain, {**not_done, "LBTESTCD": "GLUC"}, failed)
    assert not endpoint_matches_row(plain, {"LBTESTCD": "PH", "LBSTRESN": 7, "LBSTRESU": ""}, columns)


# Failure modes for a "not done" reason that states a limit: BELOW LINEARITY stays
# a failed test; another reason (a clotted sample) becomes censored; the censored
# record loses the reason text that tells its direction; the unit rule drops the
# record because it has no unit; a censored result value without a unit starts
# to match.
def test_not_done_below_a_limit_is_a_left_censored_result() -> None:
    from vcgen_query.send_semantics import parse_result

    columns = ("LBTESTCD", "LBSTRESN", "LBSTRESC", "LBSTRESU", "LBSTAT", "LBREASND")
    below = {"LBTESTCD": "GGT", "LBSTAT": "NOT DONE", "LBREASND": "Below linearity"}
    parsed = parse_result("LB", below, columns)
    assert (parsed.state, parsed.raw_value, parsed.value_column) == (
        "censored_observed",
        "BELOW LINEARITY",
        None,
    )
    assert parsed.numeric_value is None
    clotted = parse_result("LB", {**below, "LBREASND": "SAMPLE CLOTTED"}, columns)
    assert (clotted.state, clotted.raw_value) == ("explicitly_failed", None)

    endpoint = EndpointSpec("LB_GGT", "LB", ("GGT",), source_units=("U/L",), target_unit="U/L")
    assert endpoint_matches_row(endpoint, below, columns)
    assert not endpoint_matches_row(
        endpoint, {"LBTESTCD": "GGT", "LBSTRESC": "<5", "LBSTRESU": ""}, columns
    )


def test_implicit_unit_is_recorded_as_inferred_on_the_observation(make_connection) -> None:
    from vcgen_query.observation import extract_observations
    from vcgen_query.set_execution import selection_for_keys

    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "M"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "PH", "test_name": "pH",
             "specimen": "urine", "unit": "", "day": 1, "value": 6.5},
        ],
    )
    endpoint = EndpointSpec(
        "LB_PH_URINE", "LB", ("PH",), source_units=("1{pH}",), specimens=("URINE",),
        target_unit="1{pH}", implicit_unit="1{pH}",
    )
    keys = [("S", "S-1")]
    observations = extract_observations(con, selection_for_keys(con, keys, universe_keys=keys), [endpoint])

    (record,) = observations.frame.to_dict("records")
    assert (record["unit"], record["unit_inferred"], record["numeric_value"]) == ("1{pH}", True, 6.5)
    assert record["unit_inferred"] is True
