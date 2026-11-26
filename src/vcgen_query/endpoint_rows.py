"""Shared SEND endpoint-row identity and streaming helpers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from operator import itemgetter
from typing import Any

from .contract import EndpointSpec
from .send_columns import STUDY_ID, SUBJECT_ID
from .specimen import specimen_class
from .send_semantics import (
    ParsedResult,
    day_columns,
    normalize_value,
    parse_result,
    qualifier_columns,
    test_columns,
    value_columns,
)


def endpoint_projection_columns(domain: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return columns needed to identify and parse rows for an endpoint."""

    selected: list[str] = []
    for column in (
        STUDY_ID,
        SUBJECT_ID,
        *test_columns(columns),
        *day_columns(domain, columns),
        *value_columns(domain, columns),
        *qualifier_columns(domain, columns),
        f"{domain.upper()}CAT",
        *(
            item
            for item in columns
            if item.endswith(
                (
                    "DTC",
                    "SEQ",
                    "FAST",
                    "FASTING",
                    "NAM",
                    "LAB",
                    "STAT",
                    "REASND",
                    "REASON",
                    "EXCLFL",
                    "REASEX",
                    "EXCLRSN",
                    "BLFL",
                    "POOLID",
                )
            )
        ),
    ):
        if column in columns and column not in selected:
            selected.append(column)
    return tuple(selected)


def endpoint_matches_row(
    endpoint: EndpointSpec,
    row: dict[str, Any],
    columns: tuple[str, ...],
    *,
    specimen_class: str | None = None,
    parsed: ParsedResult | None = None,
) -> bool:
    """Return whether a row has the configured endpoint source identity.

    A class specimen rule compares the specimen class of the row: ``specimen_class``
    when a data decision resolved it, else the vocabulary class of the source label.
    """

    return endpoint_identity_matches(endpoint, row, columns, parsed=parsed) and endpoint_specimen_matches(
        endpoint, row, specimen_class=specimen_class
    )


def endpoint_identity_matches(
    endpoint: EndpointSpec,
    row: dict[str, Any],
    columns: tuple[str, ...],
    *,
    parsed: ParsedResult | None = None,
) -> bool:
    """Return whether a row matches the endpoint test, unit, and method, ignoring specimen.

    ``parsed`` is the row's :func:`parse_result`, when the caller has it. The
    unit rule reads only its state, unit and value column, which depend on the row values
    and not on which other columns the column list names.
    """

    test_codes, test_names, allowed_units, allowed_methods = _endpoint_sets(endpoint)
    code = row_test_code(endpoint.domain, row)
    if code not in test_codes:
        return False
    if test_names and _row_test_name(endpoint.domain, row) not in test_names:
        return False
    if allowed_units:
        if parsed is None:
            parsed = parse_result(endpoint.domain, row, columns)
        parsed_unit = parsed.unit
        if parsed_unit is None and parsed.state == "numeric_observed":
            # An implicit unit describes a measured value, never a missing one.
            parsed_unit = endpoint.implicit_unit
        # A record without a result value (a failed, empty or out-of-range test)
        # has no unit; the test, specimen and method still identify the endpoint.
        unknown_unit = parsed_unit is None and parsed.value_column is None
        if not unknown_unit and (parsed_unit is None or parsed_unit.upper() not in allowed_units):
            return False
    if endpoint.methods:
        method = _row_method(endpoint.domain, row)
        if not method:
            return False
        if endpoint.method_match == "exact" and method not in allowed_methods:
            return False
        if endpoint.method_match == "contains" and not any(
            item in method for item in endpoint.methods
        ):
            return False
        if endpoint.method_match == "not" and method in allowed_methods:
            return False
    return True


def _endpoint_sets(endpoint: EndpointSpec) -> tuple[frozenset[str], ...]:
    """Return the endpoint's test codes, test names, upper-case source units, and methods.

    The sets are not cached: extraction matches each distinct row identity once.
    """

    return (
        frozenset(endpoint.test_codes),
        frozenset(endpoint.test_names or ()),
        frozenset(unit.upper() for unit in endpoint.source_units or ()),
        frozenset(endpoint.methods or ()),
    )


def endpoint_specimen_matches(
    endpoint: EndpointSpec, row: dict[str, Any], *, specimen_class: str | None = None
) -> bool:
    """Return whether the row specimen satisfies the endpoint specimen rule."""

    if not endpoint.specimens:
        return True
    if endpoint.specimen_match == "class":
        resolved = specimen_class or row_specimen_class(endpoint.domain, row)
        return resolved in endpoint.specimens
    specimen = row_specimen(endpoint.domain, row)
    if not specimen:
        return False
    if endpoint.specimen_match == "exact":
        return specimen in set(endpoint.specimens)
    return any(item in specimen for item in endpoint.specimens)


def row_specimen_class(domain: str, row: dict[str, Any]) -> str:
    """Return the vocabulary specimen class of the source specimen label."""

    return specimen_class(_row_specimen_text(domain, row))


def row_category(domain: str, row: dict[str, Any]) -> str | None:
    """Return the normalized source category (LBCAT), or None. It never sets the specimen."""

    value = row.get(f"{domain.upper()}CAT")
    return normalize_value(value) if value not in (None, "") else None


def row_pool_id(domain: str, row: dict[str, Any]) -> str | None:
    """Return a source pool identifier when the row has one."""

    for column in (f"{domain.upper()}POOLID", "POOLID"):
        if row.get(column) not in (None, ""):
            return str(row[column]).strip()
    return None


def endpoints_by_test_code(endpoints: Iterable[EndpointSpec]) -> dict[str, list[EndpointSpec]]:
    """Index the endpoints of one domain by the test codes they name, in configuration order.

    Every endpoint requires its test code, so a row is checked only against the
    endpoints that name the row's code.
    """

    by_code: dict[str, list[EndpointSpec]] = defaultdict(list)
    for endpoint in endpoints:
        for code in dict.fromkeys(endpoint.test_codes):
            by_code[code].append(endpoint)
    return by_code


def identity_columns(domain: str, columns: tuple[str, ...], days: tuple[str, ...]) -> tuple[str, ...]:
    """Return the columns that decide a row's endpoint match and its descriptions.

    Matching, data decisions, the measurement context, the measurement
    identity and the endpoint key read the study, test, specimen, method,
    qualifier, unit, fasting and laboratory columns, and the parsed state,
    unit and value-column presence. They do not read the subject, the day and
    time columns, the sequence, pool and baseline-flag columns, or the result
    values themselves (those reach them only through the parsed values, which
    the caller adds to the key). So rows with equal values in the returned
    columns and equal parsed values match and are described identically.
    """

    domain = domain.upper()
    row_specific = {SUBJECT_ID, *days}
    row_specific.update(
        column
        for column in columns
        if column.endswith(("DTC", "SEQ", "POOLID", "BLFL"))
        or column in (f"{domain}STRESN", f"{domain}ORRES", f"{domain}STRESC")
    )
    return tuple(column for column in columns if column not in row_specific)


def key_getter(columns: tuple[str, ...], selected: tuple[str, ...]) -> Any:
    """Return a function that takes the values of ``selected`` from a raw row tuple."""

    positions = [columns.index(column) for column in selected]
    if not positions:
        return lambda raw: ()
    getter = itemgetter(*positions)
    return getter if len(positions) > 1 else lambda raw: (getter(raw),)


def fetch_batches(cursor: Any, *, batch_size: int = 50_000) -> Iterable[list[tuple[Any, ...]]]:
    """Stream cursor rows without materializing a whole observation domain."""

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        yield batch


def row_test_code(domain: str, row: dict[str, Any]) -> str:
    """Return the normalized source test code that endpoint identity matching compares."""

    domain = domain.upper()
    for column in (f"{domain}TESTCD", f"{domain}TEST", "TESTCD", "TEST"):
        if column in row and row.get(column) not in (None, ""):
            return normalize_value(row.get(column))
    return "UNKNOWN"


def _row_test_name(domain: str, row: dict[str, Any]) -> str:
    domain = domain.upper()
    for column in (f"{domain}TEST", "TEST"):
        if column in row and row.get(column) not in (None, ""):
            return normalize_value(row.get(column))
    return "UNKNOWN"


def row_specimen(domain: str, row: dict[str, Any]) -> str:
    """Return the normalized source specimen label (SPEC columns only)."""

    raw = _row_specimen_text(domain, row)
    return normalize_value(raw) if raw is not None else ""


def _row_specimen_text(domain: str, row: dict[str, Any]) -> str | None:
    # Category columns (LBCAT, LBSCAT) are not specimens and are never read here.
    for column in (f"{domain.upper()}SPEC", "SPEC"):
        if column in row and row.get(column) not in (None, ""):
            return str(row[column])
    return None


def _row_method(domain: str, row: dict[str, Any]) -> str:
    domain = domain.upper()
    for column in (f"{domain}METHOD", "METHOD"):
        if column in row and row.get(column) not in (None, ""):
            return normalize_value(row.get(column))
    return ""


__all__ = [
    "endpoint_matches_row",
    "endpoint_projection_columns",
    "fetch_batches",
    "row_pool_id",
    "row_test_code",
]
