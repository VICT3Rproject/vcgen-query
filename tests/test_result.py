"""Public v5 execution receipts and explainability tests."""

from __future__ import annotations


import pytest

from vcgen_query import (
    EndpointFacts,
    EndpointSpec,
    endpoint_catalogue_identity,
    QueryError,
    SourceDataError,
    compare_membership,
    execute_query,
    explain_query,
    fingerprint_predicate,
    parse_query,
    query_result_schema,
)
from vcgen_query.set_execution import (
    GroupExecutionPlan,
    NodeExplanation,
    RuleExecutionPlan,
    membership_hash,
    resolve_execution_plan,
)
from vcgen_query.result import _query_domains


def _query():
    return parse_query(
        {
            "contract": "vcgen.cohort_query.v5",
            "query_id": "synthetic-receipt",
            "requested_endpoints": ["BW"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
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
        "dose_duration_evidence",
    }.issubset(result.receipt)
    assert result.receipt["contract"] == "vcgen.cohort_query.v5"
    assert result.receipt["predicate_contract"] == "vcgen.subject_predicate.v1"
    assert result.receipt["predicate_fingerprint"] == fingerprint_predicate(query.predicate)
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


def test_query_result_types_and_empty_dataframe_schemas(make_connection) -> None:
    result = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "F"}]),
        _query(),
    )

    subject_schema = query_result_schema("subjects")
    observation_schema = query_result_schema("observations")
    assert result.subjects.columns.tolist() == [column.name for column in subject_schema.columns]
    assert result.observations.columns.tolist() == [
        column.name for column in observation_schema.columns
    ]
    assert result.subjects.dtypes.astype(str).to_dict() == {
        column.name: column.pandas_dtype for column in subject_schema.columns
    }
    assert result.observations.dtypes.astype(str).to_dict() == {
        column.name: column.pandas_dtype for column in observation_schema.columns
    }


def test_query_result_uses_same_dtypes_for_populated_and_empty_tables(make_connection) -> None:
    populated = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}]),
        _query(),
    )
    empty = execute_query(
        make_connection([{"study_id": "S", "subject_id": "1", "sex": "F"}]),
        _query(),
    )

    assert not populated.subjects.empty
    assert not populated.observations.empty
    assert empty.subjects.empty
    assert empty.observations.empty
    assert populated.subjects.dtypes.astype(str).to_dict() == empty.subjects.dtypes.astype(
        str
    ).to_dict()
    assert populated.observations.dtypes.astype(
        str
    ).to_dict() == empty.observations.dtypes.astype(str).to_dict()
    assert populated.observations.loc[0, "pool_id"] is None
    assert populated.observations.loc[0, "study_day"] == 1


def test_public_query_table_dictionary_describes_grain_keys_nulls_and_nested_values() -> None:
    subjects, observations = query_result_schema()

    assert subjects.grain == "one row per selected SEND subject"
    assert subjects.keys == ("study_id", "subject_id")
    routes = next(column for column in subjects.columns if column.name == "routes")
    assert routes.nested_value == "list[str]"
    assert routes.nullable is False
    numeric = next(
        column for column in observations.columns if column.name == "numeric_value"
    )
    assert numeric.pandas_dtype == "Float64"
    assert numeric.nullable is True
    source_order = next(
        column for column in observations.columns if column.name == "source_order"
    )
    assert source_order.nested_value == "tuple[Any, ...]"

    with pytest.raises(ValueError, match="unsupported query result table"):
        query_result_schema("unknown")




def test_direct_node_explanation_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported explanation node kind"):
        NodeExplanation("predicate", "unknown", 0)


def test_execution_plan_resolves_row_identity_and_negation_scope_without_flattening() -> None:
    query = parse_query(
        {
            "contract": "vcgen.cohort_query.v5",
            "query_id": "typed-plan",
            "requested_endpoints": ["BW"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
            "predicate": {
                "group": "and",
                "negated": True,
                "children": [
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "TS",
                            "column": "TSPARMCD",
                            "value_type": "string",
                        },
                        "operator": "eq",
                        "value": "DOSDUR",
                    },
                    {
                        "field": {
                            "kind": "send_column",
                            "domain": "TS",
                            "column": "TSVAL",
                            "value_type": "string",
                        },
                        "operator": "contains",
                        "value": "28",
                    },
                ],
            },
        }
    )

    plan = resolve_execution_plan(query)

    assert isinstance(plan.root, GroupExecutionPlan)
    assert plan.root.execution_scope == "source_row"
    assert plan.root.negation_scope == "source_row"
    assert len(plan.root.children) == 2
    assert all(isinstance(child, RuleExecutionPlan) for child in plan.root.children)
    assert {child.identity_scope for child in plan.root.children} == {"study"}


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
    assert metadata["unit_catalogue"]["catalogue_id"] == "vcgen.unit_catalogue.v2"
    with pytest.raises(QueryError, match="target_unit"):
        EndpointFacts("LB_HGB", "LB", target_unit={})


def test_query_domains_include_predicate_observation_and_baseline_inputs() -> None:
    query = parse_query(
        {
            "contract": "vcgen.cohort_query.v5",
            "query_id": "domain-plan",
            "requested_endpoints": ["OM_WEIGHT_LIVER"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
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
        }
    )

    assert _query_domains(query) == {"BW", "LB", "OM"}


def test_membership_hash_uses_unambiguous_structured_framing() -> None:
    assert membership_hash([("A", "B\nC\0D")]) != membership_hash([("A", "B"), ("C", "D")])


def test_dm_subject_universe_rejects_blank_subject_keys(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("UPDATE dm SET USUBJID = ' '")
    with pytest.raises(SourceDataError, match="DM contains.*whitespace-only"):
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
            "contract": "vcgen.cohort_query.v5",
            "query_id": "row-scope-explanation",
            "requested_endpoints": ["BW"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
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
