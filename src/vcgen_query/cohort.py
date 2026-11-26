"""Subject contexts and endpoint resolution for cohort-query v4 execution."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import math
from typing import Any

from .contract import DayWindow, EndpointSpec, normalize_category
from .duckdb import QuerySpec, q, quote_ident, table_columns
from .endpoint_rows import (
    endpoint_matches_row,
    endpoint_projection_columns,
    fetch_batches,
)
from .facility import facility_group
from .send_columns import SEX, SPECIES, STRAIN, STUDY_ID, SUBJECT_ID
from .send_semantics import day_columns, parse_result


@dataclass(frozen=True)
class StudyContext:
    """One SEND study with fields used for subject metadata matching."""

    study_id: str
    study_length_days: int | None
    suppliers: frozenset[str]
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "study_id": self.study_id,
            "subject_id": self.subject_id,
            "species": self.species,
            "strain": self.strain,
            "sex": self.sex,
            "study_year": self.study_year,
            "study_length_days": self.study_length_days,
            "planned_dose_duration_days": self.planned_dose_duration_days,
            "observed_exposure_span_days": self.observed_exposure_span_days,
            "dose_duration_evidence_status": self.dose_duration_evidence_status,
            "suppliers": sorted(self.suppliers),
            "study_types": sorted(self.study_types),
            "routes": sorted(self.routes),
            "dose_frequencies": sorted(self.dose_frequencies),
            "treatment_vehicles": sorted(self.treatment_vehicles),
            "test_facilities": sorted(self.test_facilities),
            "facility_group": self.facility_group,
            "facility_group_evidence": self.facility_group_evidence,
            "glp_statuses": sorted(self.glp_statuses),
            "baseline_body_weight_g": self.baseline_body_weight_g,
            "age_days": self.age_days,
            "first_exposure_day": self.first_exposure_day,
        }


@dataclass(frozen=True)
class CohortSelection:
    """Result of cohort selection: matched subjects plus a receipt."""

    subjects: tuple[SubjectContext, ...]
    warnings: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    dose_duration_evidence_status_counts: dict[str, int] = field(default_factory=dict)
    exposure_interval_status_counts: dict[str, int] = field(default_factory=dict)
    dose_duration_declaration_incomplete_subject_count: int = 0

    @property
    def subject_count(self) -> int:
        return len(self.subjects)

    @property
    def study_count(self) -> int:
        return len({subject.study_id for subject in self.subjects})

    def to_receipt(self) -> dict[str, Any]:
        studies = Counter(subject.study_id for subject in self.subjects)
        return {
            "subject_count": self.subject_count,
            "study_count": self.study_count,
            "subjects_by_study": dict(sorted(studies.items())),
            "warnings": list(self.warnings),
            "counts": dict(self.counts),
            "dose_duration_evidence": {
                "status_counts": dict(self.dose_duration_evidence_status_counts),
                "exposure_interval_status_counts": dict(self.exposure_interval_status_counts),
                "incomplete_declaration_subject_count": (
                    self.dose_duration_declaration_incomplete_subject_count
                ),
            },
        }


def _load_pre_study_endpoint_values(
    con: Any,
    subject_sexes: Mapping[tuple[str, str], str],
    resolved_endpoints: tuple[tuple[EndpointSpec, DayWindow | None], ...],
) -> dict[tuple[str, str], dict[str, float]]:
    """Load each active endpoint's strict latest pre-study value per subject."""

    if not resolved_endpoints or not subject_sexes:
        return {}
    study_ids = {study_id for study_id, _ in subject_sexes}
    endpoints_by_domain: dict[str, list[EndpointSpec]] = defaultdict(list)
    for endpoint, _ in resolved_endpoints:
        endpoints_by_domain[endpoint.domain].append(endpoint)
    candidates: dict[tuple[str, str, str], list[tuple[tuple[int, int, str], float]]] = defaultdict(
        list
    )
    for domain, endpoints in sorted(endpoints_by_domain.items()):
        columns = table_columns(con, domain.lower())
        if not columns:
            continue
        cursor = _pre_study_endpoint_cursor(con, domain, columns, study_ids)
        names = tuple(item[0] for item in cursor.description)
        for batch in fetch_batches(cursor):
            for raw in batch:
                row = dict(zip(names, raw))
                study_id = str(row.get(STUDY_ID) or "")
                subject_id = str(row.get(SUBJECT_ID) or "").strip()
                key = (study_id, subject_id)
                if key not in subject_sexes:
                    continue
                study_day = _optional_int(row.get("study_day"))
                measurement_time = row.get("measurement_timestamp")
                first_exposure_day = _optional_int(row.get("first_exposure_day"))
                reference_time = row.get("reference_timestamp")
                if study_day is not None:
                    if study_day >= (first_exposure_day if first_exposure_day is not None else 1):
                        continue
                    rank = (1, study_day, str(measurement_time or ""))
                else:
                    if (
                        measurement_time is None
                        or reference_time is None
                        or measurement_time >= reference_time
                    ):
                        continue
                    rank = (0, 0, str(measurement_time))
                parsed = parse_result(domain, row, names)
                if (
                    parsed.excluded
                    or parsed.numeric_value is None
                    or not math.isfinite(parsed.numeric_value)
                ):
                    continue
                for endpoint in endpoints:
                    if endpoint_matches_row(endpoint, row, names) and endpoint.applies_to_sex(
                        subject_sexes[key]
                    ):
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
    days = day_columns(domain, columns)
    day_terms = [f"try_cast(nullif(rows.{q(column)}, '') AS INTEGER)" for column in days]
    day_expr = f"coalesce({', '.join(day_terms)})" if day_terms else "NULL"
    timestamp_columns = tuple(
        column
        for column in (f"{domain}DTC", f"{domain}STDTC", f"{domain}ENDTC", "DTC")
        if column in columns
    )
    timestamp_terms = [
        f"try_cast(nullif(rows.{q(column)}, '') AS TIMESTAMP)" for column in timestamp_columns
    ]
    timestamp_expr = f"coalesce({', '.join(timestamp_terms)})" if timestamp_terms else "NULL"
    dm_columns = table_columns(con, "dm")
    reference_expr = (
        f"try_cast(nullif(dm.{q('RFSTDTC')}, '') AS TIMESTAMP)"
        if "RFSTDTC" in dm_columns
        else "NULL"
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
    # The outer filter keeps exactly the rows that the caller does not skip
    # for timing: a study day before the first exposure day (or day 1), or,
    # without a study day, a timestamp before the reference start. The caller
    # repeats these checks, so this filter only removes post-dose rows early.
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
            study_types=_value_set(values.get("study_type_values")),
            test_facilities=_value_set(values.get("test_facility_values")),
            facility_group=group,
            facility_group_evidence=evidence,
            study_start_year=_optional_int(values.get("study_start_year")),
            glp_statuses=_value_set(values.get("glp_status_values")),
        )
        contexts[study.study_id] = study
    return contexts


def _load_subject_contexts(
    con: Any, study_contexts: dict[str, StudyContext],
    *, selected_keys: set[tuple[str, str]], observed_keys: list[tuple[str, str]],
) -> tuple[SubjectContext, ...]:
    query = _subject_context_query(con)
    cursor = con.execute(query.sql, query.params)
    columns = [item[0] for item in cursor.description]
    contexts: list[SubjectContext] = []
    for raw in cursor.fetchall():
        values = dict(zip(columns, raw))
        study_id = str(values.get(STUDY_ID) or "")
        subject_id = str(values.get(SUBJECT_ID) or "")
        key = (study_id, subject_id)
        observed_keys.append(key)
        if key not in selected_keys:
            continue
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
                study_types=study.study_types,
                routes=_value_set(values.get("route_values")),
                dose_frequencies=_value_set(values.get("dose_frequency_values")),
                treatment_vehicles=_value_set(values.get("treatment_values")),
                test_facilities=study.test_facilities,
                facility_group=study.facility_group,
                facility_group_evidence=study.facility_group_evidence,
                glp_statuses=study.glp_statuses,
                baseline_body_weight_g=_optional_float(values.get("baseline_body_weight_g")),
                age_days=_optional_float(values.get("age_days")),
                first_exposure_day=_optional_int(values.get("first_exposure_day")),
                dose_duration_declaration_incomplete=bool(
                    values.get("dose_duration_declaration_incomplete")
                ),
                exposure_interval_status=str(
                    values.get("exposure_interval_status") or "unavailable"
                ),
            )
        )
    return tuple(contexts)


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
            {route_expr} AS route_values,
            {frequency_expr} AS dose_frequency_values,
            {treatment_expr} AS treatment_values,
            {baseline_body_weight_expr} AS baseline_body_weight_g,
            subjects.age_days,
            {first_exposure_expr} AS first_exposure_day
        FROM {quote_ident(subject_view)} AS subjects
        {exposure_join}
        {baseline_body_weight_join}
        ORDER BY subjects.{q(STUDY_ID)}, subjects.{q(SUBJECT_ID)}
        """
    )


def _value_set(raw: Any) -> frozenset[str]:
    """Normalize aggregated source values, dropping NULL and blank entries.

    EX-derived lists aggregate raw columns without NULL guards, so a missing
    ``EXTRT``/``EXROUTE``/``EXDOSFRQ`` cell arrives as a ``None`` element.
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


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else None


__all__ = [
    "CohortSelection",
    "StudyContext",
    "SubjectContext",
    "sample_id_for_subject",
]
