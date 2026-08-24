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

from .send_columns import (
    AGE,
    AGE_UNIT,
    EX_DOSE_FREQUENCY,
    EX_ROUTE,
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
    con.execute(_exposure_summary_sql())
    if table_columns(con, "bw"):
        con.execute(_baseline_body_weight_sql())


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

    required_domains = {"DM", "TX", "TA", "EX", "SE", "TS"}
    selected_domains = (
        tuple(sorted(required_domains | {normalize_domain(domain) for domain in domains}))
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
    create_derived_views(con)
    return con


def open_directory(
    csv_dir: str | Path,
    *,
    database: str | Path | None = None,
    domains: Iterable[str] | None = None,
) -> Any:
    """Register an already-extracted SEND CSV directory and create derived views."""

    con = connect(database)
    register_csv_views(con, csv_dir, domains=domains)
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
    study_length_days = _duration_days_sql(q(TS_VALUE))
    dose_duration_row = (
        f"upper({q(TS_PARAMETER_CODE)}) = {quote_literal(STUDY_LENGTH_PARAMETER_CODE)}"
    )
    parsed_duration_row = f"{dose_duration_row} AND {study_length_days} IS NOT NULL"
    nonempty_ts_value = f"nullif(trim({q(TS_VALUE)}), '') IS NOT NULL"
    return f"""
    CREATE OR REPLACE VIEW study_design_summary AS
    SELECT
        {q(STUDY_ID)},
        CASE
            WHEN count(DISTINCT {study_length_days}) FILTER (
                WHERE {parsed_duration_row}
            ) = 1
            THEN min({study_length_days}) FILTER (
                WHERE {parsed_duration_row}
            )
        END AS study_length_days,
        string_agg(
            DISTINCT {q(TS_VALUE)},
            ' | ' ORDER BY {q(TS_VALUE)}
        ) FILTER (
            WHERE {dose_duration_row}
        ) AS study_length_values,
        list(DISTINCT {study_length_days} ORDER BY {study_length_days}) FILTER (
            WHERE {parsed_duration_row}
        ) AS dose_duration_day_values,
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


def _exposure_summary_sql() -> str:
    start_day = f"try_cast(nullif({q(EX_START_DAY)}, '') AS INTEGER)"
    return f"""
    CREATE OR REPLACE VIEW subject_exposure_summary AS
    SELECT
        {q(STUDY_ID)},
        {q(SUBJECT_ID)},
        min({start_day}) AS first_exposure_day,
        -- Canonical cohort selectors sourced from EX as lossless per-subject
        -- lists so exact membership never splits a source value.
        list({q(EX_ROUTE)}) AS route_values,
        list({q(EX_DOSE_FREQUENCY)}) AS dose_frequency_values,
        list({q(EX_TREATMENT)}) AS treatment_values
    FROM ex
    GROUP BY {q(STUDY_ID)}, {q(SUBJECT_ID)}
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
