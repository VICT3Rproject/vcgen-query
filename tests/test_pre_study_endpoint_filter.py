"""v3 continuity tests for strict pre-study endpoint predicates."""

from __future__ import annotations

from conftest import members, run_query
from vcgen_query import CohortQuery, parse_subject_query


def _query(predicate: dict[str, object]):
    subject = parse_subject_query(
        {"contract": "vcgen.subject_query.v1", "query_id": "synthetic-pre-study", "predicate": predicate}
    )
    return CohortQuery.compose(subject, ["BW_BW"])


def _endpoint(operator: str, value: object) -> dict[str, object]:
    return {
        "field": {
            "kind": "pre_study_endpoint",
            "endpoint": "LB_HGB_BLOOD",
            "value_type": "number",
        },
        "operator": operator,
        "value": value,
    }


def test_pre_study_endpoint_uses_latest_value_strictly_before_exposure(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "exstdy": 2}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB", "test_name": "HEMOGLOBIN",
                "day": 0,
                "value": 6,
                "unit": "mmol/L",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB", "test_name": "HEMOGLOBIN",
                "day": 1,
                "value": 8,
                "unit": "mmol/L",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB", "test_name": "HEMOGLOBIN",
                "day": 2,
                "value": 20,
                "unit": "mmol/L",
            },
        ],
    )

    assert members(run_query(con, _query(_endpoint("gte", 8)))) == {("S", "S-1")}
    assert members(run_query(con, _query(_endpoint("gt", 8)))) == set()


def test_pre_study_endpoint_ties_and_missing_values_fail_even_negative_rules(
    make_connection,
) -> None:
    tied = make_connection(
        [{"study_id": "TIE", "subject_id": "1", "rfstdtc": "2020-01-01T12:00:00"}],
        lab=[
            {
                "study_id": "TIE",
                "subject_id": "1",
                "test_code": "HGB", "test_name": "HEMOGLOBIN",
                "day": None,
                "datetime": "2020-01-01T12:00:00",
                "value": 20,
                "unit": "mmol/L",
            },
        ],
    )
    missing = make_connection(
        [{"study_id": "MISSING", "subject_id": "1"}],
        lab=[],
    )

    assert members(run_query(tied, _query(_endpoint("not_between", [7, 15])))) == set()
    assert members(run_query(missing, _query(_endpoint("not_between", [7, 15])))) == set()


def test_pre_study_endpoint_rules_are_independent_subject_sets(make_connection) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB", "test_name": "HEMOGLOBIN",
                "day": 0,
                "value": 8,
                "unit": "mmol/L",
                "specimen": "BLOOD",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "WBC", "test_name": "LEUKOCYTES",
                "day": 0,
                "value": 12,
                "unit": "10^9/L",
                "specimen": "BLOOD",
            },
        ],
    )
    predicate = {
        "group": "and",
        "children": [
            _endpoint("gte", 7),
            {
                "field": {
                    "kind": "pre_study_endpoint",
                    "endpoint": "LB_WBC_BLOOD",
                    "value_type": "number",
                },
                "operator": "lte",
                "value": 15,
            },
        ],
    }

    assert members(run_query(con, _query(predicate))) == {("S", "S-1")}


# Failure modes: repeated bounds change subject-set semantics; resolved values
# leak into a later execution against another source with the same subject keys.
def test_repeated_endpoint_bounds_keep_source_values_local_to_execution(make_connection):
    query = _query({"group": "and", "children": [_endpoint("gte", 7), _endpoint("lte", 9)]})
    results = []
    for value in (8, 12):
        con = make_connection(
            [{"study_id": "S", "subject_id": "1"}],
            lab=[{"study_id": "S", "subject_id": "1", "test_code": "HGB", "test_name": "HEMOGLOBIN",
                  "day": 0, "value": value, "unit": "mmol/L", "specimen": "BLOOD"}],
        )
        results.append(members(run_query(con, query)))
    assert results == [{("S", "S-1")}, set()]


# Failure modes before reducing baseline scans: timestamp evidence overrides a
# valid study day; exposure-day rows enter the baseline; missing-day timestamp
# rows are lost; bulk subject-key loading changes independent predicate sets.
def test_pre_study_day_priority_and_timestamp_fallback(make_connection):
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "F", "exstdy": 2,
          "rfstdtc": "2020-01-01T12:00:00"},
         {"study_id": "S", "subject_id": "2", "sex": "M", "exstdy": 2,
          "rfstdtc": "2020-01-01T12:00:00"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "HGB", "test_name": "HEMOGLOBIN", "day": 1,
             "datetime": "2020-01-02T12:00:00", "value": 8, "unit": "mmol/L"},
            {"study_id": "S", "subject_id": "1", "test_code": "HGB", "test_name": "HEMOGLOBIN", "day": 2,
             "datetime": "2019-12-31T12:00:00", "value": 100, "unit": "mmol/L"},
            {"study_id": "S", "subject_id": "1", "test_code": "HGB", "test_name": "HEMOGLOBIN", "day": None,
             "datetime": "2020-01-01T11:00:00", "value": 7, "unit": "mmol/L"},
            {"study_id": "S", "subject_id": "2", "test_code": "HGB", "test_name": "HEMOGLOBIN", "day": None,
             "datetime": "2020-01-01T11:00:00", "value": 9, "unit": "mmol/L"},
            {"study_id": "S", "subject_id": "2", "test_code": "HGB", "test_name": "HEMOGLOBIN", "day": None,
             "datetime": "2020-01-01T12:00:00", "value": 99, "unit": "mmol/L"},
        ],
    )
    assert members(run_query(con, _query(_endpoint("eq", 8)))) == {("S", "S-1")}
    assert members(run_query(con, _query(_endpoint("eq", 9)))) == {("S", "S-2")}
