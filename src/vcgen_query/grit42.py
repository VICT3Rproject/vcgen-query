"""Fail-closed Grit42 subject-query import compiler.

``condition_string`` is intentionally never parsed or executed.  The retained
raw-payload SHA-256 includes it only as opaque import provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .contract import Query, QueryError, parse_query
from .identities import (
    COHORT_QUERY_CONTRACT,
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
)
from .endpoint_catalogue import endpoint_catalogue_identity, load_endpoint_catalogue

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Z][A-Z0-9_]*)?$")
_TYPES = {
    "string": "string",
    "text": "string",
    "select": "string",
    "multiselect": "string",
    "number": "number",
    "date": "datetime",
    "datetime": "datetime",
}
_OPS = {
    "eq": "eq",
    "equal": "eq",
    "equals": "eq",
    "select_equals": "eq",
    "ne": "ne",
    "not_equal": "ne",
    "not_equals": "ne",
    "select_not_equals": "ne",
    "in": "in",
    "select_any_in": "in",
    "like": "like",
    "not_like": "not_like",
    "not_in": "not_in",
    "select_not_any_in": "not_in",
    "contains": "contains",
    "not_contains": "not_contains",
    "starts_with": "starts_with",
    "not_starts_with": "not_starts_with",
    "ends_with": "ends_with",
    "not_ends_with": "not_ends_with",
    "gt": "gt",
    "greater": "gt",
    "greater_than": "gt",
    "gte": "gte",
    "greater_or_equal": "gte",
    "greater_or_equal_than": "gte",
    "lt": "lt",
    "less": "lt",
    "less_than": "lt",
    "lte": "lte",
    "less_or_equal": "lte",
    "less_or_equal_than": "lte",
    "between": "between",
    "not_between": "not_between",
    "is_empty": "is_empty",
    "is_not_empty": "is_not_empty",
}
_VALUELESS = frozenset({"is_empty", "is_not_empty"})
_LIST_OPS = frozenset({"in", "not_in"})
_RANGE_OPS = frozenset({"between", "not_between"})

_FORM_CATEGORIES = {
    "splrnam": "supplier",
    "sstyp": "study_type",
    "tstfnam": "test_facility",
    "glpfl": "glp_status",
    "species": "species",
    "sex": "sex",
    "strain": "strain",
    "route": "route",
    "dose_frequency": "dose_frequency",
    "trtv": "treatment_vehicle",
}
_FORM_RANGES = {
    "planned_dose_duration_days": (
        "dose_duration_min",
        "dose_duration_max",
        "planned_dose_duration_days",
    ),
    "study_start_year": (
        "study_start_year_min",
        "study_start_year_max",
        "study_start_year",
    ),
    "age_days": ("age_init_min", "age_init_max", "age_days"),
    "PRE_STUDY_BODY_TEMPERATURE_C": (
        "bodytemp_init_min",
        "bodytemp_init_max",
        "PRE_STUDY_BODY_TEMPERATURE_C",
    ),
}
_FORM_BW = {
    "male": ("bw_init_min_male", "bw_init_max_male", "M"),
    "female": ("bw_init_min_female", "bw_init_max_female", "F"),
}
_FORM_ENDPOINTS = {
    name: (f"{prefix}_init_min", f"{prefix}_init_max")
    for name, prefix in {
        "LB_UREA": "urea",
        "LB_GGT": "gamma",
        "LB_ALT": "alanine",
        "LB_ALP": "alkaline",
        "LB_AST": "aspartate",
        "LB_GLDH": "glutamate",
        "LB_HCT": "hematocrit",
        "LB_HGB": "hemoglobin",
        "LB_WBC": "leukocytes",
        "LB_LYM": "lymphocyte",
        "LB_RBC": "erythrocytes",
        "LB_PLAT": "thrombocytes",
        "LB_RETI": "reticulocytes",
    }.items()
}
_INACTIVE = frozenset({"ecg_init_min", "ecg_init_max", "lbtest", "pretestvalues", "trtvcat"})
_DERIVED = {
    **{name: "string" for name in _FORM_CATEGORIES.values()},
    "planned_dose_duration_days": "number",
    "study_start_year": "number",
    "baseline_body_weight_g": "number",
    "age_days": "number",
}


def import_grit42_subject_query(
    raw: Mapping[str, Any], requested_endpoints: Sequence[str]
) -> Query:
    """Import one complete saved Grit42 form or advanced subject query."""

    payload = _mapping(raw, "grit42 subject query")
    if payload.get("query_type") == "form":
        return import_grit42_form_subject_query(payload, requested_endpoints)
    return import_grit42_advanced_subject_query(payload, requested_endpoints)


def import_grit42_form_subject_query(
    raw: Mapping[str, Any], requested_endpoints: Sequence[str]
) -> Query:
    """Import one complete saved Grit42 form subject query."""

    payload = _mapping(raw, "grit42 form subject query")
    if payload.get("query_type") != "form":
        raise QueryError("grit42 form subject query requires query_type 'form'")
    if "form_values" not in payload:
        raise QueryError("grit42 form subject query requires form_values")
    return _compile_form_values(
        _mapping(payload["form_values"], "grit42 saved form_values"),
        raw_payload=payload,
        requested_endpoints=requested_endpoints,
    )


def import_grit42_advanced_subject_query(
    raw: Mapping[str, Any], requested_endpoints: Sequence[str]
) -> Query:
    """Import one complete saved Grit42 advanced subject query."""

    payload = _mapping(raw, "grit42 advanced subject query")
    if payload.get("query_type") == "form":
        raise QueryError("grit42 advanced subject query rejects query_type 'form'")

    inventory = _inventory(payload.get("columns"))
    if "condition_tree" not in payload:
        raise QueryError("grit42 subject query requires condition_tree")
    endpoints: set[str] = set()
    predicate = _node(payload["condition_tree"], inventory, endpoints, "condition_tree")
    digest = _raw_hash(payload)
    return _query(
        _identity(payload, digest),
        predicate,
        digest,
        GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
        endpoints,
        requested_endpoints,
    )


def _compile_form_values(
    payload: Mapping[str, Any],
    *,
    raw_payload: Mapping[str, Any],
    requested_endpoints: Sequence[str],
) -> Query:
    """Compile supported fixed-form selectors with the given raw provenance."""

    supported = {
        *_FORM_CATEGORIES,
        *(x for value in _FORM_RANGES.values() for x in value[:2]),
        *(x for value in _FORM_BW.values() for x in value[:2]),
        *(x for value in _FORM_ENDPOINTS.values() for x in value),
        *_INACTIVE,
    }
    unknown = sorted(
        key for key, value in payload.items() if key not in supported and _active(value)
    )
    if unknown:
        raise QueryError(f"unsupported active grit42 form selector(s): {unknown}")
    blocked = sorted(key for key in _INACTIVE if _active(payload.get(key)))
    if blocked:
        raise QueryError(f"unsupported active grit42 form selector(s): {blocked}")
    rules: list[dict[str, Any]] = []
    endpoints: set[str] = set()
    for source, name in _FORM_CATEGORIES.items():
        if _active(payload.get(source)):
            rules.append(_rule(_derived(name, "string"), "in", _strings(payload[source], source)))
    for endpoint, (low, high, derived_name) in _FORM_RANGES.items():
        if endpoint == "PRE_STUDY_BODY_TEMPERATURE_C":
            field = _endpoint(endpoint, "number")
            if _active(payload.get(low)) or _active(payload.get(high)):
                endpoints.add(endpoint)
        else:
            field = _derived(derived_name, "number")
        rules.extend(_bounds(field, payload.get(low), payload.get(high), low, high))
    for endpoint, (low, high) in _FORM_ENDPOINTS.items():
        endpoints.add(endpoint) if _active(payload.get(low)) or _active(payload.get(high)) else None
        rules.extend(
            _bounds(
                _endpoint(endpoint, "number"),
                payload.get(low),
                payload.get(high),
                low,
                high,
            )
        )
    sex_groups = []
    for _, (low, high, sex) in _FORM_BW.items():
        bounds = _bounds(
            _derived("baseline_body_weight_g", "number"),
            payload.get(low),
            payload.get(high),
            low,
            high,
        )
        if bounds:
            sex_groups.append(_group("and", [_rule(_derived("sex", "string"), "eq", sex), *bounds]))
    if sex_groups:
        rules.append(_group("or", sex_groups))
    digest = _raw_hash(raw_payload)
    return _query(
        _identity(raw_payload, digest),
        _group("and", rules) if rules else None,
        digest,
        GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
        endpoints,
        requested_endpoints,
    )


def compare_membership(
    local_membership: Iterable[Any], reference_membership: Iterable[Any]
) -> dict[str, Any]:
    """Return deterministic hashes and exact membership-key differences."""
    local, reference = (
        set(_keys(local_membership, "local_membership")),
        set(_keys(reference_membership, "reference_membership")),
    )
    missing, extra = tuple(sorted(reference - local)), tuple(sorted(local - reference))
    return {
        "local_count": len(local),
        "reference_count": len(reference),
        "local_hash": _key_hash(local),
        "reference_hash": _key_hash(reference),
        "missing_keys": missing,
        "extra_keys": extra,
        "matches": not missing and not extra,
    }


def _query(
    query_id: str,
    predicate: dict[str, Any] | None,
    source_payload_sha256: str,
    source_contract: str,
    endpoints: set[str],
    requested_endpoints: Sequence[str],
) -> Query:
    raw: dict[str, Any] = {
        "contract": COHORT_QUERY_CONTRACT,
        "query_id": query_id,
        "requested_endpoints": list(requested_endpoints),
        "endpoint_catalogue": endpoint_catalogue_identity(),
        "import_provenance": {
            "contract": GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
            "source_contract": source_contract,
            "source_payload_sha256": source_payload_sha256,
        },
    }
    if predicate is not None:
        raw["predicate"] = predicate
    return parse_query(raw)


def _node(
    raw: Any,
    inventory: Mapping[str, str | None],
    endpoints: set[str],
    location: str,
) -> dict[str, Any]:
    node = _mapping(raw, location)
    if node.get("type") == "rule":
        return _advanced_rule(node, inventory, endpoints, location)
    if node.get("type") != "group":
        raise QueryError(f"{location}.type must be 'group' or 'rule'")
    props = _mapping(node.get("properties"), f"{location}.properties")
    conjunction, negated = props.get("conjunction"), props.get("not", False)
    if (
        not isinstance(conjunction, str)
        or conjunction.strip().lower() not in {"and", "or"}
        or not isinstance(negated, bool)
    ):
        raise QueryError(f"{location} has invalid group properties")
    left, right = "children" in node, "children1" in node
    if left == right:
        raise QueryError(f"{location} must contain exactly one of children or children1")
    source = node["children"] if left else node["children1"]
    children = (
        list(source.values())
        if isinstance(source, Mapping)
        else list(source)
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes))
        else None
    )
    if not children:
        raise QueryError(f"{location} must contain at least one child")
    return _group(
        conjunction.strip().lower(),
        [
            _node(child, inventory, endpoints, f"{location}.children[{i}]")
            for i, child in enumerate(children)
        ],
        negated=negated,
    )


def _advanced_rule(
    node: Mapping[str, Any],
    inventory: Mapping[str, str | None],
    endpoints: set[str],
    location: str,
) -> dict[str, Any]:
    props = _mapping(node.get("properties"), f"{location}.properties")
    name = _canonical_field_name(props.get("field"), location)
    if name not in inventory:
        raise QueryError(f"{location}.properties.field must be a columns-inventory field")
    operator = _operator(props.get("operator"), location)
    requested_type = _type(
        props.get("valueType"),
        location,
        default="string" if operator in _VALUELESS else None,
    )
    inventory_type = inventory[name]
    if inventory_type is not None and inventory_type != requested_type:
        raise QueryError(f"{location}.properties.valueType does not match columns inventory")
    _value_source(
        props.get("valueSrc"),
        location,
        allow_empty=operator in _VALUELESS,
    )
    if operator in {"like", "not_like"}:
        operator = _literal_like_operator(
            props.get("value"),
            requested_type,
            location,
            negated=operator == "not_like",
        )
    field = _field(name, endpoints, requested_type, location)
    if field["value_type"] != requested_type:
        raise QueryError(f"{location}.properties.valueType does not match field type")
    return _rule(field, operator, _value(props.get("value"), operator, requested_type, location))


def _canonical_field_name(raw: Any, location: str) -> str:
    """Normalize Grit42 predicate-query field IDs to a safe ``DOMAIN.COLUMN`` reference."""

    if not isinstance(raw, str) or not raw.strip():
        raise QueryError(f"{location}.properties.field must be a non-empty string")
    name = raw.strip()
    if name.startswith("send_domain_"):
        domain_and_column = name.removeprefix("send_domain_").split(".", 1)
        if len(domain_and_column) != 2:
            raise QueryError(f"{location}.properties.field must contain domain and column")
        name = ".".join(item.upper() for item in domain_and_column)
    if name not in _DERIVED and not _is_endpoint(name) and not _IDENTIFIER.fullmatch(name):
        raise QueryError(f"{location}.properties.field must be a safe DOMAIN.COLUMN field")
    return name


def _field(name: str, endpoints: set[str], requested_type: str, location: str) -> dict[str, str]:
    if name in _DERIVED:
        return _derived(name, _DERIVED[name])
    if _is_endpoint(name):
        endpoints.add(name)
        return _endpoint(name, "number")
    if not _IDENTIFIER.fullmatch(name) or "." not in name:
        raise QueryError(
            f"{location}.properties.field must be DOMAIN.COLUMN or supported semantic field"
        )
    domain, column = name.split(".", 1)
    return {
        "kind": "send_column",
        "domain": domain,
        "column": column,
        "value_type": requested_type,
    }


def _is_endpoint(name: str) -> bool:
    return name in load_endpoint_catalogue().endpoints


def _type(
    raw: Any,
    location: str,
    *,
    default: str | None = None,
) -> str:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if not raw and default is not None:
            return default
        if not raw or any(item != raw[0] for item in raw[1:]):
            raise QueryError(f"{location}.properties.valueType must identify one type")
        raw = raw[0]
    if raw is None and default is not None:
        return default
    if not isinstance(raw, str) or raw.strip().lower() not in _TYPES:
        raise QueryError(f"{location}.properties.valueType is unsupported")
    return _TYPES[raw.strip().lower()]


def _value_source(raw: Any, location: str, *, allow_empty: bool = False) -> None:
    if raw is None:
        return
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if not raw and allow_empty:
            return
        if not raw or any(item != raw[0] for item in raw[1:]):
            raise QueryError(f"{location}.properties.valueSrc must identify one source")
        raw = raw[0]
    if raw != "value":
        raise QueryError(f"{location}.properties.valueSrc must be 'value' when supplied")


def _operator(raw: Any, location: str) -> str:
    if not isinstance(raw, str) or raw.strip().lower() not in _OPS:
        raise QueryError(f"{location}.properties.operator is unsupported")
    return _OPS[raw.strip().lower()]


def _literal_like_operator(
    raw: Any,
    value_type: str,
    location: str,
    *,
    negated: bool,
) -> str:
    """Map Grit42's wildcard-free Contains operators to canonical string operators."""

    if value_type != "string":
        raise QueryError(f"{location}.properties.like requires a string valueType")
    values = list(raw) if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else [raw]
    if len(values) != 1 or not isinstance(values[0], str):
        raise QueryError(f"{location}.properties.like must contain exactly one string value")
    if "%" in values[0] or "_" in values[0]:
        raise QueryError(f"{location}.properties.like wildcard patterns are unsupported")
    return "not_contains" if negated else "contains"


def _value(raw: Any, operator: str, value_type: str, location: str) -> Any:
    if operator in _VALUELESS:
        if raw not in (None, "", (), []):
            raise QueryError(f"{location}.properties.value must be absent for {operator}")
        return None
    values = list(raw) if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else [raw]
    if operator in _LIST_OPS:
        if (
            len(values) == 1
            and isinstance(values[0], Sequence)
            and not isinstance(values[0], (str, bytes))
        ):
            values = list(values[0])
        if not values:
            raise QueryError(f"{location}.properties.value must not be empty for {operator}")
        return [_typed(value, value_type, location) for value in values]
    if operator in _RANGE_OPS:
        if len(values) != 2:
            raise QueryError(f"{location}.properties.value must contain two values for {operator}")
        return [_typed(value, value_type, location) for value in values]
    if len(values) != 1:
        raise QueryError(f"{location}.properties.value must contain one value for {operator}")
    return _typed(values[0], value_type, location)


def _typed(value: Any, value_type: str, location: str) -> str | float:
    if value_type == "string":
        if not isinstance(value, str) or not value:
            raise QueryError(f"{location}.properties.value must be a non-empty string")
        return value
    if value_type == "number":
        return _number(value, f"{location}.properties.value")
    if not isinstance(value, str):
        raise QueryError(f"{location}.properties.value must be a supported ISO timestamp")
    timestamp = _normalize_timestamp(value)
    if timestamp is None:
        raise QueryError(f"{location}.properties.value must be a supported ISO timestamp")
    return timestamp


def _normalize_timestamp(value: str) -> str | None:
    minute = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(Z|[+-]\d{2}:\d{2})?",
        value,
    )
    if minute is not None:
        return f"{minute.group(1)}:00{minute.group(2) or ''}"
    return value if _timestamp(value) else None


def _timestamp(value: str) -> bool:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?",
        value,
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _group(
    group: str,
    children: Sequence[dict[str, Any]],
    *,
    negated: bool = False,
) -> dict[str, Any]:
    if group not in {"and", "or"}:
        raise QueryError("predicate group must use and or or")
    if not children:
        raise QueryError("predicate group must contain at least one child")
    result: dict[str, Any] = {"group": group, "children": list(children)}
    if negated:
        result["negated"] = True
    return result


def _rule(field: Mapping[str, str], operator: str, value: Any) -> dict[str, Any]:
    result = {"field": dict(field), "operator": operator}
    if operator not in _VALUELESS:
        result["value"] = value
    return result


def _derived(name: str, value_type: str) -> dict[str, str]:
    return {"kind": "derived", "name": name, "value_type": value_type}


def _endpoint(endpoint: str, value_type: str) -> dict[str, str]:
    return {
        "kind": "pre_study_endpoint",
        "endpoint": endpoint,
        "value_type": value_type,
    }


def _bounds(
    field: dict[str, str], low: Any, high: Any, low_name: str, high_name: str
) -> list[dict[str, Any]]:
    if _active(low) and _active(high):
        return [
            _rule(
                field,
                "between",
                [_number(low, low_name), _number(high, high_name)],
            )
        ]
    result = []
    if _active(low):
        result.append(_rule(field, "gte", _number(low, low_name)))
    if _active(high):
        result.append(_rule(field, "lte", _number(high, high_name)))
    return result


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise QueryError(f"{field} must be a non-empty array of strings")
    return sorted(set(_typed(item, "string", field) for item in value))


def _inventory(raw: Any) -> dict[str, str | None]:
    entries: list[tuple[str, str | None]] = []
    if isinstance(raw, Mapping):
        if _is_domain_column_inventory(raw):
            for domain, columns in raw.items():
                if (
                    not isinstance(domain, str)
                    or not _IDENTIFIER.fullmatch(domain.upper())
                    or not isinstance(columns, Sequence)
                    or isinstance(columns, (str, bytes))
                    or not columns
                ):
                    raise QueryError("columns inventory contains an invalid domain entry")
                for index, column in enumerate(columns):
                    if not isinstance(column, str) or not column.strip():
                        raise QueryError(f"columns[{domain!r}][{index}] must be a non-empty column")
                    entries.append(
                        (
                            _canonical_field_name(
                                f"send_domain_{domain}.{column}",
                                f"columns[{domain!r}][{index}]",
                            ),
                            None,
                        )
                    )
        else:
            entries.extend(
                (
                    _canonical_field_name(name, f"columns[{name!r}]"),
                    _inventory_value_type(metadata, f"columns[{name!r}]"),
                )
                for name, metadata in raw.items()
            )
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                entries.append((_canonical_field_name(item, f"columns[{index}]"), None))
            elif isinstance(item, Mapping):
                entries.append(
                    (
                        _canonical_field_name(
                            item.get("field"),
                            f"columns[{index}]",
                        ),
                        _inventory_value_type(item, f"columns[{index}]"),
                    )
                )
            else:
                raise QueryError("columns inventory contains an invalid entry")
    else:
        raise QueryError("grit42 subject query requires columns inventory")
    if not entries:
        raise QueryError("grit42 subject query requires columns inventory")

    inventory: dict[str, str | None] = {}
    for name, value_type in entries:
        previous_type = inventory.get(name)
        if previous_type is not None and value_type is not None and previous_type != value_type:
            raise QueryError(f"columns inventory contains conflicting types for {name!r}")
        inventory[name] = value_type or previous_type
    return inventory


def _is_domain_column_inventory(raw: Mapping[Any, Any]) -> bool:
    """Return whether Grit42 serialized ``columns`` as DOMAIN -> [column]."""

    return bool(raw) and all(
        isinstance(domain, str)
        and "." not in domain
        and isinstance(columns, Sequence)
        and not isinstance(columns, (str, bytes))
        for domain, columns in raw.items()
    )


def _inventory_value_type(metadata: Any, location: str) -> str | None:
    if not isinstance(metadata, Mapping) or "valueType" not in metadata:
        return None
    return _type(metadata["valueType"], location)


def _identity(payload: Mapping[str, Any], digest: str) -> str:
    for key in ("subject_query_id", "query_id", "id"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            return f"grit42:subject:{str(value).strip()}"
    return f"grit42:subject:{digest[:16]}"


def _raw_hash(payload: Mapping[str, Any]) -> str:
    try:
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise QueryError("grit42 import payload must be JSON-serializable") from error
    return hashlib.sha256(data).hexdigest()


def _keys(values: Iterable[Any], location: str) -> tuple[tuple[str, str], ...]:
    columns = getattr(values, "columns", None)
    rows = getattr(values, "itertuples", None)
    if columns is not None and callable(rows):
        lookup: dict[str, Any] = {}
        for column in columns:
            normalized = str(column).upper()
            if normalized in lookup:
                raise QueryError(f"{location} contains duplicate identity columns")
            lookup[normalized] = column
        study_column = lookup.get("STUDYID", lookup.get("STUDY_ID"))
        subject_column = lookup.get("USUBJID", lookup.get("SUBJECT_ID"))
        if study_column is None or subject_column is None:
            raise QueryError(
                f"{location} DataFrame must contain STUDYID/USUBJID or study_id/subject_id columns"
            )
        values = values[[study_column, subject_column]].itertuples(index=False, name=None)
    elif isinstance(values, Mapping):
        raise QueryError(f"{location} must contain subject-key rows, not mapping keys")
    if isinstance(values, (str, bytes)):
        raise QueryError(f"{location} must be an iterable of subject keys, not a string")
    try:
        return tuple(_key(value, location) for value in values)
    except TypeError as error:
        raise QueryError(f"{location} must be iterable") from error


def _key(value: Any, location: str) -> tuple[str, str]:
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
        and all(isinstance(part, str) and part for part in value)
    ):
        return value[0], value[1]
    raise QueryError(f"{location} contains an invalid subject key")


def _key_hash(keys: set[tuple[str, str]]) -> str:
    payload = json.dumps(
        sorted(keys),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QueryError(f"{location} must be an object")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise QueryError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise QueryError(f"{field} must be a finite number") from error
    if not math.isfinite(result):
        raise QueryError(f"{field} must be a finite number")
    return result


def _active(value: Any) -> bool:
    return value not in (None, "", (), [], {})


__all__ = [
    "compare_membership",
    "import_grit42_advanced_subject_query",
    "import_grit42_form_subject_query",
    "import_grit42_subject_query",
]
