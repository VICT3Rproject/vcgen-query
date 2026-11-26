"""Subject contexts and endpoint resolution for cohort-query v4 execution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
import math
from typing import Any

import pandas as pd

from .contract import EndpointSpec, normalize_category
from .query import StageWarning
from .duckdb import QuerySpec, pre_study_timing_sql, q, quote_ident, registered_frame, table_columns
from .table_schema import SUBJECTS_TABLE
from .endpoint_rows import (
    endpoint_matches_row,
    endpoint_projection_columns,
    endpoints_by_test_code,
    fetch_batches,
    identity_columns,
    key_getter,
    row_test_code,
)
from .facility import facility_group
from .supplier import supplier_group
from .send_columns import SEX, SPECIES, STRAIN, STUDY_ID, SUBJECT_ID
from .send_semantics import ParsedResult, day_columns, optional_float, parse_result, result_columns


@dataclass(frozen=True)
class StudyContext:
    """One SEND study with fields used for subject metadata matching."""

    study_id: str
    study_length_days: int | None
    suppliers: frozenset[str]
    supplier_group: str | None
    study_types: frozenset[str]
    test_facilities: frozenset[str]
    facility_group: str | None
    facility_group_evidence: str | None
    study_start_year: int | None
    glp_statuses: frozenset[str]


@dataclass(frozen=True)
class SubjectContext:
    """One historical subject with fields used for subject metadata matching."""

    sample_id: str
    study_id: str
    subject_id: str
    species: str
    strain: str
    sex: str
    study_year: int | None
    study_length_days: int | None
    planned_dose_duration_days: int | None
    observed_exposure_span_days: int | None
    dose_duration_evidence_status: str
    suppliers: frozenset[str]
    supplier_group: str | None
    study_types: frozenset[str]
    routes: frozenset[str]
    dose_frequencies: frozenset[str]
    treatment_vehicles: frozenset[str]
    test_facilities: frozenset[str]
    facility_group: str | None
    facility_group_evidence: str | None
    glp_statuses: frozenset[str]
    baseline_body_weight_g: float | None
    age_days: float | None
    first_exposure_day: int | None
    dose_duration_declaration_incomplete: bool = field(repr=False)
    exposure_interval_status: str = field(repr=False)
    # ``disposition`` (a DS terminal record), ``necropsy`` (the pathology-day
    # fallback) or None (no terminal record).
    terminal_record_source: str | None = field(default=None, repr=False)
    # For a necropsy-dated subject: the spread of its per-domain latest pathology days.
    pathology_day_spread_days: int | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the public fields (``SUBJECTS_TABLE``), with sets as sorted lists."""

        return {
            item.name: sorted(value) if isinstance(value, frozenset) else value
            for item in fields(self)
            if item.repr
            for value in (getattr(self, item.name),)
        }


@dataclass(frozen=True)
class CohortSelection:
    """Result of cohort selection: matched subjects plus a receipt."""

    subjects: tuple[SubjectContext, ...]
    warnings: tuple[StageWarning, ...] = ()
    dose_duration_evidence_status_counts: dict[str, int] = field(default_factory=dict)
    exposure_interval_status_counts: dict[str, int] = field(default_factory=dict)
    dose_duration_declaration_incomplete_subject_count: int = 0


# Columns that the pre-study cursor computes for each row.
_TIMING_COLUMNS = frozenset(
    {"study_day", "measurement_timestamp", "reference_timestamp", "first_exposure_day"}
)


def _load_pre_study_endpoint_values(
    con: Any,
    subject_sexes: Mapping[tuple[str, str], str],
    resolved_endpoints: tuple[EndpointSpec, ...],
) -> dict[tuple[str, str], dict[str, float]]:
    """Load each active endpoint's strict latest pre-study value per subject."""

    if not resolved_endpoints or not subject_sexes:
        return {}
    study_ids = {study_id for study_id, _ in subject_sexes}
    endpoints_by_domain: dict[str, list[EndpointSpec]] = defaultdict(list)
    for endpoint in resolved_endpoints:
        endpoints_by_domain[endpoint.domain].append(endpoint)
    candidates: dict[tuple[str, str, str], list[tuple[tuple[int, int, str], float]]] = defaultdict(
        list
    )
    for domain, domain_endpoints in sorted(endpoints_by_domain.items()):
        columns = table_columns(con, domain.lower())
        if not columns:
            continue
        cursor = _pre_study_endpoint_cursor(con, domain, columns, study_ids)
        names = tuple(item[0] for item in cursor.description)
        by_code = endpoints_by_test_code(domain_endpoints)
        # Many rows share their identity and result values, so each distinct
        # value tuple is parsed and matched once (see identity_columns). The
        # timing columns that the cursor adds are specific to a row.
        source_names = tuple(name for name in names if name not in _TIMING_COLUMNS)
        parse_key = key_getter(names, result_columns(domain, source_names))
        identity_key = key_getter(
            names, identity_columns(domain, source_names, day_columns(domain, columns))
        )
        parsed_by_values: dict[Any, ParsedResult] = {}
        matches: dict[tuple[Any, ...], tuple[EndpointSpec, ...]] = {}
        for batch in fetch_batches(cursor):
            for raw in batch:
                row = dict(zip(names, raw))
                study_id = str(row.get(STUDY_ID) or "")
                subject_id = str(row.get(SUBJECT_ID) or "").strip()
                key = (study_id, subject_id)
                if key not in subject_sexes:
                    continue
                # The cursor yields only rows before the first exposure; a day
                # outranks a timestamp, so the day is the primary key.
                study_day = _optional_int(row.get("study_day"))
                measurement_time = row.get("measurement_timestamp")
                if study_day is not None:
                    rank = (1, study_day, str(measurement_time or ""))
                else:
                    rank = (0, 0, str(measurement_time))
                values = parse_key(raw)
                parsed = parsed_by_values.get(values)
                if parsed is None:
                    parsed = parsed_by_values[values] = parse_result(domain, row, names)
                if parsed.excluded or parsed.numeric_value is None:
                    continue
                match_key = (identity_key(raw), parsed.state, parsed.unit, parsed.value_column is None)
                matched = matches.get(match_key)
                if matched is None:
                    matched = matches[match_key] = tuple(
                        endpoint
                        for endpoint in by_code.get(row_test_code(domain, row), ())
                        if endpoint_matches_row(endpoint, row, names, parsed=parsed)
                    )
                for endpoint in matched:
                    if endpoint.applies_to_sex(subject_sexes[key]):
                        candidates[(study_id, subject_id, endpoint.name)].append(
                            (rank, parsed.numeric_value)
                        )
    resolved: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (
        study_id,
        subject_id,
        endpoint_name,
    ), endpoint_candidates in candidates.items():
        latest_rank = max(rank for rank, _ in endpoint_candidates)
        latest_values = {value for rank, value in endpoint_candidates if rank == latest_rank}
        if len(latest_values) == 1:
            resolved[(study_id, subject_id)][endpoint_name] = latest_values.pop()
    return dict(resolved)


def _pre_study_endpoint_cursor(
    con: Any,
    domain: str,
    columns: tuple[str, ...],
    eligible_study_ids: set[str],
) -> Any:
    """Stream endpoint rows with study-relative timing evidence."""

    selected_columns = endpoint_projection_columns(domain, columns)
    projection = ", ".join(f"rows.{q(column)}" for column in selected_columns)
    day_expr, timestamp_expr, reference_expr = pre_study_timing_sql(
        domain, columns, table_columns(con, "dm"), "rows"
    )
    has_exposure = bool(table_columns(con, "subject_exposure_summary"))
    exposure_join = (
        f"LEFT JOIN subject_exposure_summary AS exposure "
        f"ON rows.{q(STUDY_ID)} = exposure.{q(STUDY_ID)} "
        f"AND rows.{q(SUBJECT_ID)} = exposure.{q(SUBJECT_ID)}"
        if has_exposure
        else ""
    )
    exposure_expr = "exposure.first_exposure_day" if has_exposure else "NULL"
    study_ids = tuple(sorted(study_id for study_id in eligible_study_ids if study_id))
    placeholders = ", ".join("?" for _ in study_ids)
    # The outer filter keeps only the rows before the study: a study day before
    # the first exposure day (or day 1), or, without a study day, a timestamp
    # before the reference start. The caller relies on this filter.
    return con.execute(
        f"""
        SELECT * FROM (
            SELECT {projection},
                {day_expr} AS study_day,
                {timestamp_expr} AS measurement_timestamp,
                {reference_expr} AS reference_timestamp,
                {exposure_expr} AS first_exposure_day
            FROM {quote_ident(domain.lower())} AS rows
            INNER JOIN dm AS dm
                ON rows.{q(STUDY_ID)} = dm.{q(STUDY_ID)}
                AND rows.{q(SUBJECT_ID)} = dm.{q(SUBJECT_ID)}
            {exposure_join}
            WHERE cast(rows.{q(STUDY_ID)} AS VARCHAR) IN ({placeholders})
        ) AS timed
        WHERE (study_day IS NOT NULL AND study_day < coalesce(first_exposure_day, 1))
            OR (
                study_day IS NULL
                AND measurement_timestamp IS NOT NULL
                AND reference_timestamp IS NOT NULL
                AND measurement_timestamp < reference_timestamp
            )
        """,
        list(study_ids),
    )


def _load_study_contexts(con: Any) -> dict[str, StudyContext]:
    dm_columns = table_columns(con, "dm")
    year_expr = (
        "try_cast(regexp_extract(RFSTDTC, '^[0-9]{4}', 0) AS INTEGER)"
        if "RFSTDTC" in dm_columns
        else "NULL"
    )
    cursor = con.execute(
        f"""
        SELECT
            dm.{q(STUDY_ID)} AS study_id,
            studies.study_length_days,
            studies.supplier_values,
            studies.study_type_values,
            studies.test_facility_values,
            studies.glp_status_values,
            CASE
                WHEN count(*) > 0
                     AND count({year_expr}) = count(*)
                     AND min({year_expr}) = max({year_expr})
                THEN min({year_expr})
            END AS study_start_year
        FROM dm
        LEFT JOIN study_design_summary AS studies USING ({q(STUDY_ID)})
        GROUP BY
            dm.{q(STUDY_ID)},
            studies.study_length_days,
            studies.supplier_values,
            studies.study_type_values,
            studies.test_facility_values,
            studies.glp_status_values
        """
    )
    columns = [item[0] for item in cursor.description]
    contexts = {}
    for values in (dict(zip(columns, raw)) for raw in cursor.fetchall()):
        group, evidence = _single_facility_group(values.get("test_facility_values"))
        study = StudyContext(
            study_id=str(values["study_id"] or ""),
            study_length_days=_optional_int(values.get("study_length_days")),
            suppliers=_value_set(values.get("supplier_values")),
            supplier_group=_single_supplier_group(values.get("supplier_values")),
            study_types=_value_set(values.get("study_type_values")),
            test_facilities=_value_set(values.get("test_facility_values")),
            facility_group=group,
            facility_group_evidence=evidence,
            study_start_year=_optional_int(values.get("study_start_year")),
            glp_statuses=_value_set(values.get("glp_status_values")),
        )
        contexts[study.study_id] = study
    return contexts


def _load_subject_contexts(con: Any) -> tuple[SubjectContext, ...]:
    """Build the context of every subject in the derived subject view."""

    study_contexts = _load_study_contexts(con)
    query = _subject_context_query(con)
    cursor = con.execute(query.sql, query.params)
    columns = [item[0] for item in cursor.description]
    contexts: list[SubjectContext] = []
    for raw in cursor.fetchall():
        values = dict(zip(columns, raw))
        study_id = str(values.get(STUDY_ID) or "")
        subject_id = str(values.get(SUBJECT_ID) or "")
        study = study_contexts[study_id]
        contexts.append(
            SubjectContext(
                sample_id=sample_id_for_subject(study_id, subject_id),
                study_id=study_id,
                subject_id=subject_id,
                species=str(values.get(SPECIES) or ""),
                strain=str(values.get(STRAIN) or ""),
                sex=str(values.get(SEX) or ""),
                study_year=study.study_start_year,
                study_length_days=study.study_length_days,
                planned_dose_duration_days=_optional_int(
                    values.get("planned_dose_duration_days")
                ),
                observed_exposure_span_days=_optional_int(
                    values.get("observed_exposure_span_days")
                ),
                dose_duration_evidence_status=str(
                    values.get("dose_duration_evidence_status") or "unresolved"
                ),
                suppliers=study.suppliers,
                supplier_group=study.supplier_group,
                study_types=study.study_types,
                routes=_value_set(values.get("route_values")),
                dose_frequencies=_value_set(values.get("dose_frequency_values")),
                treatment_vehicles=_value_set(values.get("treatment_values")),
                test_facilities=study.test_facilities,
                facility_group=study.facility_group,
                facility_group_evidence=study.facility_group_evidence,
                glp_statuses=study.glp_statuses,
                baseline_body_weight_g=optional_float(values.get("baseline_body_weight_g")),
                age_days=optional_float(values.get("age_days")),
                first_exposure_day=_optional_int(values.get("first_exposure_day")),
                dose_duration_declaration_incomplete=bool(
                    values.get("dose_duration_declaration_incomplete")
                ),
                exposure_interval_status=str(
                    values.get("exposure_interval_status") or "unavailable"
                ),
                terminal_record_source=values.get("terminal_record_source"),
                pathology_day_spread_days=_optional_int(values.get("pathology_day_spread_days")),
            )
        )
    return tuple(contexts)


# The stored subject context holds every SubjectContext field except the
# sample id, which is derived from the key. Set fields are sorted lists.
_DUCKDB_TYPES = {
    "string": "VARCHAR", "Int64": "BIGINT", "Float64": "DOUBLE", "object": "VARCHAR[]",
}
_SUBJECT_CONTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    *(
        (column.name, _DUCKDB_TYPES[column.pandas_dtype])
        for column in SUBJECTS_TABLE.columns
        if column.name != "sample_id"
    ),
    # Internal fields, not in the public subjects table.
    ("dose_duration_declaration_incomplete", "BOOLEAN"),
    ("exposure_interval_status", "VARCHAR"),
    ("terminal_record_source", "VARCHAR"),
    ("pathology_day_spread_days", "BIGINT"),
)
_SET_FIELDS = frozenset(name for name, kind in _SUBJECT_CONTEXT_COLUMNS if kind == "VARCHAR[]")


def create_subject_context_table(con: Any, name: str) -> None:
    """Store every subject context in table ``name``, one row per subject."""

    records = [
        tuple(
            sorted(value) if field_name in _SET_FIELDS else value
            for field_name, value in (
                (column, getattr(context, column)) for column, _ in _SUBJECT_CONTEXT_COLUMNS
            )
        )
        for context in _load_subject_contexts(con)
    ]
    frame = pd.DataFrame.from_records(
        records, columns=[column for column, _ in _SUBJECT_CONTEXT_COLUMNS]
    ).astype(object)
    _create_table_from_frame(con, name, frame, _SUBJECT_CONTEXT_COLUMNS, "study_id, subject_id")


def load_stored_subject_contexts(
    con: Any, name: str, keys: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], SubjectContext]:
    """Read the stored contexts of ``keys``; keys without a row are absent."""

    selected = pd.DataFrame.from_records(list(keys), columns=["study_id", "subject_id"])
    with registered_frame(con, selected.astype(str)) as wanted:
        rows = con.execute(
            f"SELECT {', '.join(f'stored.{q(column)}' for column, _ in _SUBJECT_CONTEXT_COLUMNS)} "
            f"FROM {quote_ident(name)} AS stored "
            f"JOIN {wanted} AS wanted USING (study_id, subject_id)"
        ).fetchall()
    contexts = {}
    for raw in rows:
        values = {
            column: frozenset(value) if column in _SET_FIELDS else value
            for (column, _), value in zip(_SUBJECT_CONTEXT_COLUMNS, raw)
        }
        key = (values["study_id"], values["subject_id"])
        contexts[key] = SubjectContext(
            sample_id=sample_id_for_subject(*key), **values
        )
    return contexts


def _create_table_from_frame(
    con: Any,
    name: str,
    frame: pd.DataFrame,
    columns: tuple[tuple[str, str], ...],
    order: str,
) -> None:
    with registered_frame(con, frame) as source:
        con.execute(
            f"CREATE TABLE {quote_ident(name)} AS SELECT "
            + ", ".join(
                f"CAST({q(column)} AS {kind}) AS {q(column)}" for column, kind in columns
            )
            + f" FROM {source} ORDER BY {order}"
        )


_PRE_STUDY_COLUMNS: tuple[tuple[str, str], ...] = (
    (STUDY_ID, "VARCHAR"),
    (SUBJECT_ID, "VARCHAR"),
    ("endpoint_name", "VARCHAR"),
    ("value", "DOUBLE"),
)


def subject_sexes(con: Any) -> dict[tuple[str, str], str]:
    """Return the sex of every subject in the derived subject view."""

    return {
        (str(study or ""), str(subject or "")): str(sex or "")
        for study, subject, sex in con.execute(
            f"SELECT {q(STUDY_ID)}, {q(SUBJECT_ID)}, {q(SEX)} FROM subject_demographics"
        ).fetchall()
    }


def create_pre_study_endpoint_table(con: Any, name: str) -> None:
    """Store the pre-study value of every catalogue endpoint for every subject."""

    from .endpoint_catalogue import load_endpoint_catalogue

    specs = tuple(endpoint.spec for endpoint in load_endpoint_catalogue().endpoints.values())
    values = _load_pre_study_endpoint_values(con, subject_sexes(con), specs)
    frame = pd.DataFrame.from_records(
        [
            (study_id, subject_id, endpoint_name, value)
            for (study_id, subject_id), resolved in values.items()
            for endpoint_name, value in resolved.items()
        ],
        columns=[column for column, _ in _PRE_STUDY_COLUMNS],
    ).astype(object)
    _create_table_from_frame(
        con, name, frame, _PRE_STUDY_COLUMNS,
        f"endpoint_name, {q(STUDY_ID)}, {q(SUBJECT_ID)}",
    )


def sample_id_for_subject(study_id: str, subject_id: str) -> str:
    """Return the stable sequence key for a study subject."""

    return f"{study_id}::{subject_id}"


def _subject_context_query(con: Any) -> QuerySpec:
    subject_view = "subject_demographics"
    has_exposure = bool(table_columns(con, "subject_exposure_summary"))
    has_baseline_body_weight = bool(table_columns(con, "subject_baseline_body_weight"))
    exposure_join = ""
    route_expr = "NULL"
    frequency_expr = "NULL"
    treatment_expr = "NULL"
    first_exposure_expr = "NULL"
    planned_dose_duration_expr = "NULL"
    observed_exposure_span_expr = "NULL"
    duration_evidence_status_expr = "'unresolved'"
    declaration_incomplete_expr = "FALSE"
    exposure_interval_status_expr = "'unavailable'"
    baseline_body_weight_join = ""
    baseline_body_weight_expr = "NULL"
    disposition_join = ""
    terminal_record_source_expr = "NULL"
    pathology_day_spread_expr = "NULL"
    if table_columns(con, "subject_disposition_summary"):
        disposition_join = f"""
            LEFT JOIN subject_disposition_summary AS disposition
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        """
        terminal_record_source_expr = "disposition.terminal_record_source"
        pathology_day_spread_expr = "disposition.pathology_day_spread_days"
    if has_exposure:
        exposure_join = f"""
            LEFT JOIN subject_exposure_summary AS exposure
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        """
        route_expr = "exposure.route_values"
        frequency_expr = "exposure.dose_frequency_values"
        treatment_expr = "exposure.treatment_values"
        first_exposure_expr = "exposure.first_exposure_day"
        planned_dose_duration_expr = "exposure.planned_dose_duration_days"
        observed_exposure_span_expr = "exposure.observed_exposure_span_days"
        duration_evidence_status_expr = "exposure.dose_duration_evidence_status"
        declaration_incomplete_expr = "exposure.dose_duration_declaration_incomplete"
        exposure_interval_status_expr = "exposure.exposure_interval_status"
    if has_baseline_body_weight:
        baseline_body_weight_join = f"""
            LEFT JOIN subject_baseline_body_weight AS baseline_bw
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        """
        baseline_body_weight_expr = "baseline_bw.baseline_body_weight_g"
    return QuerySpec(
        f"""
        SELECT
            subjects.{q(STUDY_ID)},
            subjects.{q(SUBJECT_ID)},
            subjects.{q(SPECIES)},
            subjects.{q(STRAIN)},
            subjects.{q(SEX)},
            {planned_dose_duration_expr} AS planned_dose_duration_days,
            {observed_exposure_span_expr} AS observed_exposure_span_days,
            {duration_evidence_status_expr} AS dose_duration_evidence_status,
            {declaration_incomplete_expr} AS dose_duration_declaration_incomplete,
            {exposure_interval_status_expr} AS exposure_interval_status,
            {terminal_record_source_expr} AS terminal_record_source,
            {pathology_day_spread_expr} AS pathology_day_spread_days,
            {route_expr} AS route_values,
            {frequency_expr} AS dose_frequency_values,
            {treatment_expr} AS treatment_values,
            {baseline_body_weight_expr} AS baseline_body_weight_g,
            subjects.age_days,
            {first_exposure_expr} AS first_exposure_day
        FROM {quote_ident(subject_view)} AS subjects
        {exposure_join}
        {baseline_body_weight_join}
        {disposition_join}
        ORDER BY subjects.{q(STUDY_ID)}, subjects.{q(SUBJECT_ID)}
        """
    )


def _value_set(raw: Any) -> frozenset[str]:
    """Normalize aggregated source values, dropping NULL and blank entries.

    EX-derived lists aggregate raw columns without NULL guards, so a missing
    ``EXTRTV``/``EXROUTE``/``EXDOSFRQ`` cell arrives as a ``None`` element.
    Missing source values never satisfy an active selector.
    """

    if not raw:
        return frozenset()
    items: Iterable[Any] = raw if isinstance(raw, (list, tuple)) else str(raw).split(" | ")
    return frozenset(
        normalize_category(part) for part in items if isinstance(part, str) and part.strip()
    )


def _single_facility_group(raw: Any) -> tuple[str | None, str | None]:
    groups = {facility_group(value)[:2] for value in _value_set(raw)}
    return next(iter(groups)) if len(groups) == 1 else (None, None)


def _single_supplier_group(raw: Any) -> str | None:
    """The one company of the supplier names of a study; placeholders do not count."""
    groups = {supplier_group(value) for value in _value_set(raw)} - {None}
    return next(iter(groups)) if len(groups) == 1 else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number == int(number) else None


__all__ = [
    "CohortSelection",
    "StudyContext",
    "SubjectContext",
    "create_pre_study_endpoint_table",
    "create_subject_context_table",
    "load_stored_subject_contexts",
    "sample_id_for_subject",
    "subject_sexes",
]
