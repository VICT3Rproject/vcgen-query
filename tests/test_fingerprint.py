"""Deterministic semantic fingerprint tests for v3 predicates."""

from __future__ import annotations

import vcgen_query.fingerprint as fingerprint_module
from vcgen_query import endpoint_catalogue_identity, fingerprint_predicate, parse_query


def _query(predicate, query_id="q"):
    return parse_query(
        {
            "contract": "vcgen.cohort_query.v5",
            "query_id": query_id,
            "requested_endpoints": ["BW"],
            "endpoint_catalogue": endpoint_catalogue_identity(),
            "predicate": predicate,
        }
    )


def _sex(value):
    return {
        "field": {
            "kind": "send_column",
            "domain": "DM",
            "column": "SEX",
            "value_type": "string",
        },
        "operator": "eq",
        "value": value,
    }


def test_fingerprint_ignores_query_identity_but_detects_semantic_change() -> None:
    first = _query({"group": "or", "children": [_sex("M"), _sex("F")]}, "one")
    reordered = _query({"group": "or", "children": [_sex("F"), _sex("M")]}, "two")
    changed = _query({"group": "or", "children": [_sex("M"), _sex("X")]}, "one")

    assert fingerprint_predicate(first.predicate) == fingerprint_predicate(reordered.predicate)
    assert fingerprint_predicate(first.predicate) != fingerprint_predicate(changed.predicate)


def test_fingerprint_stays_stable_after_parse_serialize_parse_round_trip() -> None:
    original = _query({"group": "and", "children": [_sex("M"), _sex("M")]})
    reparsed = parse_query(original.to_canonical_dict())

    assert fingerprint_predicate(original.predicate) == fingerprint_predicate(reparsed.predicate)


def test_absent_predicate_has_a_stable_fingerprint() -> None:
    assert fingerprint_predicate(None) == fingerprint_predicate(_query(None).predicate)


def test_predicate_only_fingerprint_excludes_requested_output_endpoints() -> None:
    predicate = {"group": "and", "children": [_sex("M")]}
    first = _query(predicate)
    second = parse_query({
        **first.to_canonical_dict(),
        "requested_endpoints": ["LB_HCT"],
    })
    assert fingerprint_predicate(first.predicate) == fingerprint_predicate(second.predicate)


def test_semantic_endpoint_fingerprint_changes_with_catalogue_resolution(monkeypatch) -> None:
    query = _query({
        "field": {"kind": "pre_study_endpoint", "endpoint": "BW", "value_type": "number"},
        "operator": "gt",
        "value": 100,
    })
    first = fingerprint_predicate(query.predicate)
    monkeypatch.setattr(
        fingerprint_module,
        "endpoint_catalogue_identity",
        lambda: {**endpoint_catalogue_identity(), "sha256": "0" * 64},
    )
    assert fingerprint_predicate(query.predicate) != first
