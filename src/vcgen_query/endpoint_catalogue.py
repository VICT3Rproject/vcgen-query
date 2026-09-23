"""Validated canonical endpoint catalogue owned by :mod:`vcgen_query`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import math
from typing import Any, Literal

from .contract_helpers import QueryError
from .unit_catalogue import unit_dimension, ratio_unit_scale


ENDPOINT_CATALOGUE_CONTRACT = "vcgen.canonical_endpoint_catalogue.v4"
ENDPOINT_CATALOGUE_ID = "grit42-canonical-endpoints-2026-09-17-v4"
ENDPOINT_CATALOGUE_FILENAME = "canonical_endpoints.2026-09-17.v4.json"


@dataclass(frozen=True)
class EndpointCatalogueIdentity:
    contract: str
    catalogue_id: str
    sha256: str

    def to_canonical_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "catalogue_id": self.catalogue_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class EndpointDerivation:
    """Study-invariant endpoint derivation semantics."""

    kind: Literal["primitive", "ratio"]
    parents: tuple[str, ...] = ()
    scale: float | None = None

    @property
    def status(self) -> str:
        return "primitive_observed" if self.kind == "primitive" else "derived_ratio"

    @property
    def modeling_role(self) -> str:
        return "modeled" if self.kind == "primitive" else "derived_postprocess"

    @property
    def formula(self) -> str | None:
        return None if self.kind == "primitive" else "scale * numerator / denominator"

    def to_canonical_dict(self) -> dict[str, Any]:
        if self.kind == "primitive":
            return {"kind": self.kind}
        return {
            "kind": self.kind,
            "parents": list(self.parents),
            "scale": self.scale,
        }


@dataclass(frozen=True)
class CanonicalEndpoint:
    spec: Any
    display_label: str
    method_rule: Mapping[str, Any]
    derivation: EndpointDerivation
    provenance: Mapping[str, str]

    @property
    def name(self) -> str:
        return str(self.spec.name)

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_label": self.display_label,
            "source_domain": self.spec.domain,
            "canonical_unit": self.spec.target_unit,
            "derivation": self.derivation.to_canonical_dict(),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class EndpointCatalogue:
    identity: EndpointCatalogueIdentity
    endpoints: Mapping[str, CanonicalEndpoint]


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{location} must be an object")
    return value


def _sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"{location} must be an array")
    return value


def _strings(value: Any, location: str) -> tuple[str, ...]:
    items = _sequence(value, location)
    if any(not isinstance(item, str) or not item for item in items):
        raise RuntimeError(f"{location} must contain non-empty strings")
    result = tuple(items)
    if len(result) != len(set(result)):
        raise RuntimeError(f"{location} contains duplicates")
    return result


def _qualifier_rule(value: Any, location: str, *, allow_any: bool) -> tuple[str, tuple[str, ...]]:
    rule = _mapping(value, location)
    if set(rule) != {"match", "values"}:
        raise RuntimeError(f"{location} has incomplete qualifier fields")
    match = rule["match"]
    supported = {"exact", "contains", "not"} | ({"any"} if allow_any else set())
    if match not in supported:
        raise RuntimeError(f"{location}.match is invalid")
    values = _strings(rule["values"], f"{location}.values")
    if (match == "any") != (not values):
        raise RuntimeError(f"{location} must use any with no values or a qualified match with values")
    return str(match), values


def _derivation(value: Any, location: str) -> EndpointDerivation:
    raw = _mapping(value, location)
    kind = raw.get("kind")
    if kind == "primitive":
        if set(raw) != {"kind"}:
            raise RuntimeError(f"{location} primitive definition has unsupported fields")
        return EndpointDerivation("primitive")
    if kind == "ratio":
        if set(raw) != {"kind", "parents", "scale"}:
            raise RuntimeError(f"{location} ratio definition has incomplete or unsupported fields")
        parents = _strings(raw["parents"], f"{location}.parents")
        if len(parents) != 2:
            raise RuntimeError(f"{location}.parents must contain numerator and denominator")
        if type(raw["scale"]) not in (int, float):
            raise RuntimeError(f"{location}.scale must be a positive number")
        try:
            scale = float(raw["scale"])
        except (TypeError, ValueError, OverflowError) as error:
            raise RuntimeError(f"{location}.scale must be a positive number") from error
        if not math.isfinite(scale) or not scale > 0:
            raise RuntimeError(f"{location}.scale must be a positive number")
        return EndpointDerivation("ratio", parents, scale)
    raise RuntimeError(f"{location}.kind must be 'primitive' or 'ratio'")


def _aliases_overlap(first: CanonicalEndpoint, second: CanonicalEndpoint) -> bool:
    if first.spec.domain != second.spec.domain:
        return False
    codes = set(first.spec.test_codes) & set(second.spec.test_codes)
    if not codes:
        return False
    if first.spec.test_names and second.spec.test_names:
        if not set(first.spec.test_names) & set(second.spec.test_names):
            return False
    first_specimens = set(first.spec.specimens)
    second_specimens = set(second.spec.specimens)
    if first_specimens and second_specimens:
        if first.spec.specimen_match == second.spec.specimen_match == "exact":
            return bool(first_specimens & second_specimens)
        if first.spec.specimen_match == "exact" and second.spec.specimen_match == "contains":
            return any(
                right in left
                for left in first_specimens
                for right in second_specimens
            )
        if first.spec.specimen_match == "contains" and second.spec.specimen_match == "exact":
            return any(
                left in right
                for left in first_specimens
                for right in second_specimens
            )
        if first.spec.specimen_match == "exact" and second.spec.specimen_match == "not":
            return bool(first_specimens - second_specimens)
        if first.spec.specimen_match == "not" and second.spec.specimen_match == "exact":
            return bool(second_specimens - first_specimens)
        # Negative and two contains rules can accept at least one common value.
        return True
    return not first_specimens or not second_specimens


@lru_cache(maxsize=1)
def load_endpoint_catalogue() -> EndpointCatalogue:
    resource = files("vcgen_query").joinpath("data", ENDPOINT_CATALOGUE_FILENAME)
    raw_bytes = resource.read_bytes()
    payload = _mapping(json.loads(raw_bytes), "endpoint catalogue")
    return endpoint_catalogue_from_mapping(payload, sha256=hashlib.sha256(raw_bytes).hexdigest())


def endpoint_catalogue_from_mapping(
    payload: Mapping[str, Any], *, sha256: str
) -> EndpointCatalogue:
    """Validate a catalogue mapping and return its compiled endpoint definitions."""

    # The local import avoids a module cycle while EndpointSpec remains part of
    # the public contract API.
    from .contract import EndpointSpec, machine_ident

    if set(payload) != {
        "contract", "catalogue_id", "captured_on", "grit42_column_catalogue_id",
        "source", "endpoints",
    }:
        raise RuntimeError("endpoint catalogue has incomplete or unsupported fields")
    if payload.get("contract") != ENDPOINT_CATALOGUE_CONTRACT:
        raise RuntimeError(f"endpoint catalogue contract must be {ENDPOINT_CATALOGUE_CONTRACT!r}")
    if payload.get("catalogue_id") != ENDPOINT_CATALOGUE_ID:
        raise RuntimeError(f"endpoint catalogue id must be {ENDPOINT_CATALOGUE_ID!r}")
    identity = EndpointCatalogueIdentity(
        ENDPOINT_CATALOGUE_CONTRACT,
        ENDPOINT_CATALOGUE_ID,
        sha256,
    )
    endpoints: dict[str, CanonicalEndpoint] = {}
    for index, item_value in enumerate(_sequence(payload.get("endpoints"), "endpoint catalogue.endpoints")):
        location = f"endpoint catalogue.endpoints[{index}]"
        item = _mapping(item_value, location)
        required = {
            "name", "source_domain", "test_code_aliases", "test_name_aliases",
            "specimen_rule", "source_units", "target_unit", "method_rule",
            "sexes", "display_label", "derivation", "provenance",
        }
        if set(item) != required:
            raise RuntimeError(f"{location} has incomplete or unsupported fields")
        name = machine_ident(item["name"], f"{location}.name")
        if name in endpoints:
            raise RuntimeError(f"endpoint catalogue duplicates {name!r}")
        specimen_match, specimens = _qualifier_rule(
            item["specimen_rule"], f"{location}.specimen_rule", allow_any=True
        )
        method_match, methods = _qualifier_rule(
            item["method_rule"], f"{location}.method_rule", allow_any=True
        )
        display_label = item["display_label"]
        if not isinstance(display_label, str) or not display_label.strip():
            raise RuntimeError(f"{location}.display_label must be a non-empty string")
        provenance = _mapping(item["provenance"], f"{location}.provenance")
        if not provenance or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in provenance.items()
        ):
            raise RuntimeError(f"{location}.provenance must contain non-empty string facts")
        endpoint = CanonicalEndpoint(
            spec=EndpointSpec(
                name=name,
                domain=item["source_domain"],
                test_codes=_strings(item["test_code_aliases"], f"{location}.test_code_aliases"),
                test_names=_strings(item["test_name_aliases"], f"{location}.test_name_aliases"),
                source_units=_strings(item["source_units"], f"{location}.source_units"),
                specimens=specimens,
                specimen_match="exact" if specimen_match == "any" else specimen_match,
                sexes=_strings(item["sexes"], f"{location}.sexes"),
                target_unit=item["target_unit"],
                methods=methods,
                method_match="exact" if method_match == "any" else method_match,
            ),
            display_label=display_label.strip(),
            method_rule={"match": method_match, "values": list(methods)},
            derivation=_derivation(item["derivation"], f"{location}.derivation"),
            provenance=dict(provenance),
        )
        endpoints[name] = endpoint
    for first_index, first in enumerate(endpoints.values()):
        for second in tuple(endpoints.values())[first_index + 1 :]:
            if _aliases_overlap(first, second):
                raise RuntimeError(
                    f"endpoint catalogue has ambiguous aliases for {first.name!r} and {second.name!r}"
                )
    unknown_parents = sorted(
        {parent for endpoint in endpoints.values() for parent in endpoint.derivation.parents}
        - set(endpoints)
    )
    if unknown_parents:
        raise RuntimeError(f"endpoint catalogue has unknown derived parent(s): {unknown_parents}")
    _dependency_closure(tuple(endpoints), endpoints)
    for endpoint in endpoints.values():
        if endpoint.derivation.kind != "ratio":
            continue
        numerator, denominator = (
            endpoints[parent] for parent in endpoint.derivation.parents
        )
        if unit_dimension(numerator.spec.target_unit) != unit_dimension(
            denominator.spec.target_unit
        ):
            raise RuntimeError(
                f"endpoint catalogue ratio {endpoint.name!r} has incompatible parent units"
            )
        if unit_dimension(endpoint.spec.target_unit) != "fraction":
            raise RuntimeError(
                f"endpoint catalogue ratio {endpoint.name!r} must use a fraction target unit"
            )
        expected_scale = ratio_unit_scale(numerator.spec.target_unit, denominator.spec.target_unit, endpoint.spec.target_unit)
        if not math.isclose(endpoint.derivation.scale, expected_scale, rel_tol=1e-12):
            raise RuntimeError(f"endpoint catalogue ratio {endpoint.name!r} scale disagrees with canonical units: expected {expected_scale}")
    return EndpointCatalogue(identity, endpoints)


def endpoint_catalogue_identity() -> dict[str, str]:
    return load_endpoint_catalogue().identity.to_canonical_dict()


def canonical_endpoint(name: str) -> CanonicalEndpoint:
    try:
        return load_endpoint_catalogue().endpoints[name]
    except KeyError as error:
        raise QueryError(f"unknown canonical endpoint {name!r}") from error


def _dependency_closure(names: Sequence[str], endpoints: Mapping[str, CanonicalEndpoint]) -> tuple[str, ...]:
    visited: set[str] = set()
    active: set[str] = set()
    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in active:
            raise RuntimeError(f"endpoint catalogue derived-parent cycle at {name!r}")
        if name in visited:
            return
        if name not in endpoints:
            raise QueryError(f"unknown canonical endpoint {name!r}")
        active.add(name)
        for parent in endpoints[name].derivation.parents:
            visit(parent)
        active.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in names:
        visit(name)
    return tuple(ordered)


def endpoint_dependency_closure(names: Sequence[str]) -> tuple[str, ...]:
    """Expand fixed derivation dependencies in deterministic parent-first order."""
    return _dependency_closure(names, load_endpoint_catalogue().endpoints)


def resolve_endpoint_specs(names: Sequence[str]) -> tuple[Any, ...]:
    return tuple(canonical_endpoint(name).spec for name in names)


__all__ = [
    "CanonicalEndpoint",
    "ENDPOINT_CATALOGUE_CONTRACT",
    "ENDPOINT_CATALOGUE_FILENAME",
    "ENDPOINT_CATALOGUE_ID",
    "EndpointCatalogue",
    "EndpointCatalogueIdentity",
    "EndpointDerivation",
    "canonical_endpoint",
    "endpoint_dependency_closure",
    "endpoint_catalogue_identity",
    "endpoint_catalogue_from_mapping",
    "load_endpoint_catalogue",
    "resolve_endpoint_specs",
]
