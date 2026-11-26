"""The shared form of the reviewed alias tables (delivery routes, dose frequencies).

Each table ships as a YAML file in this package. It maps reviewed source keys
to canonical values, and lists the source keys that were reviewed and left
unmapped. This module validates the file and holds the normalization of source
text. The key shape and the SQL of each table are in the modules of the tables.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import re
from typing import Any, Generic, TypeVar

import yaml

from .contract_helpers import QueryError

K = TypeVar("K")
T = TypeVar("T", bound="AliasTable[Any]")


def normalize_source_text(value: Any) -> str | None:
    """Collapse whitespace, trim, and upper-case; a blank value is ``None``."""

    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().upper()
    return text or None


def normalized_source_sql(expr: str) -> str:
    """The SQL form of :func:`normalize_source_text`."""

    return f"nullif(upper(trim(regexp_replace(cast({expr} AS VARCHAR), '\\s+', ' ', 'g'))), '')"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def canonical_case_sql(key_sql: str, cases: Mapping[str, str]) -> str:
    """A SQL expression that maps the lookup key ``key_sql`` to its canonical value, or NULL."""

    branches = " ".join(f"WHEN {_literal(spelling)} THEN {_literal(canonical)}" for spelling, canonical in cases.items())
    return f"(CASE {key_sql} {branches} END)"


@dataclass(frozen=True)
class AliasTable(Generic[K]):
    """The reviewed table of one query execution; ``K`` is the shape of a source key."""

    contract: str
    label: str
    plural: str
    canonical_values: tuple[str, ...]
    aliases: Mapping[K, str]
    unmapped: frozenset[K]
    reviewed_source: Mapping[str, str]
    sha256: str

    def identity(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "sha256": self.sha256,
            "reviewed_source": f"dvc-md5:{self.reviewed_source['dvc_md5']}",
        }

    def require_canonical(self, value: str) -> str:
        """The canonical value that a query value spells (case and spacing aside).

        A value that is not canonical cannot select any subject, so a query that
        names one is refused instead of matching nothing.
        """

        canonical = normalize_source_text(value)
        if canonical not in self.canonical_values:
            raise QueryError(
                f"predicate.value: {value!r} is not a canonical {self.label}; "
                f"the canonical {self.plural} are {list(self.canonical_values)}."
            )
        return canonical


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{location} must be a non-empty string")
    return value.strip()


def alias_table_from_mapping(
    table_type: Callable[..., T],
    payload: Mapping[str, Any],
    *,
    sha256: str,
    contract: str,
    label: str,
    group: str,
    value_field: str,
    id_prefix: str,
    source_fields: tuple[str, ...],
    source_noun: str,
    key: Callable[[Mapping[str, Any], str], K],
) -> T:
    """Validate an alias table: each source key maps to one canonical value or is unmapped.

    ``group`` is the file key of the canonical entries and ``value_field`` the key
    of the canonical value in each entry. ``key`` reads and validates the source
    key of one source or unmapped entry from its ``source_fields``.
    """

    name = f"{label} aliases"
    if set(payload) != {"contract", "reviewed_source", "analysis", group, "unmapped"}:
        raise RuntimeError(f"{name} have incomplete or unsupported fields")
    if payload["contract"] != contract:
        raise RuntimeError(f"{name} contract must be {contract!r}")
    reviewed = payload["reviewed_source"]
    if not isinstance(reviewed, Mapping) or set(reviewed) != {"archive", "dvc_md5"}:
        raise RuntimeError(f"{name} need a reviewed_source with archive and dvc_md5")
    id_pattern = re.compile(rf"^{id_prefix}-[0-9]{{3}}$")
    seen_keys: set[Any] = set()

    def read_key(raw: Mapping[str, Any], location: str) -> K:
        source_key = key(raw, location)
        if not isinstance(raw.get("evidence"), Mapping):
            raise RuntimeError(f"{location}.evidence must be an object")
        if source_key in seen_keys:
            raise RuntimeError(f"{location} lists {source_key!r} more than once")
        seen_keys.add(source_key)
        return source_key

    canonical_values: list[str] = []
    aliases: dict[K, str] = {}
    seen_ids: set[str] = set()
    for index, raw in enumerate(payload[group] or ()):
        location = f"{name}.{group}[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"id", value_field, "note", "sources"}:
            raise RuntimeError(f"{location} has incomplete or unsupported fields")
        entry_id = raw["id"]
        if not isinstance(entry_id, str) or not id_pattern.match(entry_id) or entry_id in seen_ids:
            raise RuntimeError(f"{location}.id must be a unique {id_prefix}-nnn identifier")
        seen_ids.add(entry_id)
        canonical = _text(raw[value_field], f"{location}.{value_field}")
        if normalize_source_text(canonical) != canonical or canonical in canonical_values:
            raise RuntimeError(f"{location}.{value_field} must be a unique normalized (upper-case) {value_field}")
        canonical_values.append(canonical)
        _text(raw["note"], f"{location}.note")
        if not raw["sources"]:
            raise RuntimeError(f"{location} needs at least one source {source_noun}")
        for source_index, source in enumerate(raw["sources"]):
            source_location = f"{location}.sources[{source_index}]"
            if not isinstance(source, Mapping) or set(source) != {*source_fields, "evidence"}:
                raise RuntimeError(f"{source_location} has incomplete or unsupported fields")
            aliases[read_key(source, source_location)] = canonical
    unmapped: set[K] = set()
    for index, raw in enumerate(payload["unmapped"] or ()):
        location = f"{name}.unmapped[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {*source_fields, "reason", "upstream", "evidence"}:
            raise RuntimeError(f"{location} has incomplete or unsupported fields")
        _text(raw["reason"], f"{location}.reason")
        _text(raw["upstream"], f"{location}.upstream")
        unmapped.add(read_key(raw, location))
    # "dose frequency" and value_field "frequency" give "dose frequencies".
    plural = label.removesuffix(value_field) + group
    return table_type(contract, label, plural, tuple(canonical_values), aliases, frozenset(unmapped), dict(reviewed), sha256)


def load_alias_table(filename: str, from_mapping: Callable[..., T]) -> T:
    """Read a packaged alias file; ``from_mapping`` takes the payload and ``sha256=``."""

    raw = files("vcgen_query").joinpath("data", filename).read_bytes()
    return from_mapping(yaml.safe_load(raw), sha256=hashlib.sha256(raw).hexdigest())


__all__ = [
    "AliasTable",
    "alias_table_from_mapping",
    "canonical_case_sql",
    "load_alias_table",
    "normalize_source_text",
    "normalized_source_sql",
]
