"""Versioned grit42 domain-column catalogue used by query parsing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.resources import files
import json
from typing import Literal, TypeAlias, cast

from .catalogue_values import catalogue_mapping, catalogue_sequence


CATALOGUE_CONTRACT = "vcgen.grit42_endpoint_catalogue.v1"
GRIT42_CATALOGUE_FILENAME = "grit42_endpoint_catalogue.2026-08-22.v1.json"
GRIT42_CATALOGUE_ID = "grit42-columns-2026-08-22-v1"
CatalogueValueType: TypeAlias = Literal["string", "number", "datetime"]
CatalogueUiType: TypeAlias = Literal["string", "numeric", "timestamp", "entity"]
AdvancedQueryDatetimeSource: TypeAlias = Literal["calendar_date_midnight"]
CatalogueDomainGrain: TypeAlias = Literal["study", "subject"]


@dataclass(frozen=True)
class Grit42Column:
    domain: str
    column: str
    ui_type: CatalogueUiType
    value_type: CatalogueValueType
    advanced_query_datetime_source: AdvancedQueryDatetimeSource | None = None


@dataclass(frozen=True)
class Grit42Catalogue:
    catalogue_id: str
    captured_on: str
    columns: Mapping[tuple[str, str], Grit42Column]


@lru_cache(maxsize=1)
def load_grit42_catalogue() -> Grit42Catalogue:
    resource = files("vcgen_query").joinpath("data", GRIT42_CATALOGUE_FILENAME)
    payload = catalogue_mapping(json.loads(resource.read_text(encoding="utf-8")), "catalogue")
    if payload.get("contract") != CATALOGUE_CONTRACT:
        raise RuntimeError(f"catalogue contract must be {CATALOGUE_CONTRACT!r}")
    if payload.get("catalogue_id") != GRIT42_CATALOGUE_ID:
        raise RuntimeError(f"catalogue_id must be {GRIT42_CATALOGUE_ID!r}")
    captured_on = payload.get("captured_on")
    if not isinstance(captured_on, str):
        raise RuntimeError("catalogue captured_on must be an ISO date")
    try:
        date.fromisoformat(captured_on)
    except ValueError as error:
        raise RuntimeError("catalogue captured_on must be an ISO date") from error

    columns: dict[tuple[str, str], Grit42Column] = {}
    domains = catalogue_sequence(payload.get("domains"), "catalogue.domains")
    for domain_index, domain_raw in enumerate(domains):
        domain = catalogue_mapping(domain_raw, f"catalogue.domains[{domain_index}]")
        domain_code = domain.get("code")
        if not isinstance(domain_code, str) or not domain_code:
            raise RuntimeError(f"catalogue.domains[{domain_index}].code is invalid")
        for column_index, column_raw in enumerate(
            catalogue_sequence(domain.get("columns"), f"catalogue.domains[{domain_index}].columns")
        ):
            column = catalogue_mapping(
                column_raw,
                f"catalogue.domains[{domain_index}].columns[{column_index}]",
            )
            column_code = column.get("code")
            ui_type = column.get("ui_type")
            value_type = column.get("value_type")
            datetime_source = column.get("advanced_query_datetime_source")
            if not isinstance(column_code, str) or not column_code:
                raise RuntimeError("catalogue column code is invalid")
            if ui_type not in {"string", "numeric", "timestamp", "entity"}:
                raise RuntimeError("catalogue column ui_type is invalid")
            if value_type not in {"string", "number", "datetime"}:
                raise RuntimeError("catalogue column value_type is invalid")
            if datetime_source not in {None, "calendar_date_midnight"}:
                raise RuntimeError("catalogue advanced-query datetime source is invalid")
            if datetime_source is not None and value_type != "datetime":
                raise RuntimeError(
                    "catalogue advanced-query datetime source requires a datetime column"
                )
            key = (domain_code, column_code)
            if key in columns:
                raise RuntimeError(f"catalogue has duplicate column {domain_code}.{column_code}")
            columns[key] = Grit42Column(
                domain_code,
                column_code,
                cast(CatalogueUiType, ui_type),
                cast(CatalogueValueType, value_type),
                cast(AdvancedQueryDatetimeSource | None, datetime_source),
            )

    counts = catalogue_mapping(payload.get("counts"), "catalogue.counts")
    if counts.get("domains") != len(domains):
        raise RuntimeError("catalogue domain count is inconsistent")
    if counts.get("domain_endpoint_occurrences") != len(columns):
        raise RuntimeError("catalogue domain-column count is inconsistent")
    if counts.get("unique_endpoint_codes") != len({column for _, column in columns}):
        raise RuntimeError("catalogue unique-column count is inconsistent")
    return Grit42Catalogue(GRIT42_CATALOGUE_ID, captured_on, columns)


def grit42_column(domain: str, column: str) -> Grit42Column | None:
    """Return the captured grit42 column definition for one domain-column pair."""

    return load_grit42_catalogue().columns.get((domain, column))


def grit42_domain_grain(domain: str) -> CatalogueDomainGrain:
    """Return the declared query grain for one catalogue domain."""

    catalogue = load_grit42_catalogue()
    if not any(item_domain == domain for item_domain, _ in catalogue.columns):
        raise RuntimeError(f"grit42 catalogue is missing domain {domain!r}")
    return "subject" if (domain, "USUBJID") in catalogue.columns else "study"


__all__ = [
    "GRIT42_CATALOGUE_FILENAME",
    "GRIT42_CATALOGUE_ID",
    "AdvancedQueryDatetimeSource",
    "CatalogueDomainGrain",
    "Grit42Catalogue",
    "Grit42Column",
    "grit42_column",
    "grit42_domain_grain",
    "load_grit42_catalogue",
]
