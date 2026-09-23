"""Typed predicate and cohort-query contracts."""

from __future__ import annotations
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

from .contract_helpers import QueryError
from .send_semantics import normalize_human_category, normalize_unit
from .unit_catalogue import _CompiledUnitRule, compile_unit_rules
from .identities import (
    COHORT_QUERY_CONTRACT,
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
)
from .grit42_catalogue import GRIT42_CATALOGUE_ID, grit42_column

ValueType: TypeAlias = Literal["string", "number", "datetime"]
SpecimenMatch: TypeAlias = Literal["exact", "contains", "not"]
DerivedFieldName: TypeAlias = Literal[
    "supplier",
    "study_type",
    "planned_dose_duration_days",
    "test_facility",
    "study_start_year",
    "glp_status",
    "species",
    "sex",
    "strain",
    "route",
    "dose_frequency",
    "treatment_vehicle",
    "baseline_body_weight_g",
    "age_days",
    "disposition_categories",
    "terminal_sacrifice_offset_days",
]
CanonicalOperator: TypeAlias = Literal[
    "eq",
    "ne",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
    "is_empty",
    "is_not_empty",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "not_between",
]
PredicateGroupOperator: TypeAlias = Literal["and", "or"]
CohortQueryContract: TypeAlias = Literal["vcgen.cohort_query.v5"]

DERIVED_FIELD_TYPES: dict[DerivedFieldName, ValueType] = {
    "supplier": "string",
    "study_type": "string",
    "planned_dose_duration_days": "number",
    "test_facility": "string",
    "study_start_year": "number",
    "glp_status": "string",
    "species": "string",
    "sex": "string",
    "strain": "string",
    "route": "string",
    "dose_frequency": "string",
    "treatment_vehicle": "string",
    "baseline_body_weight_g": "number",
    "age_days": "number",
    "disposition_categories": "string",
    "terminal_sacrifice_offset_days": "number",
}
STR_OPS = frozenset(
    {
        "eq",
        "ne",
        "in",
        "not_in",
        "contains",
        "not_contains",
        "starts_with",
        "not_starts_with",
        "ends_with",
        "not_ends_with",
        "is_empty",
        "is_not_empty",
    }
)
ORD_OPS = frozenset(
    {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between", "not_between"}
)
ZERO = frozenset({"is_empty", "is_not_empty"})
LIST = frozenset({"in", "not_in"})
RANGE = frozenset({"between", "not_between"})
ALIASES: dict[str, CanonicalOperator] = {
    "=": "eq",
    "==": "eq",
    "eq": "eq",
    "equal": "eq",
    "equals": "eq",
    "is": "eq",
    "!=": "ne",
    "<>": "ne",
    "ne": "ne",
    "not equal": "ne",
    "not equals": "ne",
    "is not": "ne",
    "in": "in",
    "not in": "not_in",
    "not_in": "not_in",
    "contains": "contains",
    "not contains": "not_contains",
    "not_contains": "not_contains",
    "starts with": "starts_with",
    "starts_with": "starts_with",
    "not starts with": "not_starts_with",
    "not_starts_with": "not_starts_with",
    "ends with": "ends_with",
    "ends_with": "ends_with",
    "not ends with": "not_ends_with",
    "not_ends_with": "not_ends_with",
    "is empty": "is_empty",
    "is_empty": "is_empty",
    "is not empty": "is_not_empty",
    "is_not_empty": "is_not_empty",
    ">": "gt",
    "gt": "gt",
    "greater than": "gt",
    ">=": "gte",
    "gte": "gte",
    "greater than or equal to": "gte",
    "<": "lt",
    "lt": "lt",
    "less than": "lt",
    "<=": "lte",
    "lte": "lte",
    "less than or equal to": "lte",
    "between": "between",
    "not between": "not_between",
    "not_between": "not_between",
}
ID = re.compile(r"^[A-Z][A-Z0-9_]*$")
HASH = re.compile(r"^[0-9a-f]{64}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$")


def mapping(v: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise QueryError(f"{location} must be an object.")
    return v


def keys(v: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unsupported = sorted(set(v) - allowed)
    if unsupported:
        raise QueryError(f"{location} has unsupported key(s): {unsupported}.")


def seq(v: Any) -> bool:
    return isinstance(v, Sequence) and not isinstance(v, (str, bytes, bytearray))


def finite(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def text(v: Any, location: str, nonempty: bool = False) -> str:
    if not isinstance(v, str) or nonempty and not v:
        raise QueryError(f"{location} must be {'a non-empty ' if nonempty else 'a '}string.")
    return v


def ident(v: Any, location: str) -> str:
    if not isinstance(v, str) or not ID.fullmatch(v):
        raise QueryError(f"{location} must be an uppercase SEND identifier.")
    return v


def machine_ident(v: Any, location: str) -> str:
    if not isinstance(v, str) or not ID.fullmatch(v):
        raise QueryError(f"{location} must be an uppercase machine identifier.")
    return v


def typ(v: Any, location: str) -> ValueType:
    if not isinstance(v, str) or v not in {"string", "number", "datetime"}:
        raise QueryError(f"{location} must be string, number, or datetime.")
    return cast(ValueType, v)


def texts(v: Any, location: str) -> tuple[str, ...]:
    if not seq(v):
        raise QueryError(f"{location} must be an array.")
    return tuple(sorted({text(item, f"{location}[{index}]", True) for index, item in enumerate(v)}))


def normalized_texts(v: Any, location: str) -> tuple[str, ...]:
    if not seq(v):
        raise QueryError(f"{location} must be an array.")
    normalized = set()
    for index, item in enumerate(v):
        value = normalize_human_category(text(item, f"{location}[{index}]", True))
        if value is None:
            raise QueryError(f"{location}[{index}] must be a non-missing category.")
        normalized.add(value)
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class DayWindow:
    min: int | None = None
    max: int | None = None

    def __post_init__(self) -> None:
        if self.min is None and self.max is None:
            raise QueryError("day_window requires at least one bound.")
        if not all(value is None or type(value) is int for value in (self.min, self.max)):
            raise QueryError("day_window bounds must be integers.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise QueryError("day_window minimum must not exceed maximum.")

    @classmethod
    def from_mapping(cls, v: Any, *, location: str = "day_window"):
        if v is None:
            return None
        raw = mapping(v, location)
        keys(raw, {"min", "max"}, location)
        return cls(raw.get("min"), raw.get("max"))

    def contains(self, value: Any) -> bool:
        return (
            type(value) is int
            and (self.min is None or value >= self.min)
            and (self.max is None or value <= self.max)
        )

    def to_canonical_dict(self):
        return {k: v for k, v in (("min", self.min), ("max", self.max)) if v is not None}


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    domain: str
    test_codes: tuple[str, ...]
    test_names: tuple[str, ...] = ()
    source_units: tuple[str, ...] = ()
    specimens: tuple[str, ...] = ()
    specimen_match: SpecimenMatch = "exact"
    sexes: tuple[str, ...] = ()
    day_window: DayWindow | None = None
    target_unit: str | None = None
    methods: tuple[str, ...] = ()
    method_match: SpecimenMatch = "exact"
    compiled_unit_rules: tuple[_CompiledUnitRule, ...] = field(
        init=False, repr=False, compare=False, default=()
    )

    def __post_init__(self) -> None:
        name = machine_ident(self.name, "endpoint.name")
        if name in {"STUDY_ID", "SUBJECT_ID", "STUDY_DAY"}:
            raise QueryError("endpoint.name cannot use a daily key column.")
        test_codes = normalized_texts(self.test_codes, "endpoint.test_codes")
        if not test_codes:
            raise QueryError("endpoint.test_codes must be a non-empty array.")
        if not seq(self.source_units):
            raise QueryError("endpoint.source_units must be an array.")
        units: list[str] = []
        for index, value in enumerate(self.source_units):
            unit = normalize_unit(text(value, f"endpoint.source_units[{index}]", True))
            if unit is None:
                raise QueryError(f"endpoint.source_units[{index}] is unsupported.")
            units.append(unit)
        if len(units) != len(set(units)):
            raise QueryError("endpoint.source_units has duplicate exact labels.")
        if not isinstance(self.specimen_match, str) or self.specimen_match not in {
            "exact",
            "contains",
            "not",
        }:
            raise QueryError("endpoint.specimen_match is invalid.")
        if not isinstance(self.method_match, str) or self.method_match not in {
            "exact",
            "contains",
            "not",
        }:
            raise QueryError("endpoint.method_match is invalid.")
        if self.day_window is not None:
            if not isinstance(self.day_window, DayWindow):
                raise QueryError("endpoint.day_window must be DayWindow when supplied.")
        source_units = tuple(sorted(units))
        target_unit = None
        compiled_unit_rules: tuple[_CompiledUnitRule, ...] = ()
        if self.target_unit is not None:
            target_unit = text(self.target_unit, "endpoint.target_unit", True)
            if not source_units:
                raise QueryError(
                    "endpoint.source_units must be non-empty when target_unit is supplied."
                )
            compiled_unit_rules = compile_unit_rules(source_units, target_unit)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "domain", ident(self.domain, "endpoint.domain"))
        object.__setattr__(self, "test_codes", test_codes)
        object.__setattr__(
            self, "test_names", normalized_texts(self.test_names, "endpoint.test_names")
        )
        object.__setattr__(self, "source_units", source_units)
        object.__setattr__(self, "target_unit", target_unit)
        object.__setattr__(self, "compiled_unit_rules", compiled_unit_rules)
        object.__setattr__(
            self, "specimens", normalized_texts(self.specimens, "endpoint.specimens")
        )
        object.__setattr__(self, "methods", normalized_texts(self.methods, "endpoint.methods"))
        object.__setattr__(self, "sexes", normalized_texts(self.sexes, "endpoint.sexes"))

    @classmethod
    def from_mapping(cls, v: Any, *, index: int = 0, location: str | None = None):
        endpoint_location = location or f"observation_contract.endpoints[{index}]"
        r = mapping(v, endpoint_location)
        req = {"name", "domain", "test_codes"}
        if req - set(r):
            raise QueryError(
                f"{endpoint_location} is missing required key(s): {sorted(req - set(r))}."
            )
        keys(
            r,
            req
            | {
                "test_names",
                "source_units",
                "specimens",
                "specimen_match",
                "sexes",
                "day_window",
                "target_unit",
                "methods",
                "method_match",
            },
            endpoint_location,
        )
        return cls(
            r["name"],
            r["domain"],
            r["test_codes"],
            r.get("test_names", ()),
            r.get("source_units", ()),
            r.get("specimens", ()),
            r.get("specimen_match", "exact"),
            r.get("sexes", ()),
            DayWindow.from_mapping(r.get("day_window"), location=f"{endpoint_location}.day_window"),
            r.get("target_unit"),
            r.get("methods", ()),
            r.get("method_match", "exact"),
        )

    def applies_to_sex(self, sex):
        normalized = normalize_human_category(sex)
        return not self.sexes or normalized in self.sexes

    def to_canonical_dict(self):
        o = {
            "name": self.name,
            "domain": self.domain,
            "test_codes": list(self.test_codes),
        }
        for k in ("test_names", "source_units", "specimens", "methods", "sexes"):
            if getattr(self, k):
                o[k] = list(getattr(self, k))
        if self.specimen_match != "exact":
            o["specimen_match"] = self.specimen_match
        if self.method_match != "exact":
            o["method_match"] = self.method_match
        if self.day_window:
            o["day_window"] = self.day_window.to_canonical_dict()
        if self.target_unit is not None:
            o["target_unit"] = self.target_unit
        return o


@dataclass(frozen=True)
class ObservationContract:
    endpoints: tuple[EndpointSpec, ...] = ()

    def __post_init__(self) -> None:
        if not seq(self.endpoints) or any(
            not isinstance(endpoint, EndpointSpec) for endpoint in self.endpoints
        ):
            raise QueryError("observation_contract.endpoints must contain EndpointSpec values.")
        endpoints = tuple(self.endpoints)
        if len(endpoints) != len({endpoint.name for endpoint in endpoints}):
            raise QueryError("observation_contract endpoint names must be unique.")
        object.__setattr__(self, "endpoints", endpoints)

    @classmethod
    def from_mapping(cls, v):
        if v is None:
            return cls()
        r = mapping(v, "observation_contract")
        keys(r, {"endpoints"}, "observation_contract")
        items = r.get("endpoints", ())
        if not seq(items):
            raise QueryError("observation_contract.endpoints must be an array.")
        return cls(tuple(EndpointSpec.from_mapping(x, index=i) for i, x in enumerate(items)))

    def to_canonical_dict(self):
        return {"endpoints": [x.to_canonical_dict() for x in self.endpoints]}


@dataclass(frozen=True)
class SendColumnField:
    domain: str
    column: str
    value_type: ValueType
    kind: Literal["send_column"] = "send_column"

    def __post_init__(self) -> None:
        if self.kind != "send_column":
            raise QueryError("field.kind must be 'send_column'.")
        domain = ident(self.domain, "field.domain")
        column = ident(self.column, "field.column")
        value_type = typ(self.value_type, "field.value_type")
        captured = grit42_column(domain, column)
        if captured is None:
            raise QueryError(
                f"field {domain}.{column} is not available in grit42 catalogue "
                f"{GRIT42_CATALOGUE_ID!r}."
            )
        if value_type != captured.value_type:
            raise QueryError(
                f"field.value_type must be {captured.value_type!r} for grit42 column "
                f"{domain}.{column}."
            )
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "value_type", value_type)

    @classmethod
    def from_mapping(cls, v, *, location="field"):
        r = mapping(v, location)
        keys(r, {"kind", "domain", "column", "value_type"}, location)
        if r.get("kind") != "send_column":
            raise QueryError(f"{location}.kind must be 'send_column'.")
        return cls(
            ident(r.get("domain"), f"{location}.domain"),
            ident(r.get("column"), f"{location}.column"),
            typ(r.get("value_type"), f"{location}.value_type"),
        )

    def to_canonical_dict(self):
        return {
            "kind": self.kind,
            "domain": self.domain,
            "column": self.column,
            "value_type": self.value_type,
        }


@dataclass(frozen=True)
class DerivedField:
    name: DerivedFieldName
    value_type: ValueType
    kind: Literal["derived"] = "derived"

    def __post_init__(self) -> None:
        if self.kind != "derived":
            raise QueryError("field.kind must be 'derived'.")
        name = text(self.name, "field.name", True)
        if name not in DERIVED_FIELD_TYPES:
            raise QueryError("field.name is unsupported.")
        value_type = typ(self.value_type, "field.value_type")
        expected = DERIVED_FIELD_TYPES[cast(DerivedFieldName, name)]
        if value_type != expected:
            raise QueryError(f"field.value_type must be {expected!r}.")

    @classmethod
    def from_mapping(cls, v, *, location="field"):
        r = mapping(v, location)
        keys(r, {"kind", "name", "value_type"}, location)
        n = r.get("name")
        if r.get("kind") != "derived" or n not in DERIVED_FIELD_TYPES:
            raise QueryError(f"{location}.name is unsupported.")
        t = typ(r.get("value_type"), f"{location}.value_type")
        if t != DERIVED_FIELD_TYPES[n]:
            raise QueryError(f"{location}.value_type must be {DERIVED_FIELD_TYPES[n]!r}.")
        return cls(n, t)

    def to_canonical_dict(self):
        return {"kind": self.kind, "name": self.name, "value_type": self.value_type}


@dataclass(frozen=True)
class PreStudyEndpointField:
    endpoint: str
    value_type: Literal["number"] = "number"
    kind: Literal["pre_study_endpoint"] = "pre_study_endpoint"

    def __post_init__(self) -> None:
        if self.kind != "pre_study_endpoint":
            raise QueryError("field.kind must be 'pre_study_endpoint'.")
        if self.value_type != "number":
            raise QueryError("field.value_type must be 'number'.")
        endpoint = machine_ident(self.endpoint, "field.endpoint")
        from .endpoint_catalogue import canonical_endpoint

        canonical_endpoint(endpoint)
        object.__setattr__(self, "endpoint", endpoint)

    @classmethod
    def from_mapping(cls, v, *, location="field"):
        r = mapping(v, location)
        keys(r, {"kind", "endpoint", "value_type"}, location)
        if (
            r.get("kind") != "pre_study_endpoint"
            or typ(r.get("value_type"), f"{location}.value_type") != "number"
        ):
            raise QueryError(f"{location} must be numeric pre_study_endpoint.")
        return cls(text(r.get("endpoint"), f"{location}.endpoint", True))

    def to_canonical_dict(self):
        return {
            "kind": self.kind,
            "endpoint": self.endpoint,
            "value_type": self.value_type,
        }


FieldRef = SendColumnField | DerivedField | PreStudyEndpointField


def parse_field(v, *, location="field"):
    r = mapping(v, location)
    return (
        SendColumnField.from_mapping(r, location=location)
        if r.get("kind") == "send_column"
        else DerivedField.from_mapping(r, location=location)
        if r.get("kind") == "derived"
        else PreStudyEndpointField.from_mapping(r, location=location)
        if r.get("kind") == "pre_study_endpoint"
        else (_ for _ in ()).throw(QueryError(f"{location}.kind is unsupported."))
    )


def normalize_operator(v, *, location="operator") -> CanonicalOperator:
    if not isinstance(v, str):
        raise QueryError(f"{location} must be a string.")
    k = " ".join(v.lower().replace("_", " ").strip().split())
    if k not in ALIASES:
        raise QueryError(f"{location} has unsupported operator {v!r}.")
    return ALIASES[k]


def val(v, t, location):
    if t == "string":
        return text(v, location)
    if t == "number":
        if not finite(v):
            raise QueryError(f"{location} must be a finite number.")
        return v
    if not isinstance(v, str):
        raise QueryError(f"{location} must be a datetime string.")
    if DATE.fullmatch(v):
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError as error:
            raise QueryError(f"{location} must be a valid ISO date.") from error
    if not TS.fullmatch(v):
        raise QueryError(f"{location} must be an ISO date or complete timestamp.")
    try:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as error:
        raise QueryError(f"{location} must be a valid ISO timestamp.") from error
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="microseconds").replace(".000000", "")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        .replace(".000000Z", "Z")
    )


def ruleval(v, t, op, location):
    if op in LIST:
        if not seq(v) or not v:
            raise QueryError(f"{location} must be a non-empty array for {op}.")
        return tuple(
            sorted(
                {val(item, t, f"{location}[{index}]") for index, item in enumerate(v)},
                key=lambda item: (type(item).__name__, item),
            )
        )
    if op in RANGE:
        if not seq(v) or len(v) != 2:
            raise QueryError(f"{location} must be a two-item array for {op}.")
        lower, upper = (
            val(v[0], t, f"{location}[0]"),
            val(v[1], t, f"{location}[1]"),
        )
        if lower > upper:
            raise QueryError(f"{location}[0] must not exceed {location}[1].")
        return (lower, upper)
    if seq(v):
        raise QueryError(f"{location} must be scalar for {op}.")
    return val(v, t, location)


@dataclass(frozen=True)
class PredicateRule:
    field: FieldRef
    operator: CanonicalOperator
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.field, (SendColumnField, DerivedField, PreStudyEndpointField)):
            raise QueryError("predicate.field must be a typed field reference.")
        valid_operators = STR_OPS if self.field.value_type == "string" else ORD_OPS
        if not isinstance(self.operator, str) or self.operator not in valid_operators:
            raise QueryError(
                f"predicate.operator {self.operator!r} is invalid for {self.field.value_type}."
            )
        if self.operator in ZERO:
            if self.value is not None:
                raise QueryError(f"predicate.value must be omitted or null for {self.operator}.")
            return
        object.__setattr__(
            self,
            "value",
            ruleval(self.value, self.field.value_type, self.operator, "predicate.value"),
        )

    @classmethod
    def from_mapping(cls, v, *, location="predicate"):
        r = mapping(v, location)
        keys(r, {"field", "operator", "value"}, location)
        if {"field", "operator"} - set(r):
            raise QueryError(f"{location} requires field and operator.")
        f = parse_field(r["field"], location=f"{location}.field")
        o = normalize_operator(r["operator"], location=f"{location}.operator")
        if o not in (STR_OPS if f.value_type == "string" else ORD_OPS):
            raise QueryError(f"{location}.operator {o!r} is invalid for {f.value_type}.")
        if o in ZERO:
            if r.get("value") is not None:
                raise QueryError(f"{location}.value must be omitted or null for {o}.")
            x = None
        else:
            if "value" not in r:
                raise QueryError(f"{location}.value is required for {o}.")
            x = ruleval(r["value"], f.value_type, o, f"{location}.value")
        return cls(f, o, x)

    @property
    def value_type(self):
        return self.field.value_type

    def to_canonical_dict(self):
        o = {"field": self.field.to_canonical_dict(), "operator": self.operator}
        if self.value is not None:
            o["value"] = list(self.value) if isinstance(self.value, tuple) else self.value
        return o


@dataclass(frozen=True)
class PredicateGroup:
    group: PredicateGroupOperator
    children: tuple[PredicateRule | PredicateGroup, ...]
    negated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.group, str) or self.group not in {"and", "or"}:
            raise QueryError("predicate.group must be 'and' or 'or'.")
        if not seq(self.children) or not self.children:
            raise QueryError("predicate.children must be a non-empty array.")
        if any(not isinstance(child, (PredicateRule, PredicateGroup)) for child in self.children):
            raise QueryError("predicate.children must contain typed predicates.")
        if not isinstance(self.negated, bool):
            raise QueryError("predicate.negated must be boolean.")
        object.__setattr__(self, "children", canon(tuple(self.children)))

    @classmethod
    def from_mapping(cls, v, *, location="predicate"):
        r = mapping(v, location)
        keys(r, {"group", "children", "negated"}, location)
        if r.get("group") not in {"and", "or"}:
            raise QueryError(f"{location}.group must be 'and' or 'or'.")
        c = r.get("children")
        if not seq(c) or not c:
            raise QueryError(f"{location}.children must be a non-empty array.")
        if not isinstance(r.get("negated", False), bool):
            raise QueryError(f"{location}.negated must be boolean.")
        return cls(
            r["group"],
            canon(
                tuple(
                    parse_predicate(x, location=f"{location}.children[{i}]")
                    for i, x in enumerate(c)
                )
            ),
            r.get("negated", False),
        )

    @property
    def operator(self):
        return self.group

    def to_canonical_dict(self):
        o = {
            "group": self.group,
            "children": [x.to_canonical_dict() for x in self.children],
        }
        if self.negated:
            o["negated"] = True
        return o


Predicate = PredicateGroup | PredicateRule


def parse_predicate(v, *, location="predicate"):
    r = mapping(v, location)
    return (
        PredicateGroup.from_mapping(r, location=location)
        if "group" in r
        else PredicateRule.from_mapping(r, location=location)
    )


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canon(items):
    d = {}
    for x in items:
        d.setdefault(digest(x.to_canonical_dict()), x)
    return tuple(d[k] for k in sorted(d))


def rules(p):
    if p is None:
        return ()
    if isinstance(p, PredicateRule):
        return (p,)
    return tuple(x for c in p.children for x in rules(c))


@dataclass(frozen=True)
class QueryImportProvenance:
    source_contract: Literal["grit42.form_subject_query.v1", "grit42.advanced_subject_query.v1"]
    source_payload_sha256: str
    contract: Literal["grit42.subject_query_import.v1"] = GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT

    def __post_init__(self) -> None:
        if not isinstance(self.source_contract, str) or self.source_contract not in {
            GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
            GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
        }:
            raise QueryError("import_provenance.source_contract is unsupported.")
        if self.contract != GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT:
            raise QueryError(
                f"import_provenance.contract must be {GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT!r}."
            )
        if not isinstance(self.source_payload_sha256, str) or not HASH.fullmatch(
            self.source_payload_sha256
        ):
            raise QueryError(
                "import_provenance.source_payload_sha256 must be a lowercase SHA-256 digest."
            )

    @classmethod
    def from_mapping(cls, value: Any) -> QueryImportProvenance:
        raw = mapping(value, "import_provenance")
        keys(
            raw,
            {"contract", "source_contract", "source_payload_sha256"},
            "import_provenance",
        )
        required = {"contract", "source_contract", "source_payload_sha256"}
        if required - set(raw):
            raise QueryError(
                f"import_provenance is missing required key(s): {sorted(required - set(raw))}."
            )
        return cls(
            source_contract=raw["source_contract"],
            source_payload_sha256=raw["source_payload_sha256"],
            contract=raw["contract"],
        )

    def to_canonical_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "source_contract": self.source_contract,
            "source_payload_sha256": self.source_payload_sha256,
        }


@dataclass(frozen=True)
class Query:
    query_id: str
    requested_endpoints: tuple[str, ...]
    endpoint_catalogue: Mapping[str, str]
    predicate: Predicate | None = None
    import_provenance: QueryImportProvenance | None = None
    source_identity: str | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    contract: CohortQueryContract = COHORT_QUERY_CONTRACT

    def __post_init__(self):
        if self.contract != COHORT_QUERY_CONTRACT:
            raise QueryError(
                f"unsupported contract {self.contract!r}; expected {COHORT_QUERY_CONTRACT!r}."
            )
        if not isinstance(self.query_id, str) or not self.query_id.strip():
            raise QueryError("query_id must be a non-empty string.")
        if self.predicate is not None and not isinstance(
            self.predicate, (PredicateRule, PredicateGroup)
        ):
            raise QueryError("predicate must be a typed predicate when supplied.")
        if not seq(self.requested_endpoints) or not self.requested_endpoints:
            raise QueryError("requested_endpoints must be a non-empty array.")
        requested = tuple(
            machine_ident(value, f"requested_endpoints[{index}]")
            for index, value in enumerate(self.requested_endpoints)
        )
        if len(requested) != len(set(requested)):
            raise QueryError("requested_endpoints must contain unique names.")
        from .endpoint_catalogue import endpoint_catalogue_identity, resolve_endpoint_specs

        catalogue = mapping(self.endpoint_catalogue, "endpoint_catalogue")
        if set(catalogue) != {"contract", "catalogue_id", "sha256"}:
            raise QueryError("endpoint_catalogue has unsupported or missing fields.")
        expected_catalogue = endpoint_catalogue_identity()
        if dict(catalogue) != expected_catalogue:
            raise QueryError(
                "endpoint_catalogue identity does not match the installed package catalogue."
            )
        resolve_endpoint_specs(requested)
        if self.import_provenance is not None and not isinstance(
            self.import_provenance, QueryImportProvenance
        ):
            raise QueryError("import_provenance must be QueryImportProvenance when supplied.")
        if self.source_identity is not None and (
            not isinstance(self.source_identity, str) or not self.source_identity.strip()
        ):
            raise QueryError("source_identity must be a non-empty string when supplied.")
        object.__setattr__(self, "query_id", self.query_id.strip())
        object.__setattr__(self, "requested_endpoints", requested)
        object.__setattr__(self, "endpoint_catalogue", MappingProxyType(dict(catalogue)))
        object.__setattr__(self, "assumptions", texts(self.assumptions, "assumptions"))
        object.__setattr__(self, "warnings", texts(self.warnings, "warnings"))

    @classmethod
    def from_mapping(cls, v):
        r = mapping(v, "query")
        keys(
            r,
            {
                "contract",
                "query_id",
                "requested_endpoints",
                "endpoint_catalogue",
                "predicate",
                "import_provenance",
                "source_identity",
                "assumptions",
                "warnings",
            },
            "query",
        )
        required = {"contract", "query_id", "requested_endpoints", "endpoint_catalogue"}
        if required - set(r):
            raise QueryError(f"query is missing required key(s): {sorted(required - set(r))}.")
        if r["contract"] != COHORT_QUERY_CONTRACT:
            if r["contract"] == "vcgen.cohort_query.v4":
                raise QueryError(
                    "unsupported contract 'vcgen.cohort_query.v4'; v4 query documents are not supported."
                )
            raise QueryError(
                f"unsupported contract {r['contract']!r}; expected {COHORT_QUERY_CONTRACT!r}."
            )
        return cls(
            r["query_id"],
            tuple(r["requested_endpoints"]) if seq(r["requested_endpoints"]) else r["requested_endpoints"],
            r["endpoint_catalogue"],
            None if r.get("predicate") is None else parse_predicate(r["predicate"]),
            None
            if r.get("import_provenance") is None
            else QueryImportProvenance.from_mapping(r["import_provenance"]),
            r.get("source_identity"),
            r.get("assumptions", ()),
            r.get("warnings", ()),
            COHORT_QUERY_CONTRACT,
        )

    def to_canonical_dict(self):
        o = {
            "contract": self.contract,
            "query_id": self.query_id,
            "requested_endpoints": list(self.requested_endpoints),
            "endpoint_catalogue": dict(self.endpoint_catalogue),
        }
        if self.predicate is not None:
            o["predicate"] = self.predicate.to_canonical_dict()
        if self.import_provenance is not None:
            o["import_provenance"] = self.import_provenance.to_canonical_dict()
        if self.source_identity is not None:
            o["source_identity"] = self.source_identity
        for k in ("assumptions", "warnings"):
            if getattr(self, k):
                o[k] = list(getattr(self, k))
        return o

    @property
    def resolved_endpoints(self) -> tuple[EndpointSpec, ...]:
        """Resolve the ordered request set through the installed catalogue."""

        from .endpoint_catalogue import resolve_endpoint_specs

        return resolve_endpoint_specs(self.requested_endpoints)


def parse_query(raw: Mapping[str, Any]) -> Query:
    """Parse one native cohort-query v5 payload."""
    return Query.from_mapping(mapping(raw, "query"))


def normalize_category(v: Any) -> str:
    return text(v, "category", True).upper()


__all__ = [
    "CanonicalOperator",
    "CohortQueryContract",
    "DayWindow",
    "DERIVED_FIELD_TYPES",
    "DerivedFieldName",
    "DerivedField",
    "EndpointSpec",
    "FieldRef",
    "ObservationContract",
    "PreStudyEndpointField",
    "Predicate",
    "PredicateGroup",
    "PredicateGroupOperator",
    "PredicateRule",
    "Query",
    "QueryError",
    "QueryImportProvenance",
    "SendColumnField",
    "SpecimenMatch",
    "ValueType",
    "normalize_category",
    "normalize_operator",
    "parse_field",
    "parse_predicate",
    "parse_query",
]
