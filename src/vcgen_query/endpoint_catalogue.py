"""Validated canonical endpoint catalogue owned by :mod:`vcgen_query`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from .catalogue_values import catalogue_mapping, catalogue_sequence
from .contract_helpers import QueryError
from .send_semantics import normalize_value
from .specimen import real_specimen_classes, specimen_class, specimen_panel_id
from .unit_catalogue import unit_dimension, ratio_unit_scale


ENDPOINT_CATALOGUE_CONTRACT = "vcgen.canonical_endpoint_catalogue.v6"
# A caller names a locked endpoint catalogue and its catalogue lock with both variables. Without them, the
# package uses its bundled endpoint catalogue and no data decisions. The bundled file is the gzip of the exact
# bytes of a locked catalogue, so its identity (the md5 of those bytes) is the one that the catalogue lock names.
CATALOGUE_ENV = "VCGEN_ENDPOINT_CATALOGUE"
CATALOGUE_LOCK_ENV = "VCGEN_ENDPOINT_CATALOGUE_LOCK"
BUNDLED_CATALOGUE = "data/endpoint_catalogue/catalogue.json.gz"


@dataclass(frozen=True)
class EndpointCatalogueIdentity:
    contract: str
    catalogue_id: str
    md5: str

    def to_canonical_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "catalogue_id": self.catalogue_id,
            "md5": self.md5,
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

    def __post_init__(self) -> None:
        rule = dict(self.method_rule)
        if "values" in rule:
            rule["values"] = tuple(rule["values"])
        object.__setattr__(self, "method_rule", MappingProxyType(rule))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

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
class SpecimenPanel:
    """Tests that a study measures on one specimen draw, with the classes they may use.

    ``categories`` are the normalized LBCATs of the panel rows; a category is in one panel only.
    """

    id: str
    label: str
    classes: tuple[str, ...]
    user_choice: bool
    evidence: str
    categories: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "classes": list(self.classes),
            "user_choice": self.user_choice,
            "categories": list(self.categories),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class EndpointCatalogue:
    """The compiled catalogue. It is shared by every consumer of a process, so it is read-only."""

    identity: EndpointCatalogueIdentity
    endpoints: Mapping[str, CanonicalEndpoint]
    specimen_panels: Mapping[str, SpecimenPanel]

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoints", MappingProxyType(dict(self.endpoints)))
        object.__setattr__(self, "specimen_panels", MappingProxyType(dict(self.specimen_panels)))


def _strings(value: Any, location: str) -> tuple[str, ...]:
    items = catalogue_sequence(value, location)
    if any(not isinstance(item, str) or not item for item in items):
        raise RuntimeError(f"{location} must contain non-empty strings")
    result = tuple(items)
    if len(result) != len(set(result)):
        raise RuntimeError(f"{location} contains duplicates")
    return result


def _qualifier_rule(value: Any, location: str, *, supported: set[str]) -> tuple[str, tuple[str, ...]]:
    rule = catalogue_mapping(value, location)
    if set(rule) != {"match", "values"}:
        raise RuntimeError(f"{location} has incomplete qualifier fields")
    match = rule["match"]
    if match not in supported:
        raise RuntimeError(f"{location}.match is invalid")
    values = _strings(rule["values"], f"{location}.values")
    if (match == "any") != (not values):
        raise RuntimeError(f"{location} must use any with no values or a qualified match with values")
    return str(match), values


def _derivation(value: Any, location: str) -> EndpointDerivation:
    raw = catalogue_mapping(value, location)
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
    return _specimens_overlap(first.spec, second.spec)


def _specimens_overlap(first: Any, second: Any) -> bool:
    """Return whether two specimen rules can accept one common source row."""

    if not first.specimens or not second.specimens:
        return True
    if first.specimen_match == "class" or second.specimen_match == "class":
        if first.specimen_match == second.specimen_match == "class":
            return bool(set(first.specimens) & set(second.specimens))
        rule, other = (first, second) if first.specimen_match == "class" else (second, first)
        if other.specimen_match == "exact":
            return any(specimen_class(value) in rule.specimens for value in other.specimens)
        # A contains rule can accept a label of any class.
        return True
    if first.specimen_match == second.specimen_match == "exact":
        return bool(set(first.specimens) & set(second.specimens))
    if first.specimen_match == "exact":
        return any(right in left for left in first.specimens for right in second.specimens)
    if second.specimen_match == "exact":
        return any(left in right for left in first.specimens for right in second.specimens)
    # Two contains rules can accept at least one common value.
    return True


def _specimen_panels(value: Any) -> dict[str, SpecimenPanel]:
    panels: dict[str, SpecimenPanel] = {}
    known = real_specimen_classes()
    for index, item_value in enumerate(catalogue_sequence(value, "endpoint catalogue.specimen_panels")):
        location = f"endpoint catalogue.specimen_panels[{index}]"
        item = catalogue_mapping(item_value, location)
        if set(item) != {"id", "label", "classes", "user_choice", "categories", "evidence"}:
            raise RuntimeError(f"{location} has incomplete or unsupported fields")
        panel_id = specimen_panel_id(item["id"], f"{location}.id")
        if panel_id in panels:
            raise RuntimeError(f"endpoint catalogue duplicates specimen panel {panel_id!r}")
        classes = tuple(sorted(_strings(item["classes"], f"{location}.classes")))
        unknown = sorted(set(classes) - known)
        if unknown:
            raise RuntimeError(f"{location} names an unknown specimen class: {unknown}")
        if not isinstance(item["user_choice"], bool):
            raise RuntimeError(f"{location}.user_choice must be a boolean")
        if item["user_choice"] and len(classes) < 2:
            raise RuntimeError(f"{location} offers a user choice with fewer than two classes")
        for key in ("label", "evidence"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise RuntimeError(f"{location}.{key} must be a non-empty string")
        categories = tuple(_strings(item["categories"], f"{location}.categories"))
        for name in categories:
            if normalize_value(name) != name or any(name in other.categories for other in panels.values()):
                raise RuntimeError(f"{location}.categories {name!r} is not normalized or is in another panel")
        panels[panel_id] = SpecimenPanel(
            panel_id, item["label"].strip(), classes, item["user_choice"], item["evidence"].strip(), categories
        )
    return panels


def catalogue_location() -> tuple[Path, Path] | None:
    """The locked endpoint catalogue and its catalogue lock that the caller names, or None for the bundled one.

    A caller sets both ``VCGEN_ENDPOINT_CATALOGUE`` and ``VCGEN_ENDPOINT_CATALOGUE_LOCK``; one without the
    other is refused, so a half-configured caller never falls back to the bundled endpoint catalogue.
    """

    catalogue, lock = os.environ.get(CATALOGUE_ENV), os.environ.get(CATALOGUE_LOCK_ENV)
    if not catalogue and not lock:
        return None
    if not catalogue or not lock:
        raise QueryError(f"Set both {CATALOGUE_ENV} and {CATALOGUE_LOCK_ENV}, or neither.")
    return Path(catalogue), Path(lock)


def load_locked_catalogue(catalogue: Path, lock: Path) -> EndpointCatalogue:
    """Read the catalogue only when its md5 is the one of the catalogue lock. There is no fallback."""

    if not catalogue.is_file():
        raise QueryError(f"Endpoint catalogue {catalogue} is missing.")
    try:
        expected = yaml.safe_load(lock.read_text(encoding="utf-8"))["outputs"]["catalogue.json"]
    except (OSError, KeyError, TypeError) as error:
        raise QueryError(f"Catalogue lock {lock} is missing or names no catalogue.json.") from error
    raw_bytes = catalogue.read_bytes()
    digest = hashlib.md5(raw_bytes).hexdigest()
    if digest != expected:
        raise QueryError(f"Endpoint catalogue {catalogue} (md5 {digest[:12]}) differs from the lock "
                         f"{lock} ({str(expected)[:12]}).")
    return endpoint_catalogue_from_mapping(catalogue_mapping(json.loads(raw_bytes), "endpoint catalogue"), md5=digest)


def bundled_catalogue_bytes() -> bytes:
    """The bytes of the locked endpoint catalogue that the package bundles."""

    return gzip.decompress(files("vcgen_query").joinpath(BUNDLED_CATALOGUE).read_bytes())


@lru_cache(maxsize=1)
def load_endpoint_catalogue() -> EndpointCatalogue:
    """The active endpoint catalogue: the locked one that the caller names (`catalogue_location`), else the bundled one."""

    location = catalogue_location()
    if location is not None:
        return load_locked_catalogue(*location)
    raw_bytes = bundled_catalogue_bytes()
    return endpoint_catalogue_from_mapping(
        catalogue_mapping(json.loads(raw_bytes), "endpoint catalogue"), md5=hashlib.md5(raw_bytes).hexdigest()
    )


def endpoint_catalogue_from_mapping(
    payload: Mapping[str, Any], *, md5: str
) -> EndpointCatalogue:
    """Validate a catalogue mapping and return its compiled endpoint definitions."""

    # The local import avoids a module cycle while EndpointSpec remains part of
    # the public contract API.
    from .contract import EndpointSpec, machine_ident

    if set(payload) != {
        "contract", "catalogue_id", "captured_on", "grit42_column_catalogue_id",
        "source", "specimen_panels", "endpoints",
    }:
        raise RuntimeError("endpoint catalogue has incomplete or unsupported fields")
    if payload.get("contract") != ENDPOINT_CATALOGUE_CONTRACT:
        raise RuntimeError(f"endpoint catalogue contract must be {ENDPOINT_CATALOGUE_CONTRACT!r}")
    catalogue_id = payload.get("catalogue_id")
    if not isinstance(catalogue_id, str) or not catalogue_id:
        raise RuntimeError("endpoint catalogue id must be a non-empty string")
    identity = EndpointCatalogueIdentity(ENDPOINT_CATALOGUE_CONTRACT, catalogue_id, md5)
    panels = _specimen_panels(payload.get("specimen_panels"))
    endpoints: dict[str, CanonicalEndpoint] = {}
    for index, item_value in enumerate(catalogue_sequence(payload.get("endpoints"), "endpoint catalogue.endpoints")):
        location = f"endpoint catalogue.endpoints[{index}]"
        item = catalogue_mapping(item_value, location)
        required = {
            "name", "source_domain", "test_code_aliases", "test_name_aliases",
            "specimen_rule", "source_units", "target_unit", "method_rule",
            "sexes", "display_label", "derivation", "provenance",
        }
        if not required <= set(item) <= required | {"implicit_unit", "specimen_panel", "unit_readings"}:
            raise RuntimeError(f"{location} has incomplete or unsupported fields")
        name = machine_ident(item["name"], f"{location}.name")
        if name in endpoints:
            raise RuntimeError(f"endpoint catalogue duplicates {name!r}")
        specimen_match, specimens = _qualifier_rule(
            item["specimen_rule"], f"{location}.specimen_rule",
            supported={"exact", "contains", "class", "any"},
        )
        method_match, methods = _qualifier_rule(
            item["method_rule"], f"{location}.method_rule",
            supported={"exact", "contains", "not", "any"},
        )
        panel_id = item.get("specimen_panel")
        if (specimen_match == "class") != (panel_id is not None):
            raise RuntimeError(f"{location} needs a specimen_panel exactly when its specimen rule is a class rule")
        if panel_id is not None:
            if panel_id not in panels:
                raise RuntimeError(f"{location} names an unknown specimen panel {panel_id!r}")
            if tuple(sorted(specimens)) != panels[panel_id].classes:
                raise RuntimeError(f"{location} specimen rule must list the classes of its specimen panel")
        display_label = item["display_label"]
        if not isinstance(display_label, str) or not display_label.strip():
            raise RuntimeError(f"{location}.display_label must be a non-empty string")
        provenance = catalogue_mapping(item["provenance"], f"{location}.provenance")
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
                implicit_unit=item.get("implicit_unit"),
                specimen_panel=panel_id,
                unit_readings=catalogue_mapping(item.get("unit_readings", {}), f"{location}.unit_readings"),
            ),
            display_label=display_label.strip(),
            method_rule={"match": method_match, "values": list(methods)},
            derivation=_derivation(item["derivation"], f"{location}.derivation"),
            provenance=dict(provenance),
        )
        endpoints[name] = endpoint
    # Two endpoints can only overlap when they share a domain and a test code, so compare within those buckets.
    # Visiting the first endpoint in catalogue order and its partners in ascending order finds the same first
    # conflicting pair as a scan over all pairs.
    ordered = tuple(endpoints.values())
    bucket: dict[tuple[str, str], list[int]] = {}
    for index, endpoint in enumerate(ordered):
        for code in set(endpoint.spec.test_codes):
            bucket.setdefault((endpoint.spec.domain, code), []).append(index)
    for first_index, first in enumerate(ordered):
        partners = {
            other for code in set(first.spec.test_codes) for other in bucket[(first.spec.domain, code)]
            if other > first_index
        }
        for second_index in sorted(partners):
            second = ordered[second_index]
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
    return EndpointCatalogue(identity, endpoints, panels)


def endpoint_catalogue_identity() -> dict[str, str]:
    return load_endpoint_catalogue().identity.to_canonical_dict()


def specimen_panels() -> Mapping[str, SpecimenPanel]:
    return load_endpoint_catalogue().specimen_panels


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
    "EndpointCatalogue",
    "EndpointCatalogueIdentity",
    "EndpointDerivation",
    "SpecimenPanel",
    "canonical_endpoint",
    "endpoint_dependency_closure",
    "endpoint_catalogue_identity",
    "endpoint_catalogue_from_mapping",
    "load_endpoint_catalogue",
    "resolve_endpoint_specs",
    "specimen_panels",
]
