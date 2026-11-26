"""Public v6 execution receipts and explainability tests."""

from __future__ import annotations


from vcgen_query import (
    CohortQuery,
    compare_membership,
    execute_query,
    fingerprint_predicate,
    parse_subject_query,
)
from vcgen_query.set_execution import membership_hash


def _query():
    subject = parse_subject_query(
        {
            "contract": "vcgen.subject_query.v1",
            "query_id": "synthetic-receipt",
            "predicate": {
                "group": "and",
                "children": [
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "DM",
                            "column": "SEX",
                            "value_type": "string",
                        },
                        "operator": "eq",
                        "value": "M",
                    }
                ],
            },
        }
    )
    return CohortQuery.compose(subject, ["BW"])


def test_execute_query_returns_a_provenance_complete_membership_receipt(
    make_connection,
) -> None:
    query = _query()
    result = execute_query(
        make_connection(
            [
                {"study_id": "S", "subject_id": "1", "sex": "M"},
                {"study_id": "S", "subject_id": "2", "sex": "F"},
            ]
        ),
        query,
    )

    assert {
        "predicate_contract",
        "predicate_fingerprint",
        "observation_fingerprint",
        "import_provenance",
        "source_identity",
        "assumptions",
        "warnings",
        "membership_hash",
        "unit_catalogue",
        "unit_semantics",
        "dose_duration_evidence",
    }.issubset(result.receipt)
    assert result.receipt["contract"] == "vcgen.cohort_query.v7"
    assert result.receipt["predicate_contract"] == "vcgen.subject_predicate.v1"
    assert result.receipt["predicate_fingerprint"] == fingerprint_predicate(query.subject_query.predicate)
    assert result.receipt["import_provenance"] is None
    assert result.receipt["membership_hash"]
    assert result.receipt["warnings"] == list(result.warnings)
    assert result.receipt["source_identity"] is None
    assert result.receipt["unit_catalogue"]["catalogue_id"] == "vcgen.unit_catalogue.v2"
    assert result.receipt["dose_duration_evidence"]["status_counts"] == {
        "declared_only": 0,
        "corroborated": 1,
        "shorter_than_planned": 0,
        "ambiguous": 0,
        "unresolved": 0,
    }
    assert result.receipt["dose_duration_evidence"][
        "exposure_interval_status_counts"
    ] == {
        "complete_contiguous": 1,
        "gapped": 0,
        "incomplete": 0,
        "unavailable": 0,
    }
    assert any("source identity is unknown" in warning for warning in result.warnings)
    comparison = compare_membership(result.subjects, result.subjects)
    assert comparison["local_hash"] == result.receipt["membership_hash"]
    assert len(result.subjects) == result.metadata["subject_count"]
    assert len(result.subjects) == result.receipt["subject_count"]
    assert not result.subjects.duplicated(["study_id", "subject_id"]).any()
    assert result.membership is not None
    assert result.membership.subject_keys == (("S", "S-1"),)
    assert result.membership.universe_keys == (("S", "S-1"), ("S", "S-2"))
    assert result.membership.membership_hash == result.receipt["membership_hash"]


def test_membership_hash_uses_unambiguous_structured_framing() -> None:
    assert membership_hash([("A", "B\nC\0D")]) != membership_hash([("A", "B"), ("C", "D")])
