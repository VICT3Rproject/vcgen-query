"""Package-owned affine unit catalogue used by query parsing and projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Any

from .contract_helpers import QueryError, packaged_data


CATALOGUE_FILENAME = "unit_catalogue.v3.json"
CATALOGUE_ID = "vcgen.unit_catalogue.v3"


@dataclass(frozen=True)
class _UnitDefinition:
    code: str
    dimension: str
    scale: Decimal
    offset: Decimal


@dataclass(frozen=True)
class _CompiledUnitRule:
    source_unit: str
    source_ucum: str
    target_unit: str
    scale: float
    offset: float
    catalogue_id: str
    catalogue_sha256: str

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "source_unit": self.source_unit,
            "source_ucum": self.source_ucum,
            "target_unit": self.target_unit,
            "scale": self.scale,
            "offset": self.offset,
            "catalogue_id": self.catalogue_id,
            "catalogue_sha256": self.catalogue_sha256,
        }


@dataclass(frozen=True)
class _UnitCatalogue:
    catalogue_id: str
    sha256: str
    units: dict[str, _UnitDefinition]
    aliases: dict[str, str]


def _decimal(value: Any, location: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RuntimeError(f"{location} must be a decimal number") from error
    if not parsed.is_finite() or positive and parsed <= 0:
        requirement = "positive and finite" if positive else "finite"
        raise RuntimeError(f"{location} must be {requirement}")
    return parsed


def _load_catalogue() -> _UnitCatalogue:
    raw_bytes, sha256 = packaged_data(CATALOGUE_FILENAME)
    payload = json.loads(raw_bytes)
    if payload.get("catalogue_id") != CATALOGUE_ID:
        raise RuntimeError("unit catalogue has an unexpected catalogue_id")
    units: dict[str, _UnitDefinition] = {}
    for index, item in enumerate(payload.get("units", [])):
        code = item.get("code")
        dimension = item.get("dimension")
        if not isinstance(code, str) or not code or code in units:
            raise RuntimeError(f"unit catalogue unit {index} has an invalid or duplicate code")
        if not isinstance(dimension, str) or not dimension:
            raise RuntimeError(f"unit catalogue unit {code!r} has no dimension")
        units[code] = _UnitDefinition(
            code=code,
            dimension=dimension,
            scale=_decimal(item.get("scale"), f"unit {code!r} scale", positive=True),
            offset=_decimal(item.get("offset"), f"unit {code!r} offset"),
        )
    aliases: dict[str, str] = {}
    for index, item in enumerate(payload.get("aliases", [])):
        label = item.get("label")
        code = item.get("code")
        if not isinstance(label, str) or not label or label in aliases or label in units:
            raise RuntimeError(f"unit catalogue alias {index} has an invalid or duplicate label")
        if code not in units:
            raise RuntimeError(f"unit catalogue alias {label!r} has an unknown code")
        aliases[label] = code
    return _UnitCatalogue(
        catalogue_id=CATALOGUE_ID,
        sha256=sha256,
        units=units,
        aliases=aliases,
    )


_CATALOGUE = _load_catalogue()


def catalogue_identity() -> dict[str, str]:
    return {
        "catalogue_id": _CATALOGUE.catalogue_id,
        "catalogue_sha256": _CATALOGUE.sha256,
    }


def unit_dimension(code: str) -> str:
    """Return the dimension for one canonical UCUM code."""

    unit = _CATALOGUE.units.get(code)
    if unit is None:
        raise QueryError(
            f"unit code must be an exact case-sensitive code in {_CATALOGUE.catalogue_id}; "
            f"got {code!r}."
        )
    return unit.dimension


def compile_unit_rules(
    source_units: tuple[str, ...], target_unit: str, readings: Mapping[str, str] | None = None
) -> tuple[_CompiledUnitRule, ...]:
    """Compile one conversion rule per source label.

    `readings` maps a source label to the unit code that the label means for
    this endpoint (French "G/L" is 10^9/L on blood counts).
    """
    target = _CATALOGUE.units.get(target_unit)
    if target is None:
        raise QueryError(
            "endpoint.target_unit must be an exact case-sensitive code in "
            f"{_CATALOGUE.catalogue_id}; got {target_unit!r}."
        )
    rules: list[_CompiledUnitRule] = []
    for source_label in source_units:
        source_code = (readings or {}).get(source_label)
        if source_code is not None and source_code not in _CATALOGUE.units:
            raise QueryError(f"endpoint unit reading {source_code!r} of {source_label!r} is not a unit code.")
        if source_code is None:
            source_code = source_label if source_label in _CATALOGUE.units else _CATALOGUE.aliases.get(source_label)
        if source_code is None:
            raise QueryError(
                f"endpoint.source_units contains unknown unit {source_label!r} for target "
                f"{target_unit!r}."
            )
        source = _CATALOGUE.units[source_code]
        if source.dimension != target.dimension:
            raise QueryError(
                f"endpoint source unit {source_label!r} ({source.dimension}) is incompatible "
                f"with target_unit {target_unit!r} ({target.dimension})."
            )
        scale_decimal = source.scale / target.scale
        offset_decimal = (source.offset - target.offset) / target.scale
        scale = float(scale_decimal)
        offset = float(offset_decimal)
        if not math.isfinite(scale) or not math.isfinite(offset):
            raise RuntimeError("unit catalogue produced a non-finite compiled rule")
        rules.append(
            _CompiledUnitRule(
                source_unit=source_label,
                source_ucum=source.code,
                target_unit=target.code,
                scale=scale,
                offset=offset,
                catalogue_id=_CATALOGUE.catalogue_id,
                catalogue_sha256=_CATALOGUE.sha256,
            )
        )
    return tuple(sorted(rules, key=lambda item: item.source_unit))


def ratio_unit_scale(numerator: str, denominator: str, target: str) -> float:
    """Resolve a dimensionless ratio scale from canonical unit definitions.

    The caller checks that the parents share a dimension and the target is a fraction.
    """
    first, second, output = (_CATALOGUE.units[code] for code in (numerator, denominator, target))
    if first.offset or second.offset or output.offset:
        raise QueryError("ratio units cannot have affine offsets")
    return float(first.scale / second.scale / output.scale)


def observation_unit_semantics(endpoints: tuple[Any, ...]) -> dict[str, Any]:
    return {
        **catalogue_identity(),
        "endpoints": [
            {
                "endpoint_name": endpoint.name,
                "target_unit": endpoint.target_unit,
                "compiled_rules": [
                    rule.to_canonical_dict() for rule in endpoint.compiled_unit_rules
                ],
            }
            for endpoint in endpoints
            if endpoint.target_unit is not None
        ],
    }


__all__ = ["CATALOGUE_FILENAME", "CATALOGUE_ID", "catalogue_identity", "unit_dimension"]
