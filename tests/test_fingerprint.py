"""Deterministic semantic fingerprint tests for v3 predicates."""

from __future__ import annotations

from vcgen_query import fingerprint_predicate, parse_query


def _query(predicate, query_id="q"):
    return parse_query(
        {
            "contract": "vcgen.cohort_query.v4",
            "query_id": query_id,
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
