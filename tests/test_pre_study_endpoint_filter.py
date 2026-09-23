"""v3 continuity tests for strict pre-study endpoint predicates."""

from __future__ import annotations

from vcgen_query import endpoint_catalogue_identity, execute_query, parse_query


def _query(predicate: dict[str, object]):
    return parse_query(
        {
            "contract": "vcgen.cohort_query.v5",
            "query_id": "synthetic-pre-study",
            "requested_endpoints": ["BW"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
            "predicate": predicate,
        }
    )


def _members(result) -> set[tuple[str, str]]:
    return {(row["study_id"], row["subject_id"]) for row in result.subjects.to_dict("records")}


def _endpoint(operator: str, value: object) -> dict[str, object]:
    return {
        "field": {
            "kind": "pre_study_endpoint",
            "endpoint": "LB_HGB",
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
                "test_code": "HGB",
                "day": 0,
                "value": 6,
                "unit": "mmol/L",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB",
                "day": 1,
                "value": 8,
                "unit": "mmol/L",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB",
                "day": 2,
                "value": 20,
                "unit": "mmol/L",
            },
        ],
    )

    assert _members(execute_query(con, _query(_endpoint("gte", 8)))) == {("S", "S-1")}
    assert _members(execute_query(con, _query(_endpoint("gt", 8)))) == set()


def test_pre_study_endpoint_ties_and_missing_values_fail_even_negative_rules(
    make_connection,
) -> None:
    tied = make_connection(
        [{"study_id": "TIE", "subject_id": "1", "rfstdtc": "2020-01-01T12:00:00"}],
        lab=[
            {
                "study_id": "TIE",
                "subject_id": "1",
                "test_code": "HGB",
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

    assert _members(execute_query(tied, _query(_endpoint("not_between", [7, 15])))) == set()
    assert _members(execute_query(missing, _query(_endpoint("not_between", [7, 15])))) == set()


def test_pre_study_endpoint_rules_are_independent_subject_sets(make_connection) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "HGB",
                "day": 0,
                "value": 8,
                "unit": "mmol/L",
                "specimen": "BLOOD",
            },
            {
                "study_id": "S",
                "subject_id": "1",
                "test_code": "WBC",
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
                    "endpoint": "LB_WBC",
                    "value_type": "number",
                },
                "operator": "lte",
                "value": 15,
            },
        ],
    }

    assert _members(execute_query(con, _query(predicate))) == {("S", "S-1")}
