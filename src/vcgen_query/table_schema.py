"""Stable public pandas table schemas for query results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class ResultColumn:
    """One public result column and its pandas representation."""

    name: str
    pandas_dtype: str
    nullable: bool
    meaning: str
    unit: str | None = None
    nested_value: str | None = None


@dataclass(frozen=True)
class ResultTable:
    """The grain, keys, and ordered columns of one public result table."""

    name: Literal["subjects", "observations"]
    grain: str
    keys: tuple[str, ...]
    columns: tuple[ResultColumn, ...]


def _column(
    name: str,
    pandas_dtype: str,
    nullable: bool,
    meaning: str,
    *,
    unit: str | None = None,
    nested_value: str | None = None,
) -> ResultColumn:
    return ResultColumn(name, pandas_dtype, nullable, meaning, unit, nested_value)


SUBJECTS_TABLE = ResultTable(
    name="subjects",
    grain="one row per selected SEND subject",
    keys=("study_id", "subject_id"),
    columns=(
        _column("sample_id", "string", False, "Stable package sample identifier."),
        _column("study_id", "string", False, "SEND STUDYID."),
        _column("subject_id", "string", False, "SEND USUBJID."),
        _column("species", "string", False, "Normalized SEND species."),
        _column("strain", "string", False, "Normalized SEND strain."),
        _column("sex", "string", False, "Normalized SEND sex."),
        _column("study_year", "Int64", True, "Calendar year of the study start."),
        _column("study_length_days", "Int64", True, "Study duration.", unit="day"),
        _column(
            "planned_dose_duration_days",
            "Int64",
            True,
            "Planned treatment duration resolved from source evidence.",
            unit="day",
        ),
        _column(
            "observed_exposure_span_days",
            "Int64",
            True,
            "Observed span between first and last exposure.",
            unit="day",
        ),
        _column(
            "dose_duration_evidence_status",
            "string",
            False,
            "Relationship between planned duration and observed exposure evidence.",
        ),
        _column("suppliers", "object", False, "Distinct normalized suppliers.", nested_value="list[str]"),
        _column("study_types", "object", False, "Distinct normalized study types.", nested_value="list[str]"),
        _column("routes", "object", False, "Distinct normalized administration routes.", nested_value="list[str]"),
        _column("dose_frequencies", "object", False, "Distinct normalized dose frequencies.", nested_value="list[str]"),
        _column("treatment_vehicles", "object", False, "Distinct normalized treatment vehicles.", nested_value="list[str]"),
        _column("test_facilities", "object", False, "Distinct normalized test facilities.", nested_value="list[str]"),
        _column("glp_statuses", "object", False, "Distinct normalized GLP statuses.", nested_value="list[str]"),
        _column("baseline_body_weight_g", "Float64", True, "Resolved pre-study body weight.", unit="g"),
        _column("age_days", "Float64", True, "Age normalized to days.", unit="day"),
        _column("first_exposure_day", "Int64", True, "First exposure day relative to study start.", unit="day"),
    ),
)


_OPTIONAL_OBSERVATION_TEXT = {
    "sex",
    "pool_id",
    "test_code",
    "test_name",
    "specimen",
    "unit",
    "method",
    "laterality",
    "portion_totality",
    "fasting_status",
    "laboratory_name",
    "raw_value",
    "value_column",
    "unit_column",
    "exclusion_reason",
    "failure_reason",
}


def _observation_text(name: str, meaning: str) -> ResultColumn:
    # Nullable text intentionally stays object-backed. This preserves Python
    # ``None`` at the pandas/application boundary instead of exposing pd.NA.
    nullable = name in _OPTIONAL_OBSERVATION_TEXT
    return _column(name, "object" if nullable else "string", nullable, meaning)


OBSERVATIONS_TABLE = ResultTable(
    name="observations",
    grain="one row per matched source measurement; duplicate cells remain separate rows",
    keys=("study_id", "subject_id", "measurement_identity", "source_order"),
    columns=(
        _observation_text("study_id", "SEND STUDYID."),
        _observation_text("subject_id", "SEND USUBJID."),
        _observation_text("sample_id", "Stable package sample identifier."),
        _observation_text("sex", "Normalized SEND sex when known."),
        _observation_text("pool_id", "Source pool identifier when present."),
        _observation_text("endpoint_name", "Canonical requested endpoint name."),
        _observation_text("domain", "SEND source domain."),
        _observation_text("test_code", "Source test code."),
        _observation_text("test_name", "Source test name."),
        _column("study_day", "Int64", True, "Measurement day relative to study start.", unit="day"),
        _observation_text("measurement_identity", "Stable identity for the measurement channel."),
        _observation_text("endpoint_key", "Canonical source endpoint key."),
        _observation_text("specimen", "Normalized specimen."),
        _observation_text("unit", "Original source unit; target units are declared in endpoint_facts."),
        _observation_text("method", "Normalized measurement method."),
        _observation_text("laterality", "Normalized laterality."),
        _observation_text("portion_totality", "Normalized portion or totality qualifier."),
        _observation_text("fasting_status", "Normalized fasting status."),
        _observation_text("laboratory_name", "Normalized laboratory name."),
        _observation_text("result_state", "Parsed factual result state."),
        _column("numeric_value", "Float64", True, "Parsed finite numeric source value; not unit-converted."),
        _observation_text("raw_value", "Original source result text."),
        _observation_text("value_column", "SEND column from which the result was read."),
        _observation_text("unit_column", "SEND column from which the unit was read."),
        _column("excluded", "boolean", False, "Whether SEND marks the source result as excluded."),
        _observation_text("exclusion_reason", "Reason the result was excluded."),
        _observation_text("failure_reason", "Reason the source result is a failure."),
        _column("source_order", "object", False, "Deterministic source-row ordering key.", nested_value="tuple[Any, ...]"),
        _column("explicit_baseline", "boolean", False, "Whether SEND explicitly marks the row as baseline."),
    ),
)


def query_result_schema(
    table: Literal["subjects", "observations"] | None = None,
) -> ResultTable | tuple[ResultTable, ResultTable]:
    """Return the stable, inspectable schema for public query-result tables."""

    if table == "subjects":
        return SUBJECTS_TABLE
    if table == "observations":
        return OBSERVATIONS_TABLE
    if table is not None:
        raise ValueError(f"unsupported query result table: {table!r}")
    return (SUBJECTS_TABLE, OBSERVATIONS_TABLE)


def normalize_result_table(frame: pd.DataFrame, schema: ResultTable) -> pd.DataFrame:
    """Return a copy with the declared column order and stable pandas dtypes."""

    expected = [column.name for column in schema.columns]
    if frame.columns.tolist() != expected:
        raise ValueError(f"{schema.name} columns do not match the public table schema")
    normalized = frame.copy()
    for column in schema.columns:
        normalized[column.name] = normalized[column.name].astype(column.pandas_dtype)
    return normalized


__all__ = [
    "OBSERVATIONS_TABLE",
    "SUBJECTS_TABLE",
    "ResultColumn",
    "ResultTable",
    "normalize_result_table",
    "query_result_schema",
]
