"""v3 continuity tests for the established derived baseline selector."""

from __future__ import annotations

from conftest import members, run_query
from vcgen_query import CohortQuery, parse_subject_query


def _query(predicate: dict[str, object]):
    subject = parse_subject_query(
        {"contract": "vcgen.subject_query.v1", "query_id": "synthetic-derived", "predicate": predicate}
    )
    return CohortQuery.compose(subject, ["BW_BW"])


def test_derived_baseline_body_weight_reuses_latest_qualifying_pre_study_value(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "exstdy": 2}],
        body_weight=[
            {"study_id": "S", "subject_id": "1", "day": 0, "value": 240},
            {"study_id": "S", "subject_id": "1", "day": 1, "value": 260},
            {"study_id": "S", "subject_id": "1", "day": 2, "value": 999},
        ],
    )
    predicate = {
        "field": {
            "kind": "derived",
            "name": "baseline_body_weight_g",
            "value_type": "number",
        },
        "operator": "gte",
        "value": 260,
    }

    assert members(run_query(con, _query(predicate))) == {("S", "S-1")}


def test_derived_baseline_body_weight_missing_value_does_not_match_negative_predicate(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        body_weight=[],
    )
    predicate = {
        "field": {
            "kind": "derived",
            "name": "baseline_body_weight_g",
            "value_type": "number",
        },
        "operator": "not_between",
        "value": [200, 300],
    }

    assert members(run_query(con, _query(predicate))) == set()
