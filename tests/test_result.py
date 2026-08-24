"""Public v4 execution receipts and explainability tests."""

from __future__ import annotations


import pytest

from vcgen_query import (
    EndpointFacts,
    EndpointSpec,
    QueryError,
    compare_membership,
    execute_query,
    explain_query,
    fingerprint_predicate,
    parse_query,
)
from vcgen_query.set_execution import NodeExplanation, membership_hash
from vcgen_query.result import _query_domains


def _query():
    return parse_query(
        {
            "contract": "vcgen.cohort_query.v4",
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
    }.issubset(result.receipt)
    assert result.receipt["contract"] == "vcgen.cohort_query.v4"
    assert result.receipt["predicate_contract"] == "vcgen.subject_predicate.v1"
    assert result.receipt["predicate_fingerprint"] == fingerprint_predicate(query.predicate)
    assert result.receipt["import_provenance"] is None
    assert result.receipt["membership_hash"]
    assert result.receipt["warnings"] == list(result.warnings)
    assert result.receipt["source_identity"] is None
    assert result.receipt["unit_catalogue"]["catalogue_id"] == "vcgen.unit_catalogue.v1"
    assert any("source identity is unknown" in warning for warning in result.warnings)
    comparison = compare_membership(result.subjects, result.subjects)
    assert comparison["local_hash"] == result.receipt["membership_hash"]


def test_query_result_types_and_empty_dataframe_schemas(make_connection) -> None:
    result = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "F"}]),
        _query(),
    )

    assert result.subjects.columns.tolist() == [
        "sample_id",
        "study_id",
        "subject_id",
        "species",
        "strain",
        "sex",
        "study_year",
        "study_length_days",
        "suppliers",
        "study_types",
        "routes",
        "dose_frequencies",
        "treatment_vehicles",
        "test_facilities",
        "glp_statuses",
        "baseline_body_weight_g",
        "age_days",
        "first_exposure_day",
    ]
    assert len(result.observations.columns) == 29




def test_direct_node_explanation_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported explanation node kind"):
        NodeExplanation("predicate", "unknown", 0)


def test_endpoint_facts_derive_consistent_conversion_properties() -> None:
    endpoint = EndpointSpec(
        "LB_HGB", "LB", ("HGB",), source_units=("g/L",), target_unit="g/dL"
    )
    facts = EndpointFacts(
        "LB_HGB", "LB", ("m",), endpoint.target_unit, endpoint.compiled_unit_rules
    )

    assert facts.sex_applicability == ("M",)
    assert facts.target_unit == "g/dL"
    assert facts.compiled_unit_rules == endpoint.compiled_unit_rules
    metadata = facts.metadata()
    assert metadata["conversion_rules"][0]["source_ucum"] == "g/L"
    assert metadata["conversion_rules"][0]["scale"] == pytest.approx(0.1)
    assert metadata["unit_catalogue"]["catalogue_id"] == "vcgen.unit_catalogue.v1"
    with pytest.raises(QueryError, match="target_unit"):
        EndpointFacts("LB_HGB", "LB", target_unit={})


def test_query_domains_include_predicate_observation_and_baseline_inputs() -> None:
    query = parse_query(
        {
            "contract": "vcgen.cohort_query.v4",
            "query_id": "domain-plan",
            "predicate": {
                "group": "and",
                "children": [
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "LB",
                            "column": "LBTESTCD",
                            "value_type": "string",
                        },
                        "operator": "eq",
                        "value": "ALT",
                    },
                    {
                        "field": {
                            "kind": "derived",
                            "name": "baseline_body_weight_g",
                            "value_type": "number",
                        },
                        "operator": "gte",
                        "value": 200,
                    },
                ],
            },
            "observation_contract": {
                "endpoints": [{"name": "LIVER", "domain": "OM", "test_codes": ["LIVER"]}]
            },
        }
    )

    assert _query_domains(query) == {"BW", "LB", "OM"}


def test_membership_hash_uses_unambiguous_structured_framing() -> None:
    assert membership_hash([("A", "B\nC\0D")]) != membership_hash([("A", "B"), ("C", "D")])


def test_dm_subject_universe_rejects_blank_subject_keys(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("UPDATE dm SET USUBJID = ' '")
    with pytest.raises(ValueError, match="DM subject universe contains"):
        execute_query(con, _query())


def test_explain_query_reports_deterministic_subject_counts_without_changing_membership(
    make_connection,
) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "1", "sex": "M"},
            {"study_id": "S", "subject_id": "2", "sex": "F"},
        ]
    )

    explanation = explain_query(con, _query())

    assert explanation["matched_subject_count"] == 1
    assert explanation["candidate_subject_count"] == 2
    assert explanation["predicate_contract"] == "vcgen.subject_predicate.v1"
    assert explanation["predicate_fingerprint"] == fingerprint_predicate(_query().predicate)
    assert explanation["import_provenance"] is None


def test_explain_query_reports_source_row_and_subject_set_scopes(make_connection) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "AST", "value": 2.0}
        ],
    )
    query = parse_query(
        {
            "contract": "vcgen.cohort_query.v4",
            "query_id": "row-scope-explanation",
            "predicate": {
                "group": "and",
                "children": [
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "LB",
                            "column": "LBTESTCD",
                            "value_type": "string",
                        },
                        "operator": "eq",
                        "value": "AST",
                    },
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "LB",
                            "column": "LBSTRESN",
                            "value_type": "number",
                        },
                        "operator": "gt",
                        "value": 1.8,
                    },
                ],
            },
        }
    )

    explanation = explain_query(con, query)

    row_node = next(node for node in explanation["nodes"] if node.get("scope") == "source_row")
    assert row_node["domain"] == "LB"
    assert row_node["count"] == 1
    assert row_node["rule_paths"] == [
        "predicate.children[0]",
        "predicate.children[1]",
    ]
    assert explanation["nodes"][-1]["scope"] == "subject_set"
    assert explanation["nodes"][-1]["row_scoped_domains"] == [
        {"domain": "LB", "rule_count": 2}
    ]
    assert any("same domain and group" in item for item in explanation["assumptions"])
