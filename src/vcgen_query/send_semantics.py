"""Central SEND-like column and endpoint parsing helpers.

This module is a dependency-free port of the host project's SEND semantics. It
classifies raw SEND result cells, derives measurement identities, and selects
baseline records deterministically. It depends only on the standard library so
that the contract and parsing layers remain usable without DuckDB or pandas.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

ResultState: TypeAlias = Literal[
    "numeric_observed",
    "explicitly_failed",
    "censored_observed",
    "qualitative_observed",
    "silently_absent",
]
RESULT_STATES: frozenset[ResultState] = frozenset(
    {
        "numeric_observed",
        "explicitly_failed",
        "censored_observed",
        "qualitative_observed",
        "silently_absent",
    }
)

DAY_COLUMN_SUFFIXES = (
    "DY",
    "NOMDY",
    "STDY",
    "ENDY",
    "RPDY",
    "RPSTDY",
    "RPENDY",
    "RPPLDY",
    "RPPLSTDY",
    "RPPLENDY",
)
VALUE_COLUMN_SUFFIXES = (
    "STRESN",
    "STRESC",
    "ORRES",
    "ORRESU",
    "STRESU",
)
QUALIFIER_SUFFIXES = (
    "SPEC",
    "LOC",
    "LAT",
    "PORTOT",
    "CAT",
    "SCAT",
    "METHOD",
    "EVAL",
)


@dataclass(frozen=True)
class ParsedResult:
    """One SEND result with paired value/unit provenance and disposition state."""

    state: ResultState
    numeric_value: float | None
    raw_value: str | None
    value_column: str | None
    unit: str | None
    unit_column: str | None
    excluded: bool
    exclusion_reason: str | None
    failure_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in RESULT_STATES:
            raise ValueError(f"unsupported SEND result state: {self.state!r}")


_CENSORED_RESULT = re.compile(
    r"^\s*(?:<|>|<=|>=|≤|≥|BLQ\b|BQL\b|BELOW\b|ABOVE\b)",
    re.IGNORECASE,
)

_MISSING_CATEGORIES = {
    "N/A",
    "NA",
    "NO DATA",
    "NOT APPLICABLE",
    "NOT AVAILABLE",
    "NOT PROVIDED",
    "UNKNOWN",
}
_METHOD_TOKEN_ALIASES = {
    "COLOURIMETRIC": "COLORIMETRIC",
    "FLORESCENT": "FLUORESCENT",
    "IMMUNOTURBIOMETRIC": "IMMUNOTURBIDIMETRIC",
    "TURBIOMETRIC": "TURBIDIMETRIC",
    "TURBIDOMETRIC": "TURBIDIMETRIC",
    "TURBIMETRIC": "TURBIDIMETRIC",
}
def test_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return endpoint/test identifier columns in SEND-like priority order."""

    return tuple(column for column in columns if column.endswith(("TESTCD", "TEST")))


def day_columns(domain: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return candidate study-day columns for a source domain."""

    domain = domain.upper()
    preferred = (
        f"{domain}DY",
        f"{domain}NOMDY",
        "VISITDY",
        "RPPLDY",
        f"{domain}RPDY",
        f"{domain}STDY",
        f"{domain}ENDY",
        "RPPLSTDY",
        "RPPLENDY",
    )
    found = [column for column in preferred if column in columns]
    found.extend(
        column
        for column in columns
        if column not in found and any(column.endswith(suffix) for suffix in DAY_COLUMN_SUFFIXES)
    )
    return tuple(found)


def baseline_flag_columns(domain: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return SEND baseline-flag columns in deterministic priority order."""

    domain = domain.upper()
    preferred = (f"{domain}BLFL", "BLFL")
    return tuple(column for column in preferred if column in columns)


def is_explicit_baseline(row: Mapping[str, Any], columns: tuple[str, ...]) -> bool:
    """Return whether one factual row carries a normalized baseline flag."""

    return any(normalize_flag(row.get(column)) == "Y" for column in columns)


def value_columns(domain: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return candidate result-value columns for a source domain."""

    domain = domain.upper()
    preferred = (
        f"{domain}STRESN",
        f"{domain}ORRES",
        f"{domain}STRESC",
        f"{domain}ORRESU",
        f"{domain}STRESU",
    )
    found = [column for column in preferred if column in columns]
    found.extend(
        column
        for column in columns
        if column not in found and any(column.endswith(suffix) for suffix in VALUE_COLUMN_SUFFIXES)
    )
    return tuple(found)


def parse_result(
    domain: str,
    row: Mapping[str, Any],
    columns: tuple[str, ...],
) -> ParsedResult:
    """Classify a raw result independently from whether it is numerically usable.

    Standardized and original values are paired with their corresponding unit
    columns. Exclusion flags are retained as disposition metadata and prevent a
    source-identified duplicate from entering the numeric modeling cell.
    """

    domain = domain.upper()
    excluded = any(
        normalize_flag(row.get(column)) == "Y"
        for column in _present_columns(
            columns,
            (f"{domain}EXCLFL", "EXCLFL"),
        )
    )
    exclusion_reason = _first_normalized(
        row,
        _present_columns(
            columns,
            (f"{domain}REASEX", f"{domain}EXCLRSN", "REASEX", "EXCLRSN"),
        ),
    )
    status = _first_normalized(
        row,
        _present_columns(columns, (f"{domain}STAT", "STAT")),
    )
    failure_reason = _first_normalized(
        row,
        _present_columns(
            columns,
            (f"{domain}REASND", f"{domain}REASON", "REASND", "REASON"),
        ),
    )

    numeric_candidates = (
        (f"{domain}STRESN", f"{domain}STRESU"),
        (f"{domain}ORRES", f"{domain}ORRESU"),
        (f"{domain}STRESC", f"{domain}STRESU"),
    )
    for value_column, unit_column in numeric_candidates:
        if value_column not in columns:
            continue
        numeric = optional_float(row.get(value_column))
        if numeric is None:
            continue
        unit = normalize_unit(_first_text(row, (unit_column,)))
        resolved_unit_column = unit_column
        if unit is None:
            # Standardized values frequently omit STRESU when ORRESU already
            # carries the standard unit (curated identity conversions). Adopt
            # the original unit label only when the original value is the same
            # number, so a converted value can never inherit the wrong unit.
            for fallback_value_column, fallback_unit_column in numeric_candidates:
                if fallback_unit_column == unit_column:
                    continue
                if fallback_value_column not in columns or fallback_unit_column not in columns:
                    continue
                fallback_unit = normalize_unit(_first_text(row, (fallback_unit_column,)))
                if fallback_unit is None:
                    continue
                fallback_numeric = optional_float(row.get(fallback_value_column))
                if fallback_numeric is None or fallback_numeric != numeric:
                    continue
                unit = fallback_unit
                resolved_unit_column = fallback_unit_column
                break
        return ParsedResult(
            state="numeric_observed",
            numeric_value=float(numeric),
            raw_value=str(row.get(value_column)).strip(),
            value_column=value_column,
            unit=unit,
            unit_column=resolved_unit_column if resolved_unit_column in columns else None,
            excluded=excluded,
            exclusion_reason=exclusion_reason,
            failure_reason=failure_reason,
        )

    if status in {"NOT DONE", "NOT PERFORMED", "FAILED", "FAILURE"}:
        return ParsedResult(
            state="explicitly_failed",
            numeric_value=None,
            raw_value=None,
            value_column=None,
            unit=None,
            unit_column=None,
            excluded=excluded,
            exclusion_reason=exclusion_reason,
            failure_reason=failure_reason or status,
        )

    raw_value, value_column, unit_column = _first_result_text(
        domain,
        row,
        columns,
    )
    if raw_value is not None:
        state = "censored_observed" if _CENSORED_RESULT.match(raw_value) else "qualitative_observed"
        return ParsedResult(
            state=state,
            numeric_value=None,
            raw_value=raw_value,
            value_column=value_column,
            unit=(normalize_unit(_first_text(row, (unit_column,))) if unit_column else None),
            unit_column=unit_column,
            excluded=excluded,
            exclusion_reason=exclusion_reason,
            failure_reason=failure_reason,
        )

    return ParsedResult(
        state="silently_absent",
        numeric_value=None,
        raw_value=None,
        value_column=None,
        unit=None,
        unit_column=None,
        excluded=excluded,
        exclusion_reason=exclusion_reason,
        failure_reason=failure_reason,
    )


def qualifier_columns(domain: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return endpoint qualifier columns for a source domain."""

    domain = domain.upper()
    found = tuple(
        column
        for column in columns
        if any(column == f"{domain}{suffix}" for suffix in QUALIFIER_SUFFIXES)
    )
    unit_columns = tuple(
        column
        for column in (f"{domain}STRESU", f"{domain}ORRESU")
        if column in columns and column not in found
    )
    return (*found, *unit_columns)


def measurement_context(
    domain: str,
    row: Mapping[str, Any],
    columns: tuple[str, ...],
    *,
    parsed_result: ParsedResult | None = None,
) -> dict[str, str | None]:
    """Return endpoint identity fields plus laboratory measurement metadata."""

    domain = domain.upper()
    context = {
        "domain": domain,
        "test_code": _first_normalized(row, (f"{domain}TESTCD", "TESTCD")),
        "test_name": _first_normalized(row, (f"{domain}TEST", "TEST")),
        "specimen": _first_normalized(row, (f"{domain}SPEC", "SPEC")),
        "unit": (
            parsed_result.unit
            if parsed_result is not None
            else normalize_unit(
                _first_text(
                    row,
                    (
                        f"{domain}STRESU",
                        f"{domain}ORRESU",
                        "STRESU",
                        "ORRESU",
                    ),
                )
            )
        ),
        "method": normalize_method(_first_text(row, (f"{domain}METHOD", "METHOD"))),
        "laterality": (_first_normalized(row, (f"{domain}LAT", "LAT")) if domain == "OM" else None),
        "portion_totality": (
            _first_normalized(row, (f"{domain}PORTOT", "PORTOT")) if domain == "OM" else None
        ),
        "fasting_status": None,
        "laboratory_name": None,
    }
    if domain == "LB":
        context["fasting_status"] = normalize_flag(
            _first_text(row, ("LBFAST", "LBFASTING", "FASTING", "FAST"))
        )
        context["laboratory_name"] = normalize_human_category(
            _first_text(row, ("LBNAM", "LBLAB", "LABNAME", "LAB"))
        )
    return context


def measurement_identity(
    domain: str,
    row: Mapping[str, Any],
    columns: tuple[str, ...],
    *,
    parsed_result: ParsedResult | None = None,
    canonical_test_code: str | None = None,
) -> str:
    """Return a canonical measurement identity for compatibility checks."""

    context = measurement_context(
        domain,
        row,
        columns,
        parsed_result=parsed_result,
    )
    if canonical_test_code:
        context["test_code"] = normalize_value(canonical_test_code)
    fields = ["domain", "test_code", "specimen", "unit", "method"]
    if domain.upper() == "OM":
        fields.extend(("laterality", "portion_totality"))
    return "|".join(f"{field}={context.get(field) or 'UNKNOWN'}" for field in fields)


def source_order_key(
    domain: str,
    row: Mapping[str, Any],
    *,
    stable_index: int,
) -> tuple[str, float, str, int]:
    """Return deterministic SEND source order using timestamp, sequence, then row order."""

    domain = domain.upper()
    timestamp = (
        _first_text(
            row,
            (
                f"{domain}DTC",
                f"{domain}STDTC",
                f"{domain}ENDTC",
                "DTC",
            ),
        )
        or ""
    )
    sequence_text = _first_text(row, (f"{domain}SEQ", "SEQ")) or ""
    sequence_number = optional_float(sequence_text)
    return (
        normalize_value(timestamp) if timestamp else "",
        float(sequence_number) if sequence_number is not None else float("-inf"),
        normalize_value(sequence_text) if sequence_text else "",
        int(stable_index),
    )


def endpoint_key(
    domain: str,
    row: Mapping[str, Any],
    columns: tuple[str, ...],
    qualifiers: tuple[str, ...],
) -> str:
    """Return a stable endpoint key from test and qualifier columns."""

    domain = domain.upper()
    primary_column = next(
        (
            column
            for column in (f"{domain}TESTCD", f"{domain}TEST", "TESTCD", "TEST")
            if column in columns and present(row.get(column))
        ),
        None,
    )
    primary = normalize_value(row.get(primary_column)) if primary_column else domain
    qualifier_parts = [
        f"{column.removeprefix(domain)}={normalize_value(row.get(column))}"
        for column in qualifiers
        if present(row.get(column))
    ]
    suffix = "|" + "|".join(qualifier_parts) if qualifier_parts else ""
    return f"{domain}:{primary}{suffix}"


def first_numeric_day(row: Mapping[str, Any], columns: tuple[str, ...]) -> int | None:
    """Return the first parseable day according to the supplied priority list."""

    for column in columns:
        value = optional_int(row.get(column))
        if value is not None:
            return value
    return None


def optional_int(value: Any) -> int | None:
    """Parse an integer-ish SEND value, returning ``None`` for blanks/non-numeric."""

    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


def optional_float(value: Any) -> float | None:
    """Parse a finite numeric SEND value, returning ``None`` when unavailable."""

    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def present(value: Any) -> bool:
    """Return whether a raw value is semantically present."""

    return value is not None and str(value).strip() != ""


def normalize_value(value: Any) -> str:
    """Normalize a raw endpoint string for stable keys."""

    return _normalize_text(value) if present(value) else "UNKNOWN"


def normalize_human_category(value: Any) -> str | None:
    """Canonicalize formatting without guessing semantic equivalence."""

    if not present(value):
        return None
    normalized = _normalize_text(value)
    return None if normalized in _MISSING_CATEGORIES else normalized


def normalize_method(value: Any) -> str | None:
    """Conservatively normalize a human-entered measurement method label.

    This handles formatting and known lexical spelling variants only. Learned
    or edit-distance category merging must be fit on training folds rather than
    applied globally while parsing.
    """

    normalized = normalize_human_category(value)
    if normalized is None:
        return None
    normalized = normalized.replace("_", " ")
    normalized = re.sub(r"\s*([/+])\s*", r"\1", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for source, target in _METHOD_TOKEN_ALIASES.items():
        normalized = re.sub(rf"\b{source}\b", target, normalized)
    return normalized


def normalize_unit(value: Any) -> str | None:
    """Preserve an exact unit label and trim surrounding whitespace only."""

    if not present(value):
        return None
    unit = str(value).strip()
    missing_probe = _normalize_text(unit)
    if missing_probe in _MISSING_CATEGORIES or missing_probe == "NO UNITS":
        return None
    return unit


def normalize_flag(value: Any) -> str | None:
    """Normalize common human-entered boolean/flag spellings."""

    normalized = normalize_human_category(value)
    if normalized in {"1", "TRUE", "Y", "YES"}:
        return "Y"
    if normalized in {"0", "FALSE", "N", "NO"}:
        return "N"
    return normalized


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().upper()


def _first_text(row: Mapping[str, Any], columns: tuple[str, ...]) -> str:
    for column in columns:
        if present(row.get(column)):
            return str(row[column]).strip()
    return ""


def _first_normalized(row: Mapping[str, Any], columns: tuple[str, ...]) -> str | None:
    value = _first_text(row, columns)
    return normalize_human_category(value) if value else None


def _present_columns(columns: tuple[str, ...], candidates: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(column for column in candidates if column in columns)


def _first_result_text(
    domain: str,
    row: Mapping[str, Any],
    columns: tuple[str, ...],
) -> tuple[str | None, str | None, str | None]:
    for value_column, unit_column in (
        (f"{domain}ORRES", f"{domain}ORRESU"),
        (f"{domain}STRESC", f"{domain}STRESU"),
    ):
        if value_column not in columns or not present(row.get(value_column)):
            continue
        return (
            str(row[value_column]).strip(),
            value_column,
            unit_column if unit_column in columns else None,
        )
    return None, None, None
