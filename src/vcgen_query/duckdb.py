"""DuckDB registration and query helpers for SEND-like source domains.

This is a dependency-decoupled port of the SEND registration layer. It builds
derived subject, study-design, and exposure views needed for canonical cohort
selection.

* No repository path defaults. ``DEFAULT_ARCHIVE`` / ``DEFAULT_EXTRACT_DIR`` /
  ``DEFAULT_DUCKDB_PATH`` are gone. Callers pass explicit paths, so the library
  never silently reads stale data relative to its own install location.
* Content-aware extraction. The archive is hashed and a marker is written next
  to the extracted CSVs; a changed archive (or a corrupted member) forces
  re-extraction instead of reusing files by filename alone.
* Cohort-selector columns. The exposure summary additionally exposes
  ``route_values``, ``dose_frequency_values`` and ``treatment_values`` as
  per-subject aggregated sets so the canonical selectors use exact membership.
* DuckDB is a required package dependency.

Column constants come from the package-local :mod:`vcgen_query.send_columns`,
not the host ``vcgen_ai.constants``.
"""

from __future__ import annotations

import hashlib
import shutil
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZipInfo

import duckdb

from .source_validation import validate_derived_source_schema
from .send_columns import (
    AGE,
    AGE_UNIT,
    DOSE_DURATION_PARAMETER_CODE,
    EX_DOSE_FREQUENCY,
    EX_END,
    EX_END_DAY,
    EX_ROUTE,
    EX_SEQUENCE,
    EX_START,
    EX_START_DAY,
    EX_TREATMENT,
    GLP_STATUS_PARAMETER_CODE,
    RAW_DOMAIN_ORDER,
    SEX,
    SPECIES,
    STRAIN,
    STUDY_ID,
    STUDY_LENGTH_PARAMETER_CODE,
    STUDY_TYPE_PARAMETER_CODE,
    SUBJECT_ID,
    SUPPLIER_PARAMETER_CODE,
    TEST_FACILITY_PARAMETER_CODE,
    TS_PARAMETER_CODE,
    TS_VALUE,
)


_REQUIRED_COHORT_DOMAINS = frozenset({"DM", "TX", "TA", "EX", "SE", "TS"})


@dataclass(frozen=True)
class QuerySpec:
    """Parameterized SQL plus bound values."""

    sql: str
    params: tuple[Any, ...] = ()


def connect(database: str | Path | None = None, *, read_only: bool = False) -> Any:
    """Open a DuckDB connection (in-memory when ``database`` is ``None``)."""

    db = ":memory:" if database is None else str(database)
    if db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db, read_only=read_only)


def archive_sha256(archive: str | Path) -> str:
    """Return the SHA-256 of a SEND archive for content-aware caching."""

    archive = Path(archive)
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_extracted(
    archive: str | Path,
    extract_dir: str | Path,
    *,
    domains: Iterable[str] | None = None,
    overwrite: bool = False,
) -> Path:
    """Extract selected CSV domains from the zip into a local folder.

    Content-aware: the archive is hashed and a ``.archive_sha256`` marker is
    stored in ``extract_dir``. If the marker is missing or differs from the
    member is additionally verified against its zip CRC-32, so a partially
    corrupted cache is repaired rather than reused.
    """

    archive = Path(archive)
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    current_hash = archive_sha256(archive)
    marker = extract_dir / ".archive_sha256"
    cached_hash = ""
    if marker.is_file():
        cached_hash = marker.read_text(encoding="utf-8").strip()
    archive_changed = cached_hash != current_hash
    force = overwrite or archive_changed

    wanted = {normalize_domain(domain) for domain in domains} if domains else None
    extracted_any = False
    with ZipFile(archive) as zf:
        for member in zf.infolist():
            member_path = Path(member.filename)
            if member_path.suffix.lower() != ".csv":
                continue
            domain = normalize_domain(member_path.stem)
            if wanted is not None and domain not in wanted:
                continue
            target = extract_dir / f"{domain}.csv"
            if target.exists() and not force and _member_matches_cache(target, member):
                continue
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_any = True

    if force or extracted_any or not marker.is_file():
        marker.write_text(current_hash, encoding="utf-8")
    return extract_dir


def _member_matches_cache(target: Path, member: ZipInfo) -> bool:
    """Return whether an extracted file still matches its zip member identity.

    Size alone is insufficient: a same-size corruption passes it. Compare the
    extracted file's CRC-32 against the member's recorded CRC as well.
    """

    try:
        if target.stat().st_size != member.file_size:
            return False
    except OSError:
        return False
    digest = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest = zlib.crc32(chunk, digest)
    return digest == member.CRC


def register_csv_views(
    con: Any,
    csv_dir: str | Path,
    *,
    domains: Iterable[str] | None = None,
    all_varchar: bool = True,
) -> None:
    """Create one DuckDB view per raw CSV domain.

    Raw CSV values are registered as varchar by default to keep the import
    lossless; downstream views cast numeric day/dose fields only where needed.
    """

    csv_dir = Path(csv_dir)
    selected = tuple(normalize_domain(domain) for domain in (domains or RAW_DOMAIN_ORDER))
    for domain in selected:
        path = csv_dir / f"{domain}.csv"
        if not path.exists():
            continue
        view_name = domain_view_name(domain)
        parallel = "false" if domain == "CO" else "true"
        con.execute(
            f"""
            CREATE OR REPLACE VIEW {quote_ident(view_name)} AS
            SELECT *
            FROM read_csv_auto(
                {quote_literal(path)},
                header = true,
                all_varchar = {str(all_varchar).lower()},
                null_padding = true,
                parallel = {parallel}
            )
            """
        )


def create_derived_views(con: Any) -> None:
    """Create derived views needed for canonical cohort selection."""

    con.execute(_subject_demographics_sql())
    con.execute(_study_design_summary_sql())
    exposure_columns = table_columns(con, "ex")
    con.execute(_exposure_intervals_sql(exposure_columns, table_columns(con, "dm")))
    con.execute(_exposure_summary_sql())
    if table_columns(con, "bw"):
        con.execute(_baseline_body_weight_sql())
    if table_columns(con, "ds"):
        con.execute(_subject_disposition_sql())


def open_archive(
    archive: str | Path,
    extract_dir: str | Path,
    *,
    database: str | Path | None = None,
    domains: Iterable[str] | None = None,
    overwrite_extract: bool = False,
) -> Any:
    """Extract a SEND archive, register raw views, and create derived views.

    Required cohort domains are always included. ``database`` defaults to an
    in-memory DuckDB instance so no file is written unless requested.
    """

    selected_domains = (
        tuple(
            sorted(
                _REQUIRED_COHORT_DOMAINS
                | {normalize_domain(domain) for domain in domains}
            )
        )
        if domains is not None
        else None
    )
    con = connect(database)
    csv_dir = ensure_extracted(
        archive=archive,
        extract_dir=extract_dir,
        domains=selected_domains,
        overwrite=overwrite_extract,
    )
    register_csv_views(con, csv_dir, domains=selected_domains)
    validate_derived_source_schema(con)
    create_derived_views(con)
    return con


def open_directory(
    csv_dir: str | Path,
    *,
    database: str | Path | None = None,
    domains: Iterable[str] | None = None,
) -> Any:
    """Register an already-extracted SEND CSV directory and create derived views."""

    selected_domains = (
        tuple(
            sorted(
                _REQUIRED_COHORT_DOMAINS
                | {normalize_domain(domain) for domain in domains}
            )
        )
        if domains is not None
        else None
    )
    con = connect(database)
    register_csv_views(con, csv_dir, domains=selected_domains)
    validate_derived_source_schema(con)
    create_derived_views(con)
    return con


def table_columns(con: Any, table: str) -> tuple[str, ...]:
    """Return the column names of a table or view, or ``()`` if absent.

    DuckDB raises a binder/catalog exception (subclass varies by version) when
    the relation is missing, so the existence probe is intentionally broad.
    """

    try:
        cursor = con.execute(f"SELECT * FROM {quote_ident(table)} LIMIT 0")
    except Exception:  # noqa: BLE001 - missing relation probe, see docstring
        return ()
    return tuple(item[0] for item in cursor.description)


def normalize_domain(domain: str) -> str:
    value = domain.strip().upper()
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Invalid domain name: {domain!r}")
    return value


def domain_view_name(domain: str) -> str:
    return normalize_domain(domain).lower()


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def quote_literal(value: str | Path) -> str:
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _subject_demographics_sql() -> str:
    raw_age = f"try_cast(nullif({q(AGE)}, '') AS DOUBLE)"
    age_unit = f"upper(trim({q(AGE_UNIT)}))"
    age_factor = f"""
        CASE {age_unit}
            WHEN 'DAY' THEN 1
            WHEN 'DAYS' THEN 1
            WHEN 'D' THEN 1
            WHEN 'WEEK' THEN 7
            WHEN 'WEEKS' THEN 7
            WHEN 'WK' THEN 7
            WHEN 'WKS' THEN 7
            WHEN 'MONTH' THEN 30.4375
            WHEN 'MONTHS' THEN 30.4375
            WHEN 'MO' THEN 30.4375
            WHEN 'MOS' THEN 30.4375
            WHEN 'YEAR' THEN 365.25
            WHEN 'YEARS' THEN 365.25
            WHEN 'YR' THEN 365.25
            WHEN 'YRS' THEN 365.25
        END
    """
    return f"""
    CREATE OR REPLACE VIEW subject_demographics AS
    SELECT
        {q(STUDY_ID)},
        {q(SUBJECT_ID)},
        {q(SPECIES)},
        {q(STRAIN)},
        {q(SEX)},
        CASE
            WHEN isfinite({raw_age}) AND {raw_age} >= 0
            THEN {raw_age} * ({age_factor})
        END AS age_days
    FROM dm
    """


def _duration_days_sql(value_expression: str) -> str:
    """Return strict SQL for SEND duration values normalized to days."""
    normalized = f"upper(trim(cast({value_expression} AS VARCHAR)))"
    number = f"try_cast(regexp_extract({normalized}, '([0-9]+(?:\\.[0-9]+)?)', 1) AS DOUBLE)"
    return f"""
        CASE
            WHEN regexp_full_match({normalized}, '^P[0-9]+(?:\\.[0-9]+)?D$')
                THEN {number}
            WHEN regexp_full_match({normalized}, '^P[0-9]+(?:\\.[0-9]+)?W$')
                THEN {number} * 7
            WHEN regexp_full_match(
                {normalized},
                '^[0-9]+(?:\\.[0-9]+)?(?:\\s*D(?:AY|AYS)?)?$'
            )
                THEN {number}
            WHEN regexp_full_match(
                {normalized},
                '^[0-9]+(?:\\.[0-9]+)?\\s*(?:W|WK|WKS|WEEK|WEEKS)$'
            )
                THEN {number} * 7
        END
    """


def _study_design_summary_sql() -> str:
    duration_days = _duration_days_sql(q(TS_VALUE))
    study_length_row = (
        f"upper({q(TS_PARAMETER_CODE)}) = {quote_literal(STUDY_LENGTH_PARAMETER_CODE)}"
    )
    dose_duration_row = (
        f"upper({q(TS_PARAMETER_CODE)}) = {quote_literal(DOSE_DURATION_PARAMETER_CODE)}"
    )
    parsed_study_length_row = f"{study_length_row} AND {duration_days} IS NOT NULL"
    parsed_dose_duration_row = f"{dose_duration_row} AND {duration_days} IS NOT NULL"
    nonempty_ts_value = f"nullif(trim({q(TS_VALUE)}), '') IS NOT NULL"
    return f"""
    CREATE OR REPLACE VIEW study_design_summary AS
    SELECT
        {q(STUDY_ID)},
        CASE
            WHEN count(DISTINCT {duration_days}) FILTER (
                WHERE {parsed_study_length_row}
            ) = 1
            THEN min({duration_days}) FILTER (
                WHERE {parsed_study_length_row}
            )
        END AS study_length_days,
        string_agg(
            DISTINCT {q(TS_VALUE)},
            ' | ' ORDER BY {q(TS_VALUE)}
        ) FILTER (
            WHERE {study_length_row}
        ) AS study_length_values,
        CASE
            WHEN count(DISTINCT {duration_days}) FILTER (
                WHERE {parsed_dose_duration_row}
            ) = 1
            THEN min({duration_days}) FILTER (
                WHERE {parsed_dose_duration_row}
            )
        END AS study_planned_dose_duration_days,
        string_agg(
            DISTINCT {q(TS_VALUE)},
            ' | ' ORDER BY {q(TS_VALUE)}
        ) FILTER (
            WHERE {dose_duration_row}
        ) AS declared_dose_duration_values,
        list(DISTINCT {duration_days} ORDER BY {duration_days}) FILTER (
            WHERE {parsed_dose_duration_row}
        ) AS planned_dose_duration_day_values,
        list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (
            WHERE {dose_duration_row}
              AND {nonempty_ts_value}
              AND {duration_days} IS NULL
        ) AS unparsed_dose_duration_values,
        count(*) FILTER (
            WHERE {dose_duration_row}
              AND {nonempty_ts_value}
              AND {duration_days} IS NULL
        ) > 0 AS dose_duration_declaration_incomplete,
        list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (
            WHERE upper({q(TS_PARAMETER_CODE)}) = {quote_literal(SUPPLIER_PARAMETER_CODE)}
                AND {nonempty_ts_value}
        ) AS supplier_values,
        list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (
            WHERE upper({q(TS_PARAMETER_CODE)}) = {quote_literal(STUDY_TYPE_PARAMETER_CODE)}
                AND {nonempty_ts_value}
        ) AS study_type_values,
        list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (
            WHERE upper({q(TS_PARAMETER_CODE)}) = {quote_literal(TEST_FACILITY_PARAMETER_CODE)}
                AND {nonempty_ts_value}
        ) AS test_facility_values,
        list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (
            WHERE upper({q(TS_PARAMETER_CODE)}) = {quote_literal(GLP_STATUS_PARAMETER_CODE)}
                AND {nonempty_ts_value}
        ) AS glp_status_values
    FROM ts
    GROUP BY {q(STUDY_ID)}
    """


def _exposure_intervals_sql(
    exposure_columns: Iterable[str], demographics_columns: Iterable[str]
) -> str:
    """Preserve and normalize every EX interval without expanding calendar rows."""

    exposure = set(exposure_columns)
    demographics = set(demographics_columns)

    def raw(column: str) -> str:
        if column not in exposure:
            return "CAST(NULL AS VARCHAR)"
        return f"cast(ex.{q(column)} AS VARCHAR)"

    sequence = raw(EX_SEQUENCE)
    start_datetime = raw(EX_START)
    end_datetime = raw(EX_END)
    start_day = raw(EX_START_DAY)
    end_day = raw(EX_END_DAY)
    reference_datetime = (
        f"cast(dm.{q('RFSTDTC')} AS VARCHAR)"
        if "RFSTDTC" in demographics
        else "CAST(NULL AS VARCHAR)"
    )
    return f"""
    CREATE OR REPLACE VIEW subject_exposure_intervals AS
    WITH raw AS (
        SELECT
            ex.{q(STUDY_ID)},
            ex.{q(SUBJECT_ID)},
            {sequence} AS exposure_sequence,
            {start_datetime} AS raw_start_datetime,
            {end_datetime} AS raw_end_datetime,
            {start_day} AS raw_start_day,
            {end_day} AS raw_end_day,
            {reference_datetime} AS raw_reference_datetime,
            ex.{q(EX_ROUTE)} AS route_value,
            ex.{q(EX_DOSE_FREQUENCY)} AS dose_frequency_value,
            ex.{q(EX_TREATMENT)} AS treatment_value
        FROM ex
        INNER JOIN dm
            ON ex.{q(STUDY_ID)} = dm.{q(STUDY_ID)}
           AND ex.{q(SUBJECT_ID)} = dm.{q(SUBJECT_ID)}
    ),
    parsed AS (
        SELECT
            *,
            try_cast(nullif(trim(raw_start_datetime), '') AS TIMESTAMP) AS start_timestamp,
            try_cast(nullif(trim(raw_end_datetime), '') AS TIMESTAMP) AS end_timestamp,
            try_cast(nullif(trim(raw_reference_datetime), '') AS TIMESTAMP)
                AS reference_timestamp,
            try_cast(nullif(trim(raw_start_day), '') AS INTEGER) AS start_study_day,
            try_cast(nullif(trim(raw_end_day), '') AS INTEGER) AS end_study_day,
            nullif(trim(raw_start_datetime), '') IS NOT NULL
                OR nullif(trim(raw_end_datetime), '') IS NOT NULL AS has_date_evidence,
            nullif(trim(raw_start_datetime), '') IS NOT NULL
                AND nullif(trim(raw_end_datetime), '') IS NOT NULL
                AS has_complete_date_bounds
        FROM raw
    ),
    normalized AS (
        SELECT
            *,
            cast(start_timestamp AS DATE) AS start_date,
            cast(end_timestamp AS DATE) AS end_date,
            cast(reference_timestamp AS DATE) AS reference_date,
            CASE
                WHEN has_complete_date_bounds
                 AND start_timestamp IS NOT NULL
                 AND end_timestamp IS NOT NULL
                 AND end_timestamp >= start_timestamp
                THEN 'date'
                WHEN NOT has_complete_date_bounds
                 AND start_study_day IS NOT NULL
                 AND end_study_day IS NOT NULL
                 AND start_study_day <= end_study_day
                 AND start_study_day <> 0
                 AND end_study_day <> 0
                 AND NOT (start_study_day < 0 AND end_study_day > 0)
                THEN 'study_day'
            END AS interval_source
        FROM parsed
    )
    SELECT
        {q(STUDY_ID)},
        {q(SUBJECT_ID)},
        exposure_sequence,
        raw_start_datetime,
        raw_end_datetime,
        raw_start_day,
        raw_end_day,
        start_timestamp,
        end_timestamp,
        reference_timestamp,
        start_study_day,
        end_study_day,
        interval_source,
        has_date_evidence,
        has_complete_date_bounds,
        CASE
            WHEN interval_source = 'date'
            THEN date_diff('day', DATE '1970-01-01', start_date)
            WHEN interval_source = 'study_day'
            THEN start_study_day
        END AS interval_start_position,
        CASE
            WHEN interval_source = 'date'
            THEN date_diff('day', DATE '1970-01-01', end_date)
            WHEN interval_source = 'study_day'
            THEN end_study_day
        END AS interval_end_position,
        CASE
            WHEN interval_source = 'date'
            THEN date_diff('day', start_date, end_date) + 1
            WHEN interval_source = 'study_day'
            THEN end_study_day - start_study_day + 1
        END AS interval_duration_days,
        CASE
            WHEN interval_source = 'date'
             AND reference_date IS NOT NULL
             AND start_date < reference_date
             AND end_date >= reference_date
            THEN TRUE
            ELSE FALSE
        END AS crosses_reference_date,
        CASE
            WHEN interval_source IS NOT NULL THEN NULL
            WHEN has_complete_date_bounds
             AND (start_timestamp IS NULL OR end_timestamp IS NULL)
            THEN 'incomplete_or_invalid_date_bounds'
            WHEN has_complete_date_bounds AND end_timestamp < start_timestamp
            THEN 'inverted_date_bounds'
            WHEN start_study_day IS NULL OR end_study_day IS NULL
            THEN 'missing_study_day_bounds'
            WHEN start_study_day > end_study_day
            THEN 'inverted_study_day_bounds'
            WHEN start_study_day = 0 OR end_study_day = 0
            THEN 'invalid_study_day_zero'
            WHEN start_study_day < 0 AND end_study_day > 0
            THEN 'crosses_missing_study_day_zero'
            ELSE 'invalid_interval'
        END AS interval_issue,
        route_value,
        dose_frequency_value,
        treatment_value
    FROM normalized
    """


def _exposure_summary_sql() -> str:
    """Resolve planned duration separately from complete observed EX coverage."""

    return f"""
    CREATE OR REPLACE VIEW subject_exposure_summary AS
    WITH raw_summary AS (
        SELECT
            {q(STUDY_ID)},
            {q(SUBJECT_ID)},
            count(*) AS exposure_row_count,
            min(start_study_day) AS first_exposure_day,
            max(end_study_day) AS last_exposure_day,
            CASE
                WHEN count(*) FILTER (WHERE interval_source = 'date') > 0 THEN 'date'
                ELSE 'study_day'
            END AS preferred_interval_source,
            list(route_value) AS route_values,
            list(dose_frequency_value) AS dose_frequency_values,
            list(treatment_value) AS treatment_values
        FROM subject_exposure_intervals
        GROUP BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
    ),
    preferred AS (
        SELECT intervals.*
        FROM subject_exposure_intervals AS intervals
        INNER JOIN raw_summary AS summary
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        WHERE intervals.interval_source = summary.preferred_interval_source
    ),
    ordered AS (
        SELECT
            *,
            max(interval_end_position) OVER (
                PARTITION BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
                ORDER BY interval_start_position, interval_end_position, exposure_sequence
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS previous_max_end
        FROM preferred
    ),
    grouped AS (
        SELECT
            *,
            sum(
                CASE
                    WHEN previous_max_end IS NULL
                      OR interval_start_position > previous_max_end + 1
                    THEN 1 ELSE 0
                END
            ) OVER (
                PARTITION BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
                ORDER BY interval_start_position, interval_end_position, exposure_sequence
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS merged_interval_group
        FROM ordered
    ),
    merged AS (
        SELECT
            {q(STUDY_ID)},
            {q(SUBJECT_ID)},
            merged_interval_group,
            min(interval_start_position) AS interval_start_position,
            max(interval_end_position) AS interval_end_position
        FROM grouped
        GROUP BY {q(STUDY_ID)}, {q(SUBJECT_ID)}, merged_interval_group
    ),
    preferred_counts AS (
        SELECT {q(STUDY_ID)}, {q(SUBJECT_ID)}, count(*) AS valid_interval_count
        FROM preferred
        GROUP BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
    ),
    merged_summary AS (
        SELECT
            {q(STUDY_ID)},
            {q(SUBJECT_ID)},
            count(*) AS merged_interval_count,
            sum(interval_end_position - interval_start_position + 1)
                AS merged_coverage_days,
            min(interval_start_position) AS observed_start_position,
            max(interval_end_position) AS observed_end_position
        FROM merged
        GROUP BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
    ),
    evidence AS (
        SELECT
            summary.*,
            coalesce(counts.valid_interval_count, 0) AS valid_interval_count,
            coalesce(merged.merged_interval_count, 0) AS merged_interval_count,
            merged.observed_start_position,
            merged.observed_end_position,
            CASE
                WHEN counts.valid_interval_count = summary.exposure_row_count
                 AND merged.merged_interval_count = 1
                THEN merged.merged_coverage_days
            END AS observed_exposure_span_days,
            CASE
                WHEN counts.valid_interval_count IS NULL THEN 'incomplete'
                WHEN counts.valid_interval_count <> summary.exposure_row_count THEN 'incomplete'
                WHEN merged.merged_interval_count > 1 THEN 'gapped'
                WHEN merged.merged_interval_count = 1 THEN 'complete_contiguous'
                ELSE 'unavailable'
            END AS exposure_interval_status
        FROM raw_summary AS summary
        LEFT JOIN preferred_counts AS counts
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        LEFT JOIN merged_summary AS merged
            USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
    ),
    candidate AS (
        SELECT
            dm.{q(STUDY_ID)},
            dm.{q(SUBJECT_ID)},
            evidence.* EXCLUDE ({q(STUDY_ID)}, {q(SUBJECT_ID)}),
            studies.declared_dose_duration_values,
            studies.planned_dose_duration_day_values,
            studies.unparsed_dose_duration_values,
            coalesce(studies.dose_duration_declaration_incomplete, FALSE)
                AS dose_duration_declaration_incomplete,
            CASE
                WHEN coalesce(len(studies.planned_dose_duration_day_values), 0) = 1
                THEN list_extract(studies.planned_dose_duration_day_values, 1)
                WHEN evidence.observed_exposure_span_days IS NOT NULL
                 AND list_contains(
                    studies.planned_dose_duration_day_values,
                    evidence.observed_exposure_span_days
                 )
                THEN evidence.observed_exposure_span_days
            END AS planned_dose_duration_days
        FROM dm
        LEFT JOIN evidence USING ({q(STUDY_ID)}, {q(SUBJECT_ID)})
        LEFT JOIN study_design_summary AS studies USING ({q(STUDY_ID)})
    )
    SELECT
        *,
        CASE
            WHEN coalesce(len(planned_dose_duration_day_values), 0) = 0
            THEN 'unresolved'
            WHEN planned_dose_duration_days IS NULL
            THEN 'ambiguous'
            WHEN observed_exposure_span_days IS NULL
            THEN 'declared_only'
            WHEN observed_exposure_span_days = planned_dose_duration_days
            THEN 'corroborated'
            WHEN observed_exposure_span_days < planned_dose_duration_days
            THEN 'shorter_than_planned'
            ELSE 'ambiguous'
        END AS dose_duration_evidence_status
    FROM candidate
    """


def _subject_disposition_sql() -> str:
    """Classify DS disposition evidence into canonical per-subject categories.

    ``terminal_sacrifice_offset_day_values`` holds every terminal-sacrifice
    study day minus the subject's planned dose duration, so window predicates
    resolve with ``any value`` semantics over the terminal rows.
    """

    return """
    CREATE OR REPLACE VIEW subject_disposition_summary AS
    WITH classified AS (
        SELECT
            ds."STUDYID",
            ds."USUBJID",
            CASE upper(trim(ds."DSDECOD"))
                WHEN 'TERMINAL SACRIFICE' THEN 'TERMINAL'
                WHEN 'TERMINAL SACRIFICED' THEN 'TERMINAL'
                WHEN 'SCHEDULED REMOVAL (TERMINAL)' THEN 'TERMINAL'
                WHEN 'SCHEDULED SACRIFICE' THEN 'TERMINAL'
                WHEN 'NON-MORIBUND SACRIFICE' THEN 'TERMINAL'
                WHEN 'RECOVERY SACRIFICE' THEN 'RECOVERY'
                WHEN 'SCHEDULED REMOVAL (RECOVERY)' THEN 'RECOVERY'
                WHEN 'INTERIM SACRIFICE' THEN 'INTERIM'
                WHEN 'MORIBUND SACRIFICE' THEN 'UNSCHEDULED_DEATH'
                WHEN 'FOUND DEAD' THEN 'UNSCHEDULED_DEATH'
                WHEN 'ACCIDENTAL DEATH' THEN 'UNSCHEDULED_DEATH'
                WHEN 'REMOVED FROM STUDY ALIVE' THEN 'REMOVED_ALIVE'
            END AS category,
            try_cast(ds."DSSTDY" AS INTEGER) AS study_day,
            exposure.planned_dose_duration_days
        FROM ds
        INNER JOIN dm USING ("STUDYID", "USUBJID")
        LEFT JOIN subject_exposure_summary AS exposure
            USING ("STUDYID", "USUBJID")
        WHERE nullif(trim(ds."DSDECOD"), '') IS NOT NULL
    )
    SELECT
        "STUDYID",
        "USUBJID",
        list(DISTINCT category ORDER BY category) AS disposition_category_values,
        list(
            DISTINCT study_day - planned_dose_duration_days
            ORDER BY study_day - planned_dose_duration_days
        ) FILTER (
            WHERE category = 'TERMINAL'
              AND study_day IS NOT NULL
              AND planned_dose_duration_days IS NOT NULL
        ) AS terminal_sacrifice_offset_day_values
    FROM classified
    WHERE category IS NOT NULL
    GROUP BY "STUDYID", "USUBJID"
    """


def _baseline_body_weight_sql() -> str:
    """Derive one strict pre-study baseline body weight per subject."""

    return """
    CREATE OR REPLACE VIEW subject_baseline_body_weight AS
    WITH candidates AS (
        SELECT
            bw."STUDYID",
            bw."USUBJID",
            try_cast(bw."BWSTRESN" AS DOUBLE) AS baseline_body_weight_g,
            try_cast(bw."BWDY" AS INTEGER) AS study_day,
            try_cast(bw."BWDTC" AS TIMESTAMP) AS measurement_time,
            rank() OVER (
                PARTITION BY bw."STUDYID", bw."USUBJID"
                ORDER BY
                    try_cast(bw."BWDY" AS INTEGER) DESC NULLS LAST,
                    try_cast(bw."BWDTC" AS TIMESTAMP) DESC NULLS LAST
            ) AS candidate_rank
        FROM bw
        INNER JOIN dm USING ("STUDYID", "USUBJID")
        LEFT JOIN subject_exposure_summary AS exposure
            USING ("STUDYID", "USUBJID")
        WHERE isfinite(try_cast(bw."BWSTRESN" AS DOUBLE))
          AND upper(trim(bw."BWSTRESU")) = 'G'
          AND (
              upper(trim(bw."BWTESTCD")) IN ('BW', 'AA01')
              OR (
                  nullif(trim(bw."BWTESTCD"), '') IS NULL
                  AND nullif(trim(bw."BWTEST"), '') IS NULL
              )
          )
          AND upper(trim(coalesce(bw."BWTESTCD", ''))) NOT IN ('TERMBW', 'RA13', 'TBW1')
          AND upper(trim(coalesce(bw."BWTEST", ''))) <> 'TERMINAL BODY WEIGHT'
          AND (
              try_cast(bw."BWDY" AS INTEGER)
                  < coalesce(exposure.first_exposure_day, 1)
              OR (
                  try_cast(bw."BWDY" AS INTEGER) IS NULL
                  AND try_cast(bw."BWDTC" AS TIMESTAMP) < try_cast(dm."RFSTDTC" AS TIMESTAMP)
              )
          )
    )
    SELECT
        "STUDYID",
        "USUBJID",
        CASE WHEN count(DISTINCT baseline_body_weight_g) = 1
             THEN min(baseline_body_weight_g)
        END AS baseline_body_weight_g
    FROM candidates
    WHERE candidate_rank = 1
    GROUP BY "STUDYID", "USUBJID"
    """


def q(identifier: str) -> str:
    return quote_ident(identifier)


__all__ = [
    "QuerySpec",
    "archive_sha256",
    "connect",
    "create_derived_views",
    "domain_view_name",
    "ensure_extracted",
    "normalize_domain",
    "open_archive",
    "open_directory",
    "q",
    "quote_ident",
    "quote_literal",
    "register_csv_views",
    "table_columns",
]
