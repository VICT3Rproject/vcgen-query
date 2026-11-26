"""Fail-closed identity checks for active SEND source relations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from .contract import EndpointSpec, PreStudyEndpointField, SendColumnField, rules

if TYPE_CHECKING:
    from .query import SubjectQuery
from .contract_helpers import SourceDataError
from .grit42_catalogue import grit42_domain_grain
from .send_columns import STUDY_ID, SUBJECT_ID

SourceGrain: TypeAlias = Literal["study", "subject"]

_DERIVED_SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "DM": ("STUDYID", "USUBJID", "SPECIES", "STRAIN", "SEX", "AGE", "AGEU"),
    "TS": ("STUDYID", "TSPARMCD", "TSVAL"),
    "EX": ("STUDYID", "USUBJID", "EXSTDY", "EXROUTE", "EXDOSFRQ", "EXTRT"),
}
_OPTIONAL_DERIVED_SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "BW": (
        "STUDYID",
        "USUBJID",
        "BWSTRESN",
        "BWSTRESU",
        "BWTESTCD",
        "BWTEST",
        "BWDY",
        "BWDTC",
    ),
    "DS": (
        "STUDYID",
        "USUBJID",
        "DSDECOD",
        "DSSTDY",
    ),
}
_CONTEXT_GRAINS: dict[str, SourceGrain] = {
    "DM": "subject",
    "TS": "study",
    "EX": "subject",
    "BW": "subject",
    "DS": "subject",
}


def validate_derived_source_schema(con: Any) -> None:
    """Validate raw columns before DuckDB creates derived cohort views."""

    for domain, required in _DERIVED_SOURCE_COLUMNS.items():
        columns = _relation_columns(con, domain.lower())
        if not columns:
            raise SourceDataError(f"required source domain {domain} is not available")
        _require_columns(columns, required, domain)

    for domain, required in _OPTIONAL_DERIVED_SOURCE_COLUMNS.items():
        columns = _relation_columns(con, domain.lower())
        if columns:
            _require_columns(columns, required, domain)
            _require_columns(_relation_columns(con, "dm"), ("RFSTDTC",), "DM")


def validate_active_source(
    con: Any, query: SubjectQuery, endpoints: Sequence[EndpointSpec] = ()
) -> None:
    """Validate identities in only the relations that the query stages can consume."""

    requirements: dict[str, tuple[SourceGrain, bool]] = {"DM": ("subject", True)}

    for domain, grain in _CONTEXT_GRAINS.items():
        if _relation_columns(con, domain.lower()):
            requirements[domain] = (grain, requirements.get(domain, (grain, False))[1])

    for rule in rules(query.predicate):
        field = rule.field
        if isinstance(field, SendColumnField):
            _merge_requirement(
                requirements,
                field.domain,
                grit42_domain_grain(field.domain),
                required=True,
            )
        elif isinstance(field, PreStudyEndpointField):
            from .endpoint_catalogue import canonical_endpoint

            endpoint = canonical_endpoint(field.endpoint).spec
            _merge_requirement(requirements, endpoint.domain, "subject", required=True)

    for endpoint in endpoints:
        _merge_requirement(requirements, endpoint.domain, "subject", required=False)

    for domain, (grain, required) in sorted(requirements.items()):
        columns = _relation_columns(con, domain.lower())
        if not columns:
            if required:
                raise SourceDataError(f"required source domain {domain} is not available")
            continue
        identity_columns = (STUDY_ID, SUBJECT_ID) if grain == "subject" else (STUDY_ID,)
        actual = _require_columns(columns, identity_columns, domain)
        _validate_identity_values(con, domain, actual)

    _validate_unique_dm_subjects(con)


def _merge_requirement(
    requirements: dict[str, tuple[SourceGrain, bool]],
    domain: str,
    grain: SourceGrain,
    *,
    required: bool,
) -> None:
    current = requirements.get(domain)
    if current is None:
        requirements[domain] = (grain, required)
        return
    current_grain, current_required = current
    requirements[domain] = (
        "subject" if "subject" in {current_grain, grain} else "study",
        current_required or required,
    )


def _validate_identity_values(con: Any, domain: str, columns: Sequence[str]) -> None:
    invalid = " OR ".join(
        f"{_quote(column)} IS NULL OR trim(cast({_quote(column)} AS VARCHAR)) = ''"
        for column in columns
    )
    padded = " OR ".join(
        f"cast({_quote(column)} AS VARCHAR) <> trim(cast({_quote(column)} AS VARCHAR))"
        for column in columns
    )
    invalid_count, padded_count = con.execute(
        f"SELECT count(*) FILTER (WHERE {invalid}), "
        f"count(*) FILTER (WHERE {padded}) FROM {_quote(domain.lower())}"
    ).fetchone()
    names = "/".join(columns)
    if invalid_count:
        raise SourceDataError(
            f"{domain} contains {invalid_count} row(s) with null, empty, or "
            f"whitespace-only {names} values"
        )
    if padded_count:
        raise SourceDataError(
            f"{domain} contains {padded_count} row(s) with surrounding whitespace "
            f"in {names} values"
        )


def _validate_unique_dm_subjects(con: Any) -> None:
    duplicate_groups, excess_rows = con.execute(
        f"""
        WITH duplicates AS (
            SELECT {_quote(STUDY_ID)}, {_quote(SUBJECT_ID)}, count(*) AS row_count
            FROM dm
            GROUP BY {_quote(STUDY_ID)}, {_quote(SUBJECT_ID)}
            HAVING count(*) > 1
        )
        SELECT count(*), coalesce(sum(row_count - 1), 0)
        FROM duplicates
        """
    ).fetchone()
    if duplicate_groups:
        raise SourceDataError(
            "DM contains "
            f"{duplicate_groups} duplicate STUDYID/USUBJID group(s) "
            f"and {excess_rows} excess row(s)"
        )


def _require_columns(
    columns: Sequence[str], required: Iterable[str], relation: str
) -> tuple[str, ...]:
    available = {column.casefold(): column for column in columns}
    missing = [column for column in required if column.casefold() not in available]
    if missing:
        names = ", ".join(missing)
        raise SourceDataError(f"{relation} does not provide required column(s): {names}")
    return tuple(available[column.casefold()] for column in required)


def _relation_columns(con: Any, relation: str) -> tuple[str, ...]:
    try:
        cursor = con.execute(f"SELECT * FROM {_quote(relation)} LIMIT 0")
    except Exception:  # noqa: BLE001 - relation existence probe
        return ()
    return tuple(item[0] for item in cursor.description)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


__all__ = [
    "SourceGrain",
    "validate_active_source",
    "validate_derived_source_schema",
]
