"""Stable fingerprints for query contracts."""

from __future__ import annotations
import hashlib
import json
from typing import Any
from .contract import PreStudyEndpointField, Predicate, Query, rules
from .endpoint_catalogue import endpoint_catalogue_identity
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_PREDICATE_CONTRACT
from .unit_catalogue import observation_unit_semantics


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def predicate_semantic_payload(predicate: Predicate | None) -> dict[str, Any]:
    """Only subject-set membership semantics, excluding observations and provenance."""
    result: dict[str, Any] = {
        "contract": SUBJECT_PREDICATE_CONTRACT,
        "predicate": None,
    }
    if predicate is not None:
        result["predicate"] = predicate.to_canonical_dict()
    dependencies = sorted(
        {
            rule.field.endpoint
            for rule in rules(predicate)
            if isinstance(rule.field, PreStudyEndpointField)
        }
    )
    if dependencies:
        result["endpoint_catalogue"] = endpoint_catalogue_identity()
        result["semantic_endpoint_dependencies"] = dependencies
    return result


def predicate_semantic_json(predicate: Predicate | None) -> str:
    return _json(predicate_semantic_payload(predicate))


def fingerprint_predicate(predicate: Predicate | None) -> str:
    """Return the stable semantic SHA-256 used by execution receipts."""
    return hashlib.sha256(predicate_semantic_json(predicate).encode("utf-8")).hexdigest()


def fingerprint_observation_contract(query: Query) -> str:
    """Return the stable SHA-256 for requested endpoint semantics."""
    payload = {
        "contract": COHORT_QUERY_CONTRACT,
        "requested_endpoints": list(query.requested_endpoints),
        "endpoint_catalogue": dict(query.endpoint_catalogue),
        "endpoint_specs": [item.to_canonical_dict() for item in query.resolved_endpoints],
        "unit_semantics": observation_unit_semantics(query.resolved_endpoints),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "fingerprint_observation_contract",
    "fingerprint_predicate",
    "predicate_semantic_json",
    "predicate_semantic_payload",
]
