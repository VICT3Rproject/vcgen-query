"""Stable fingerprints for query contracts."""

from __future__ import annotations
import hashlib
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from .contract import DerivedField, PreStudyEndpointField, Predicate, rules
from .endpoint_catalogue import endpoint_catalogue_identity
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_PREDICATE_CONTRACT
from .frequency_aliases import load_frequency_aliases
from .route_aliases import load_route_aliases
from .unit_catalogue import observation_unit_semantics

if TYPE_CHECKING:
    from .query import EndpointObservations


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
    # The route and dose frequency fields resolve source spellings through the reviewed alias tables.
    names = {rule.field.name for rule in rules(predicate) if isinstance(rule.field, DerivedField)}
    if "route" in names:
        result["route_aliases"] = load_route_aliases().identity()
    if "dose_frequency" in names:
        result["dose_frequency_aliases"] = load_frequency_aliases().identity()
    return result


def predicate_semantic_json(predicate: Predicate | None) -> str:
    return _json(predicate_semantic_payload(predicate))


def fingerprint_predicate(predicate: Predicate | None) -> str:
    """Return the stable semantic SHA-256 used by execution receipts."""
    return hashlib.sha256(predicate_semantic_json(predicate).encode("utf-8")).hexdigest()


def fingerprint_observation_contract(
    stage: EndpointObservations,
    unit_semantics: dict[str, Any] | None = None,
) -> str:
    """Return the stable SHA-256 for requested endpoint semantics.

    A caller that holds ``observation_unit_semantics`` of the stage passes it.
    """
    payload = {
        "contract": COHORT_QUERY_CONTRACT,
        "requested_endpoints": list(stage.requested_endpoints),
        "endpoint_catalogue": endpoint_catalogue_identity(),
        "endpoint_specs": [item.to_canonical_dict() for item in stage.resolved_endpoints],
        "unit_semantics": (
            observation_unit_semantics(stage.resolved_endpoints)
            if unit_semantics is None else unit_semantics
        ),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def membership_hash(keys: Sequence[tuple[str, str]]) -> str:
    """Return the SHA-256 of a set of (study, subject) keys."""
    payload = json.dumps(
        sorted(set(keys)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "fingerprint_observation_contract",
    "fingerprint_predicate",
    "membership_hash",
    "predicate_semantic_json",
    "predicate_semantic_payload",
]
