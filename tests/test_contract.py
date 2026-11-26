"""Behavioral tests for the subject-query and cohort-query contracts."""

from __future__ import annotations

import math

import pytest
from vcgen_query import (
    SUBJECT_QUERY_CONTRACT,
    EndpointObservations,
    EndpointSpec,
    QueryError,
    SendColumnField,
    fingerprint_observation_contract,
    fingerprint_predicate,
    parse_cohort_query,
    parse_subject_query,
)


def _field(
    *,
    domain: str = "DM",
    column: str = "SEX",
    value_type: str = "string",
) -> dict[str, str]:
    return {
        "kind": "send_column",
        "domain": domain,
        "column": column,
        "value_type": value_type,
    }


def _rule(
    operator: str = "eq",
    value: object = "M",
    *,
    field: dict[str, str] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "field": field or _field(),
        "operator": operator,
    }
    if value is not None or operator not in {"is_empty", "is_not_empty"}:
        rule["value"] = value
    return rule


def _query(predicate: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract": SUBJECT_QUERY_CONTRACT,
        "query_id": "synthetic-subject-query",
        **extra,
    }
    if predicate is not None:
        payload["predicate"] = predicate
    return payload


@pytest.mark.parametrize(
    ("endpoints", "message"),
    [
        ([], "non-empty"),
        (["BW", "BW"], "unique"),
        (["bw"], "uppercase"),
        (["NOT_A_CANONICAL_ENDPOINT"], "unknown canonical endpoint"),
    ],
)
def test_requested_endpoints_are_required_unique_uppercase_catalogue_names(
    endpoints: list[str], message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        EndpointObservations(tuple(endpoints))


def test_cohort_queries_require_one_final_endpoint_stage() -> None:
    with pytest.raises(QueryError, match="endpoint_observations"):
        parse_cohort_query({"contract": "vcgen.cohort_query.v7", "subject_query": _query(), "stages": []})


@pytest.mark.parametrize(
    ("source", "target", "message"),
    [
        ("S", "s", "unknown unit"),
        ("arbitrary", "1", "unknown unit"),
        ("[pH]", "1", "unknown unit"),
        ("g", "mol", "incompatible"),
        ("g/L", "mol/L", "incompatible"),
        ("s", "g", "incompatible"),
    ],
)
def test_unit_catalogue_rejects_unknown_nonlinear_and_incompatible_units(
    source: str, target: str, message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=(source,), target_unit=target)


def test_unit_labels_are_trimmed_but_not_casefolded_or_rewritten() -> None:
    endpoint = EndpointSpec(
        "VALUE", "LB", ("VALUE",), source_units=(" g ", "G"), target_unit="g"
    )
    assert endpoint.source_units == ("G", "g")
    with pytest.raises(QueryError, match="duplicate exact labels"):
        EndpointSpec(
            "VALUE", "LB", ("VALUE",), source_units=("g", " g "), target_unit="g"
        )
    with pytest.raises(QueryError, match="exact case-sensitive code"):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=("g",), target_unit="G")
    with pytest.raises(QueryError, match="exact case-sensitive code"):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=("g",), target_unit=" g ")


def test_legacy_manual_conversion_rules_are_rejected() -> None:
    with pytest.raises(QueryError, match="unsupported key"):
        parse_subject_query(
            _query(
                observation_contract={
                    "endpoints": [
                        {
                            "name": "VALUE",
                            "domain": "LB",
                            "test_codes": ["VALUE"],
                            "source_units": ["g/L"],
                            "unit_conversion": {
                                "target_unit": "g/dL",
                                "rules": [{"source_unit": "g/L", "scale": 0.1}],
                            },
                        }
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"query_id": "missing-contract"},
        {"contract": "vcgen.cohort_query.v3", "query_id": "old-contract"},
        {"contract": "vcgen.cohort_query.v5", "query_id": "old-contract"},
    ],
)
def test_native_parsers_require_their_explicit_contract(payload: dict[str, object]) -> None:
    with pytest.raises(QueryError):
        parse_subject_query(payload)
    with pytest.raises(QueryError):
        parse_cohort_query(payload)


@pytest.mark.parametrize(
    "raw",
    [
        _query({"group": "and", "children": []}),
        _query({"group": "xor", "children": [_rule()]}),
        _query({"group": "and", "negated": "true", "children": [_rule()]}),
        _query(_rule("unknown", "M")),
        _query(_rule("between", [1], field=_field(column="AGE", value_type="number"))),
        _query(_rule("in", "M")),
        _query(_rule("eq", math.nan, field=_field(column="AGE", value_type="number"))),
        _query(_rule("eq", math.inf, field=_field(column="AGE", value_type="number"))),
        _query(
            _rule(
                "eq",
                "not-a-timestamp",
                field=_field(column="RFSTDTC", value_type="datetime"),
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "send_column",
                    "domain": "DM; DROP TABLE dm",
                    "column": "SEX",
                    "value_type": "string",
                },
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "send_column",
                    "domain": "DM",
                    "column": "SEX; DROP TABLE dm",
                    "value_type": "string",
                },
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={"kind": "derived", "name": "unknown", "value_type": "string"},
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "pre_study_endpoint",
                    "endpoint": "UNKNOWN",
                    "value_type": "number",
                },
            )
        ),
    ],
)
def test_malformed_predicates_fail_closed(raw: dict[str, object]) -> None:
    with pytest.raises(QueryError):
        parse_subject_query(raw)


def test_send_column_fields_are_closed_by_the_versioned_grit42_catalogue() -> None:
    assert SendColumnField("LB", "LBSTRESN", "number").value_type == "number"

    with pytest.raises(QueryError, match="not available in grit42 catalogue"):
        SendColumnField("DM", "NOT_A_COLUMN", "string")
    with pytest.raises(QueryError, match="must be 'number'.*LB.LBSTRESN"):
        SendColumnField("LB", "LBSTRESN", "string")


def test_predicate_fingerprint_ignores_query_and_import_provenance() -> None:
    predicate = {"group": "and", "children": [_rule("eq", "M")]}
    first = parse_subject_query(_query(predicate, query_id="first"))
    second = parse_subject_query(
        _query(
            predicate,
            query_id="second",
            import_provenance={
                "contract": "grit42.subject_query_import.v1",
                "source_contract": "grit42.form_subject_query.v1",
                "source_payload_sha256": "b" * 64,
            },
        )
    )

    assert fingerprint_predicate(first.predicate) == fingerprint_predicate(second.predicate)
    assert first.import_provenance is None
    assert second.import_provenance is not None
    assert parse_subject_query(second.to_dict()) == second


@pytest.mark.parametrize(
    "provenance",
    [
        {
            "contract": "wrong",
            "source_contract": "grit42.form_subject_query.v1",
            "source_payload_sha256": "a" * 64,
        },
        {
            "contract": "grit42.subject_query_import.v1",
            "source_contract": "wrong",
            "source_payload_sha256": "a" * 64,
        },
        {
            "contract": "grit42.subject_query_import.v1",
            "source_contract": "grit42.form_subject_query.v1",
            "source_payload_sha256": "A" * 64,
        },
    ],
)
def test_import_provenance_fails_closed(provenance: dict[str, str]) -> None:
    with pytest.raises(QueryError):
        parse_subject_query(_query(import_provenance=provenance))


def test_observation_fingerprint_includes_catalogue_endpoint_semantics() -> None:
    first = EndpointObservations(("LB_HGB",))
    changed = EndpointObservations(("LB_HCT",))

    assert fingerprint_observation_contract(first) != fingerprint_observation_contract(changed)
