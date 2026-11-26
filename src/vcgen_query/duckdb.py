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
import uuid
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZipInfo

import duckdb

from .control_arm import control_arm_sql
from .endpoint_catalogue import canonical_endpoint
from .facility import facility_group
from .send_semantics import MISSING_UNIT_LABELS, day_columns
from .source_validation import validate_derived_source_schema
from .sql_identifiers import quote_ident, table_columns
from .send_columns import (
    AGE,
    AGE_UNIT,
    ARM,
    DOSE_DURATION_PARAMETER_CODE,
    DOSE_FREQUENCY_PARAMETER_CODE,
    EX_DOSE_FREQUENCY,
    EX_END,
    EX_END_DAY,
    EX_METHOD,
    EX_ROUTE,
    EX_SEQUENCE,
    EX_START,
    EX_START_DAY,
    EX_TREATMENT_VALUE,
    GLP_STATUS_PARAMETER_CODE,
    RAW_DOMAIN_ORDER,
    ROUTE_PARAMETER_CODE,
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
    VEHICLE_PARAMETER_CODE,
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


_ARCHIVE_DIGESTS: dict[tuple[str, int, int, int], str] = {}


def archive_sha256(archive: str | Path) -> str:
    """Return the SHA-256 of a SEND archive for content-aware caching.

    One process hashes one file version once. The key includes the inode,
    size, and modification time, so a replaced archive is hashed again.
    """

    archive = Path(archive).resolve()
    stat = archive.stat()
    key = (str(archive), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if key not in _ARCHIVE_DIGESTS:
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        _ARCHIVE_DIGESTS[key] = digest.hexdigest()
    return _ARCHIVE_DIGESTS[key]


def archive_source_identity(archive: str | Path) -> str:
    """The DVC identity of a SEND archive, ``dvc-md5:<md5>``: the key that every source record uses."""

    digest = hashlib.md5()
    with Path(archive).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"dvc-md5:{digest.hexdigest()}"


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


def _csv_source_sql(path: Path, domain: str, *, all_varchar: bool = True) -> str:
    # Fix the quote rule: the sniffer reads only the first rows, and turns quoting
    # off when none of them is quoted.
    parallel = "false" if domain == "CO" else "true"
    return f"""
            SELECT *
            FROM read_csv_auto(
                {quote_literal(path)},
                header = true,
                quote = '"',
                escape = '"',
                all_varchar = {str(all_varchar).lower()},
                null_padding = true,
                parallel = {parallel}
            )
            """


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
        con.execute(
            f"CREATE OR REPLACE VIEW {quote_ident(view_name)} AS "
            f"{_csv_source_sql(path, domain, all_varchar=all_varchar)}"
        )


# An archive source is one DuckDB database: each raw SEND domain and each
# derived view is a table. The scientific pipeline and the service image build
# it with the same functions. A domain table is reused only when its archive
# and zip member identity match; the derived tables are reused only when the
# domain records and the derived SQL match. Change the format when the CSV read
# options change.
_SOURCE_TABLES = "__vcgen_source_tables"
_SOURCE_TABLE_FORMAT = "vcgen.source_table.v2:read_csv_auto(header,quote,escape,all_varchar,null_padding)"
_DERIVED_RECORD = "__derived__"


def _archive_members(archive: Path) -> dict[str, ZipInfo]:
    with ZipFile(archive) as zf:
        members = {
            normalize_domain(Path(member.filename).stem): member
            for member in zf.infolist()
            if Path(member.filename).suffix.lower() == ".csv"
        }
    return {domain: members[domain] for domain in RAW_DOMAIN_ORDER if domain in members}


def _source_table_records(con: Any) -> dict[str, tuple[str, int, int, str]]:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {quote_ident(_SOURCE_TABLES)} ("
        "domain VARCHAR PRIMARY KEY, archive_sha256 VARCHAR, member_crc BIGINT, "
        "member_size BIGINT, table_format VARCHAR)"
    )
    return _read_source_records(con)


def _read_source_records(con: Any) -> dict[str, tuple[str, int, int, str]]:
    """Read the stored source records; unlike the caller above, it never creates the table."""

    return {
        str(domain): (str(digest), int(crc), int(size), str(table_format))
        for domain, digest, crc, size, table_format in con.execute(
            f"SELECT domain, archive_sha256, member_crc, member_size, table_format "
            f"FROM {quote_ident(_SOURCE_TABLES)}"
        ).fetchall()
    }


def _set_record(con: Any, domain: str, values: tuple[str, int, int, str]) -> None:
    con.execute(f"DELETE FROM {quote_ident(_SOURCE_TABLES)} WHERE domain = ?", [domain])
    con.execute(
        f"INSERT INTO {quote_ident(_SOURCE_TABLES)} VALUES (?, ?, ?, ?, ?)", [domain, *values]
    )


def _relation_kind(con: Any, name: str) -> str | None:
    # The catalogue functions are much faster than information_schema.tables.
    row = con.execute(
        "SELECT 'BASE TABLE' FROM duckdb_tables() WHERE schema_name = 'main' AND table_name = ? "
        "UNION ALL SELECT 'VIEW' FROM duckdb_views() WHERE schema_name = 'main' AND view_name = ? AND NOT internal",
        [name, name],
    ).fetchone()
    return None if row is None else str(row[0])


def _drop_relation(con: Any, name: str) -> None:
    kind = _relation_kind(con, name)
    if kind == "VIEW":
        con.execute(f"DROP VIEW {quote_ident(name)}")
    elif kind is not None:
        con.execute(f"DROP TABLE {quote_ident(name)}")


def materialize_archive_tables(
    con: Any,
    archive: str | Path,
    extract_dir: str | Path,
    *,
    overwrite_extract: bool = False,
) -> bool:
    """Make each raw archive domain a table that matches the archive.

    The domains are the archive CSV members named in ``RAW_DOMAIN_ORDER``. A
    table holds the CSV rows as varchar. It is reused only when its record
    names the current archive SHA-256 and the zip member CRC and size.
    Otherwise the member is extracted and verified with
    :func:`ensure_extracted`, then loaded in one transaction. Tables that were
    loaded from another archive are dropped. Returns whether a table changed.
    """

    archive = Path(archive)
    digest = archive_sha256(archive)
    members = _archive_members(archive)
    records = _source_table_records(con)
    changed = False
    for domain, record in records.items():
        if domain != _DERIVED_RECORD and (record[0] != digest or domain not in members):
            con.execute("BEGIN TRANSACTION")
            _drop_relation(con, domain_view_name(domain))
            con.execute(f"DELETE FROM {quote_ident(_SOURCE_TABLES)} WHERE domain = ?", [domain])
            con.execute("COMMIT")
            changed = True

    def identity(domain: str) -> tuple[str, int, int, str]:
        member = members[domain]
        return (digest, member.CRC, member.file_size, _SOURCE_TABLE_FORMAT)

    stale = tuple(
        domain
        for domain in members
        if overwrite_extract
        or records.get(domain) != identity(domain)
        or _relation_kind(con, domain_view_name(domain)) != "BASE TABLE"
    )
    if not stale:
        return changed
    csv_dir = ensure_extracted(
        archive=archive, extract_dir=extract_dir, domains=stale, overwrite=overwrite_extract
    )
    for domain in stale:
        name = domain_view_name(domain)
        building = f"__vcgen_building_{name}"
        con.execute("BEGIN TRANSACTION")
        con.execute(f"DROP TABLE IF EXISTS {quote_ident(building)}")
        con.execute(
            f"CREATE TABLE {quote_ident(building)} AS "
            f"{_csv_source_sql(csv_dir / f'{domain}.csv', domain)}"
        )
        _drop_relation(con, name)
        con.execute(f"ALTER TABLE {quote_ident(building)} RENAME TO {quote_ident(name)}")
        _set_record(con, domain, identity(domain))
        con.execute("COMMIT")
    return True


def register_facility_group_function(con: Any) -> None:
    """Make the query-layer category available on fresh and hosted connections."""

    try:
        con.create_function(
            "vcgen_facility_group", lambda value: facility_group(value)[0],
            ["VARCHAR"], "VARCHAR",
        )
    except duckdb.Error as error:
        if "already" not in str(error).lower() or "vcgen_facility_group" not in str(error):
            raise


def _derived_view_statements(con: Any) -> tuple[tuple[str, str], ...]:
    """Return (view name, CREATE VIEW statement) pairs in dependency order."""

    statements = [
        ("subject_demographics", _subject_demographics_sql()),
        ("study_design_summary", _study_design_summary_sql()),
        (
            "subject_exposure_intervals",
            _exposure_intervals_sql(table_columns(con, "ex"), table_columns(con, "dm")),
        ),
        ("subject_exposure_summary", _exposure_summary_sql()),
    ]
    if table_columns(con, "bw"):
        statements.append((
            "subject_baseline_body_weight",
            _baseline_body_weight_sql(table_columns(con, "bw"), table_columns(con, "dm")),
        ))
    necropsy_sources = tuple(
        (table, column) for table, column in NECROPSY_DAY_COLUMNS if column in table_columns(con, table)
    )
    if table_columns(con, "ds") or necropsy_sources:
        statements.append((
            "subject_disposition_summary",
            _subject_disposition_sql(bool(table_columns(con, "ds")), necropsy_sources),
        ))
    return tuple(statements)


def create_derived_views(con: Any) -> None:
    """Create derived views needed for canonical cohort selection.

    Views have no stored Python-derived tables; queries compute those values.
    """

    register_facility_group_function(con)
    for name in PYTHON_DERIVED_TABLES:
        _drop_relation(con, name)
    for _name, statement in _derived_view_statements(con):
        con.execute(statement)


# Two derived tables are computed in Python from the derived views: the
# normalized context of every subject, and the strict latest pre-study value
# of every catalogue endpoint for every subject. Queries read them only while
# the derived record names the current domain records, derived SQL and
# Python-derived identity. The identity hashes every module and data file of
# this package (such as the specimen vocabulary) and the identity of the active
# endpoint catalogue, so a moved function, an edited vocabulary or another
# catalogue cannot leave stale tables. Change the tag when their content
# changes for a reason that the hashed sources do not show.
SUBJECT_CONTEXT_TABLE = "subject_context"
PRE_STUDY_ENDPOINT_TABLE = "pre_study_endpoint_values"
PYTHON_DERIVED_TABLES = (SUBJECT_CONTEXT_TABLE, PRE_STUDY_ENDPOINT_TABLE)
_PYTHON_DERIVED_TAG = "vcgen.python_derived.v1"


@lru_cache(maxsize=1)
def _package_source_digests() -> tuple[tuple[str, str], ...]:
    """Return the digest of every module and data file of this package; the code is fixed per process."""

    package = Path(__file__).parent
    return tuple(
        (path.relative_to(package).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted([*package.glob("*.py"), *package.glob("data/*.yaml"), *package.glob("data/*.json")])
    )


def _python_derived_identity() -> str:
    """Return the identity of the code and the active endpoint catalogue behind the Python tables.

    The catalogue is not part of the package, and ``VCGEN_ENDPOINT_CATALOGUE`` can
    name another one, so its identity is read at each call and never cached here.
    """

    from .endpoint_catalogue import endpoint_catalogue_identity

    return hashlib.sha256(
        repr((_PYTHON_DERIVED_TAG, sorted(endpoint_catalogue_identity().items()), _package_source_digests())).encode(
            "utf-8"
        )
    ).hexdigest()


def _derived_record(
    records: dict[str, tuple[str, int, int, str]], statements: tuple[tuple[str, str], ...]
) -> tuple[str, int, int, str]:
    """Return the derived record for these domain records and derived SQL."""

    identity = hashlib.sha256(
        repr((
            sorted((domain, record) for domain, record in records.items() if domain != _DERIVED_RECORD),
            statements,
            _python_derived_identity(),
        )).encode("utf-8")
    ).hexdigest()
    return (identity, 0, 0, _SOURCE_TABLE_FORMAT)


def python_derived_tables_current(con: Any) -> bool:
    """Return whether the stored Python-derived tables match this code and the domain tables.

    This only reads, so it works on a read-only connection. A connection
    without stored tables, such as one from :func:`open_directory`, returns
    false.
    """

    try:
        records = _read_source_records(con)
    except duckdb.CatalogException:
        return False
    return records.get(_DERIVED_RECORD) == _derived_record(
        records, _derived_view_statements(con)
    ) and all(_relation_kind(con, name) == "BASE TABLE" for name in PYTHON_DERIVED_TABLES)


_DERIVED_VIEWS = (
    "subject_demographics",
    "study_design_summary",
    "subject_exposure_intervals",
    "subject_exposure_summary",
    "subject_baseline_body_weight",
    "subject_disposition_summary",
)


def materialize_derived_tables(con: Any) -> bool:
    """Store each derived view as a table computed from the domain tables.

    The tables are reused while the domain records, the derived SQL, and the
    Python-derived identity are unchanged. A view that reads another derived
    view is computed after it. The Python-derived tables are computed last,
    from the derived tables. Returns whether the tables were rebuilt.
    """

    from .cohort import create_pre_study_endpoint_table, create_subject_context_table

    register_facility_group_function(con)
    statements = _derived_view_statements(con)
    records = _source_table_records(con)
    expected = _derived_record(records, statements)
    if (
        records.get(_DERIVED_RECORD) == expected
        and all(
            _relation_kind(con, name) == "BASE TABLE"
            for name in (*(name for name, _ in statements), *PYTHON_DERIVED_TABLES)
        )
    ):
        return False
    con.execute("BEGIN TRANSACTION")
    for name in (*_DERIVED_VIEWS, *PYTHON_DERIVED_TABLES):
        _drop_relation(con, name)
    for _name, statement in statements:
        con.execute(statement)
    for name, _ in statements:
        building = f"__vcgen_building_{name}"
        con.execute(f"CREATE TABLE {quote_ident(building)} AS SELECT * FROM {quote_ident(name)}")
        con.execute(f"DROP VIEW {quote_ident(name)}")
        con.execute(f"ALTER TABLE {quote_ident(building)} RENAME TO {quote_ident(name)}")
    create_subject_context_table(con, SUBJECT_CONTEXT_TABLE)
    create_pre_study_endpoint_table(con, PRE_STUDY_ENDPOINT_TABLE)
    _set_record(con, _DERIVED_RECORD, expected)
    con.execute("COMMIT")
    return True


def open_archive(
    archive: str | Path,
    extract_dir: str | Path,
    *,
    database: str | Path | None = None,
    overwrite_extract: bool = False,
) -> Any:
    """Open the source database for a SEND archive, building what is stale.

    The database holds every raw domain and derived view as a table, as
    described above. ``database`` defaults to an in-memory DuckDB instance; a
    file database keeps its tables for later calls with the same archive. The
    service image builds its reference database with this function.
    """

    con = open_source_tables(archive, extract_dir, database=database, overwrite_extract=overwrite_extract)
    materialize_derived_tables(con)
    return con


def open_source_tables(
    archive: str | Path,
    extract_dir: str | Path,
    *,
    database: str | Path | None = None,
    overwrite_extract: bool = False,
) -> Any:
    """Open the raw domain tables of a SEND archive, without the derived tables.

    The derived tables read the endpoint catalogue. The step that produces the
    catalogue reads the archive with this function, so it needs no catalogue.
    """

    con = connect(database)
    materialize_archive_tables(con, archive, extract_dir, overwrite_extract=overwrite_extract)
    validate_derived_source_schema(con)
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


def normalize_domain(domain: str) -> str:
    value = domain.strip().upper()
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Invalid domain name: {domain!r}")
    return value


def domain_view_name(domain: str) -> str:
    return normalize_domain(domain).lower()


@contextmanager
def registered_frame(con: Any, frame: Any) -> Iterator[str]:
    """Register ``frame`` on ``con`` for the block and yield its quoted name.

    The name is unique, so calls that share the connection do not replace
    each other's frames.
    """

    name = f"_vcgen_frame_{uuid.uuid4().hex}"
    con.register(name, frame)
    try:
        yield quote_ident(name)
    finally:
        con.unregister(name)


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


def _declared_values(code: str) -> str:
    """Return the sorted distinct non-blank TS values of one parameter code, per study."""
    return (
        f"list(DISTINCT {q(TS_VALUE)} ORDER BY {q(TS_VALUE)}) FILTER (WHERE "
        f"upper({q(TS_PARAMETER_CODE)}) = {quote_literal(code)} AND nullif(trim({q(TS_VALUE)}), '') IS NOT NULL)"
    )


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
        {_declared_values(SUPPLIER_PARAMETER_CODE)} AS supplier_values,
        {_declared_values(STUDY_TYPE_PARAMETER_CODE)} AS study_type_values,
        {_declared_values(TEST_FACILITY_PARAMETER_CODE)} AS test_facility_values,
        {_declared_values(GLP_STATUS_PARAMETER_CODE)} AS glp_status_values,
        {_declared_values(ROUTE_PARAMETER_CODE)} AS declared_route_values,
        {_declared_values(DOSE_FREQUENCY_PARAMETER_CODE)} AS declared_dose_frequency_values,
        {_declared_values(VEHICLE_PARAMETER_CODE)} AS declared_treatment_values
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
    method = raw(EX_METHOD)
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
            {method} AS method_value,
            ex.{q(EX_DOSE_FREQUENCY)} AS dose_frequency_value,
            {raw(EX_TREATMENT_VALUE)} AS treatment_value
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
        method_value,
        dose_frequency_value,
        treatment_value
    FROM normalized
    """


def _exposure_summary_sql() -> str:
    """Resolve planned duration separately from complete observed EX coverage.

    A subject of a study without EX rows (a control-only export) takes its route,
    dose frequency and treatment lists from the TS ROUTE, PDOSFRQ and TRTV values
    of its study when its DM ARM is empty or a control arm (`control_arm_sql`); a
    TS route has no method. A subject of another arm in such a study takes no TS
    values: the TS values describe the study, and a dosed animal must not pass as
    a vehicle control. A subject without EX rows in a study that has EX rows takes
    no TS values: its exposure is not known, and it can be a dosed animal. Every
    other column stays EX evidence.
    """
    ts_fallback = f"(exposed.has_exposure_rows IS NULL AND {control_arm_sql(f'dm.{q(ARM)}')})"
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
            -- One (route, method) pair per EX row; the route field resolves it through the alias table.
            list({{'route': route_value, 'method': method_value}}) AS route_method_values,
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
    exposed_studies AS (
        SELECT DISTINCT {q(STUDY_ID)}, TRUE AS has_exposure_rows FROM raw_summary
    ),
    candidate AS (
        SELECT
            dm.{q(STUDY_ID)},
            dm.{q(SUBJECT_ID)},
            evidence.* EXCLUDE ({q(STUDY_ID)}, {q(SUBJECT_ID)}) REPLACE (
                CASE WHEN {ts_fallback} THEN studies.declared_route_values
                     ELSE evidence.route_values END AS route_values,
                CASE WHEN {ts_fallback}
                     THEN list_transform(studies.declared_route_values,
                                         lambda value: {{'route': value, 'method': CAST(NULL AS VARCHAR)}})
                     ELSE evidence.route_method_values END AS route_method_values,
                CASE WHEN {ts_fallback} THEN studies.declared_dose_frequency_values
                     ELSE evidence.dose_frequency_values END AS dose_frequency_values,
                CASE WHEN {ts_fallback} THEN studies.declared_treatment_values
                     ELSE evidence.treatment_values END AS treatment_values
            ),
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
        LEFT JOIN exposed_studies AS exposed USING ({q(STUDY_ID)})
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


# Pathology domains whose study day dates the necropsy of a subject without a
# DS record that has a DSDECOD.
NECROPSY_DAY_COLUMNS = (("mi", "MIDY"), ("ma", "MADY"), ("om", "OMDY"))
# The latest days of the pathology domains of one subject agree when they differ
# by this number of days or less: the width of the terminal sacrifice window
# above the planned dose duration (``config/population/base_query.yaml``).
PATHOLOGY_DAY_TOLERANCE_DAYS = 4


def _subject_disposition_sql(has_ds: bool, necropsy_sources: tuple[tuple[str, str], ...]) -> str:
    """Classify DS disposition evidence into canonical per-subject categories.

    ``terminal_sacrifice_offset_day_values`` holds every terminal-sacrifice
    study day minus the subject's planned dose duration, so window predicates
    resolve with ``any value`` semantics over the terminal rows.

    A subject without a DS record that has a DSDECOD (a control-only export has
    no DS) has one TERMINAL record dated by its necropsy: the earliest of the
    latest MI, the latest MA and the latest OM study day. A pathology day can be
    an evaluation or reporting date long after the necropsy (most often MIDY),
    so the latest of all the days is not the necropsy. The offset then separates
    main-study animals from recovery animals and early deaths, as a DS terminal
    sacrifice does. A DS record with an unlisted DSDECOD is a disposition that
    this code cannot classify: it gets no necropsy record.

    ``terminal_record_source`` is ``disposition`` for a DS terminal record and
    ``necropsy`` for this fallback. ``pathology_day_spread_days`` is, for a
    fallback subject, the latest minus the earliest of the per-domain latest
    days. The subject context stage makes its warnings from these two columns.
    """

    classified = """
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
            try_cast(ds."DSSTDY" AS INTEGER) AS study_day
        FROM ds
        INNER JOIN dm USING ("STUDYID", "USUBJID")
        WHERE nullif(trim(ds."DSDECOD"), '') IS NOT NULL
    """ if has_ds else """
        SELECT CAST(NULL AS VARCHAR) AS "STUDYID", CAST(NULL AS VARCHAR) AS "USUBJID",
               CAST(NULL AS VARCHAR) AS category, CAST(NULL AS INTEGER) AS study_day
        WHERE FALSE
    """
    days = " UNION ALL ".join(
        f'SELECT "STUDYID", "USUBJID", \'{table}\' AS pathology_domain, '
        f"try_cast(nullif(trim(cast({q(column)} AS VARCHAR)), \'\') AS INTEGER) AS study_day FROM {table}"
        for table, column in necropsy_sources
    ) or (
        'SELECT CAST(NULL AS VARCHAR) AS "STUDYID", CAST(NULL AS VARCHAR) AS "USUBJID", '
        "CAST(NULL AS VARCHAR) AS pathology_domain, CAST(NULL AS INTEGER) AS study_day WHERE FALSE"
    )
    return f"""
    CREATE OR REPLACE VIEW subject_disposition_summary AS
    WITH decoded AS ({classified}),
    classified AS (
        SELECT * FROM decoded WHERE category IS NOT NULL
    ),
    pathology_latest AS (
        SELECT pathology."STUDYID", pathology."USUBJID", pathology.pathology_domain,
               max(pathology.study_day) AS latest_day
        FROM ({days}) AS pathology
        INNER JOIN dm USING ("STUDYID", "USUBJID")
        WHERE pathology.study_day IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM decoded
              WHERE decoded."STUDYID" = pathology."STUDYID" AND decoded."USUBJID" = pathology."USUBJID")
        GROUP BY pathology."STUDYID", pathology."USUBJID", pathology.pathology_domain
    ),
    necropsy AS (
        SELECT "STUDYID", "USUBJID", 'TERMINAL' AS category,
               min(latest_day) AS study_day,
               TRUE AS from_necropsy,
               max(latest_day) - min(latest_day) AS pathology_day_spread_days
        FROM pathology_latest
        GROUP BY "STUDYID", "USUBJID"
    ),
    records AS (
        SELECT records.*, exposure.planned_dose_duration_days
        FROM (SELECT * FROM classified UNION ALL BY NAME SELECT * FROM necropsy) AS records
        LEFT JOIN subject_exposure_summary AS exposure USING ("STUDYID", "USUBJID")
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
        ) AS terminal_sacrifice_offset_day_values,
        CASE
            WHEN coalesce(bool_or(from_necropsy), FALSE) THEN 'necropsy'
            WHEN bool_or(category = 'TERMINAL') THEN 'disposition'
        END AS terminal_record_source,
        CAST(max(pathology_day_spread_days) AS INTEGER) AS pathology_day_spread_days
    FROM records
    GROUP BY "STUDYID", "USUBJID"
    """


def pre_study_timing_sql(
    domain: str, columns: tuple[str, ...], dm_columns: tuple[str, ...], alias: str
) -> tuple[str, str, str]:
    """Return study-day, measurement-time and reference-time SQL for one domain.

    The study day is the first readable day column, the measurement time is the
    first readable timestamp column, and the reference time is DM ``RFSTDTC``.
    """

    day_terms = [
        f"try_cast(nullif({alias}.{q(column)}, '') AS INTEGER)"
        for column in day_columns(domain, columns)
    ]
    timestamp_terms = [
        f"try_cast(nullif({alias}.{q(column)}, '') AS TIMESTAMP)"
        for column in (f"{domain}DTC", f"{domain}STDTC", f"{domain}ENDTC", "DTC")
        if column in columns
    ]
    reference = (
        f"try_cast(nullif(dm.{q('RFSTDTC')}, '') AS TIMESTAMP)"
        if "RFSTDTC" in dm_columns
        else "NULL"
    )
    return (
        f"coalesce({', '.join(day_terms)})" if day_terms else "NULL",
        f"coalesce({', '.join(timestamp_terms)})" if timestamp_terms else "NULL",
        reference,
    )


def _baseline_body_weight_sql(bw_columns: tuple[str, ...], dm_columns: tuple[str, ...]) -> str:
    """Derive one strict pre-study baseline body weight per subject.

    This is the SQL form of the pre-study endpoint rule in ``cohort.py`` for the
    catalogue BW_BW endpoint: ``endpoint_matches_row`` selects the row, and
    ``parse_result`` gives its value, unit and exclusion flag. Text is
    normalized for ASCII whitespace and U+00A0 only, not for full NFKC.
    """

    spec = canonical_endpoint("BW_BW").spec
    if spec.specimens or spec.methods or spec.sexes:
        raise ValueError("the derived baseline body weight supports test and unit filters only")

    def column(name: str) -> str:
        return f"cast(rows.{q(name)} AS VARCHAR)" if name in bw_columns else "NULL"

    space = r"[\s\x{00A0}]+"

    def stripped(expr: str) -> str:
        return f"regexp_replace({expr}, '^{space}|{space}$', '', 'g')"

    def normalized(expr: str) -> str:
        collapsed = f"regexp_replace({expr}, '{space}', ' ', 'g')"
        return f"upper({stripped(collapsed)})"

    def literals(values: Iterable[str]) -> str:
        return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(set(values)))

    def first_present(names: tuple[str, ...]) -> str:
        # endpoint_rows reads the first column that is not NULL or exactly ''.
        terms = [f"nullif({column(name)}, '')" for name in names if name in bw_columns]
        value = f"coalesce({', '.join(terms)})" if terms else "NULL"
        return f"coalesce(nullif({normalized(value)}, ''), 'UNKNOWN')"

    def number(name: str) -> str:
        value = f"try_cast(nullif({column(name)}, '') AS DOUBLE)"
        return f"CASE WHEN isfinite({value}) THEN {value} END"

    def unit(name: str) -> str:
        value = stripped(column(name))
        return (
            f"CASE WHEN nullif({value}, '') IS NOT NULL "
            f"AND {normalized(column(name))} NOT IN ({literals(MISSING_UNIT_LABELS)}) "
            f"THEN {value} END"
        )

    flags = [
        f"coalesce({normalized(column(name))} IN ('1', 'TRUE', 'Y', 'YES'), false)"
        for name in ("BWEXCLFL", "EXCLFL")
        if name in bw_columns
    ]
    excluded = " OR ".join(flags) if flags else "false"
    test_code = first_present(("BWTESTCD", "BWTEST", "TESTCD", "TEST"))
    test_name = first_present(("BWTEST", "TEST"))
    stresn, orres, stresc = number("BWSTRESN"), number("BWORRES"), number("BWSTRESC")
    stres_unit, orres_unit = unit("BWSTRESU"), unit("BWORRESU")
    day, measured, reference = pre_study_timing_sql("BW", bw_columns, dm_columns, "rows")
    name_filter = (
        f"AND test_name IN ({literals(spec.test_names)})" if spec.test_names else ""
    )
    unit_filter = (
        f"AND upper(source_unit) IN ({literals(u.upper() for u in spec.source_units)})"
        if spec.source_units
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW subject_baseline_body_weight AS
    WITH parsed AS (
        SELECT
            rows."STUDYID",
            rows."USUBJID",
            {day} AS study_day,
            {measured} AS measurement_time,
            {reference} AS reference_time,
            exposure.first_exposure_day,
            {test_code} AS test_code,
            {test_name} AS test_name,
            {excluded} AS excluded,
            {stresn} AS stresn,
            {orres} AS orres,
            {stresc} AS stresc,
            {stres_unit} AS stres_unit,
            {orres_unit} AS orres_unit
        FROM bw AS rows
        INNER JOIN dm USING ("STUDYID", "USUBJID")
        LEFT JOIN subject_exposure_summary AS exposure USING ("STUDYID", "USUBJID")
    ),
    valued AS (
        SELECT
            *,
            coalesce(stresn, orres, stresc) AS baseline_body_weight_g,
            -- parse_result pairs each value with its own unit column and
            -- borrows the other unit only when the other value is equal.
            CASE
                WHEN stresn IS NOT NULL
                    THEN coalesce(stres_unit, CASE WHEN orres = stresn THEN orres_unit END)
                WHEN orres IS NOT NULL
                    THEN coalesce(orres_unit, CASE WHEN stresc = orres THEN stres_unit END)
                ELSE stres_unit
            END AS source_unit
        FROM parsed
    ),
    candidates AS (
        SELECT
            "STUDYID",
            "USUBJID",
            baseline_body_weight_g,
            rank() OVER (
                PARTITION BY "STUDYID", "USUBJID"
                ORDER BY
                    study_day IS NOT NULL DESC,
                    study_day DESC,
                    measurement_time DESC NULLS LAST
            ) AS candidate_rank
        FROM valued
        WHERE NOT excluded
          AND baseline_body_weight_g IS NOT NULL
          AND test_code IN ({literals(spec.test_codes)})
          {name_filter}
          {unit_filter}
          AND (
              study_day < coalesce(first_exposure_day, 1)
              OR (study_day IS NULL AND measurement_time < reference_time)
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
    "PRE_STUDY_ENDPOINT_TABLE",
    "PYTHON_DERIVED_TABLES",
    "SUBJECT_CONTEXT_TABLE",
    "create_derived_views",
    "domain_view_name",
    "ensure_extracted",
    "materialize_archive_tables",
    "materialize_derived_tables",
    "python_derived_tables_current",
    "normalize_domain",
    "open_archive",
    "open_source_tables",
    "open_directory",
    "q",
    "quote_ident",
    "quote_literal",
    "register_csv_views",
    "registered_frame",
    "table_columns",
]
