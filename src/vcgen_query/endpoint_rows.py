"""Shared SEND endpoint-row identity and streaming helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .contract import EndpointSpec
from .send_columns import STUDY_ID, SUBJECT_ID
from .send_semantics import (
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
    endpoint: EndpointSpec, row: dict[str, Any], columns: tuple[str, ...]
) -> bool:
    """Return whether a row has the configured endpoint source identity."""

    code = _row_test_code(endpoint.domain, row)
    if code not in set(endpoint.test_codes):
        return False
    if endpoint.test_names and _row_test_name(endpoint.domain, row) not in set(endpoint.test_names):
        return False
    if endpoint.source_units:
        parsed_unit = parse_result(endpoint.domain, row, columns).unit
        allowed_units = {unit.upper() for unit in endpoint.source_units}
        if parsed_unit is None or parsed_unit.upper() not in allowed_units:
            return False
    if endpoint.specimens:
        specimen = _row_specimen(endpoint.domain, row)
        if not specimen:
            return False
        allowed = set(endpoint.specimens)
        if endpoint.specimen_match == "exact" and specimen not in allowed:
            return False
        if endpoint.specimen_match == "contains" and not any(
            item in specimen for item in endpoint.specimens
        ):
            return False
        if endpoint.specimen_match == "not" and specimen in allowed:
            return False
    if endpoint.methods:
        method = _row_method(endpoint.domain, row)
        if not method:
            return False
        allowed_methods = set(endpoint.methods)
        if endpoint.method_match == "exact" and method not in allowed_methods:
            return False
        if endpoint.method_match == "contains" and not any(
            item in method for item in endpoint.methods
        ):
            return False
        if endpoint.method_match == "not" and method in allowed_methods:
            return False
    return True


def row_pool_id(domain: str, row: dict[str, Any]) -> str | None:
    """Return a source pool identifier when the row has one."""

    for column in (f"{domain.upper()}POOLID", "POOLID"):
        if row.get(column) not in (None, ""):
            return str(row[column]).strip()
    return None


def fetch_batches(cursor: Any, *, batch_size: int = 50_000) -> Iterable[list[tuple[Any, ...]]]:
    """Stream cursor rows without materializing a whole observation domain."""

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        yield batch


def _row_test_code(domain: str, row: dict[str, Any]) -> str:
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


def _row_specimen(domain: str, row: dict[str, Any]) -> str:
    domain = domain.upper()
    for column in (
        f"{domain}SPEC",
        f"{domain}CAT",
        f"{domain}SCAT",
        "SPEC",
        "CAT",
        "SCAT",
    ):
        if column in row and row.get(column) not in (None, ""):
            return normalize_value(row.get(column))
    return ""


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
]
