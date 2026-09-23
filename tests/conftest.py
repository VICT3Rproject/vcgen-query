"""Shared fixtures and synthetic SEND builders for the vcgen-query test suite.

Tests build small, deterministic SEND-like datasets in an in-memory DuckDB
connection and run the derived views, mirroring the host project's fixture
pattern but self-contained inside this package.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from vcgen_query.duckdb import connect, create_derived_views


@pytest.fixture
def make_connection():
    """Return a builder that constructs a synthetic SEND in-memory connection."""

    return _make_connection


def _make_connection(
    subjects: Iterable[dict[str, Any]],
    *,
    observations: Iterable[dict[str, Any]] | None = None,
    body_weight: Iterable[dict[str, Any]] | None = None,
    lab: Iterable[dict[str, Any]] | None = None,
    vital_signs: Iterable[dict[str, Any]] | None = None,
    exposure_intervals: Iterable[dict[str, Any]] | None = None,
    dispositions: Iterable[dict[str, Any]] | None = None,
    study_metadata: Mapping[str, Mapping[str, str | Iterable[str] | None]] | None = None,
) -> Any:
    subjects = list(subjects)
    con = connect(":memory:")
    con.execute(
        """
        CREATE TABLE dm (
            STUDYID VARCHAR, DOMAIN VARCHAR, USUBJID VARCHAR, VICT3RID VARCHAR,
            SPECIES VARCHAR, STRAIN VARCHAR, SBSTRAIN VARCHAR, SEX VARCHAR,
            AGE VARCHAR, AGETXT VARCHAR, AGEU VARCHAR, ARMCD VARCHAR, ARM VARCHAR,
            SETCD VARCHAR, RPATHCD VARCHAR, RFSTDTC VARCHAR
        )
        """
    )
    studies = []
    for s in subjects:
        con.execute(
            "INSERT INTO dm VALUES (" + ",".join(["?"] * 16) + ")",
            (
                s["study_id"],
                "DM",
                s.get("usubjid", f"{s['study_id']}-{s['subject_id']}"),
                "VID",
                s.get("species", "RAT"),
                s.get("strain", "WISTAR HAN"),
                "",
                s.get("sex", "M"),
                str(s.get("age", "")),
                "",
                s.get("age_unit", "WEEK"),
                "CTRL",
                "CONTROL",
                "1",
                "",
                s.get("rfstdtc", "2020-01-01"),
            ),
        )
        if s["study_id"] not in studies:
            studies.append(s["study_id"])

    con.execute(
        "CREATE TABLE ts (STUDYID VARCHAR, TSSEQ VARCHAR, TSPARMCD VARCHAR, TSPARM VARCHAR, TSVAL VARCHAR)"
    )
    for study in studies:
        length = next((s.get("study_length", 28) for s in subjects if s["study_id"] == study), 28)
        dose_duration = next(
            (
                s.get("dose_duration", length)
                for s in subjects
                if s["study_id"] == study
            ),
            length,
        )
        next_sequence = 1
        if dose_duration is not None:
            con.execute(
                "INSERT INTO ts VALUES (?,?,?,?,?)",
                (study, str(next_sequence), "DOSDUR", "Dosing Duration", str(dose_duration)),
            )
            next_sequence += 1
        overall_length = next(
            (
                s.get("overall_study_length", length)
                for s in subjects
                if s["study_id"] == study
            ),
            length,
        )
        con.execute(
            "INSERT INTO ts VALUES (?,?,?,?,?)",
            (study, str(next_sequence), "SLENGTH", "Study Length", str(overall_length)),
        )
        next_sequence += 1
        for code, raw_values in (study_metadata or {}).get(study, {}).items():
            if raw_values is None:
                continue
            values = (raw_values,) if isinstance(raw_values, str) else raw_values
            for sequence, value in enumerate(values, start=next_sequence):
                con.execute(
                    "INSERT INTO ts VALUES (?,?,?,?,?)",
                    (study, str(sequence), code, code, value),
                )
    con.execute(
        """
        CREATE TABLE ex (
            STUDYID VARCHAR, USUBJID VARCHAR, EXSEQ VARCHAR, EXTRT VARCHAR,
            EXDOSE VARCHAR, EXDOSTXT VARCHAR, EXDOSU VARCHAR, EXDOSFRM VARCHAR,
            EXDOSFRQ VARCHAR, EXROUTE VARCHAR, EXMETHOD VARCHAR, EXTRTV VARCHAR,
            EXSTDY VARCHAR, EXENDY VARCHAR, EXSTDTC VARCHAR, EXENDTC VARCHAR
        )
        """
    )
    interval_rows = (
        list(exposure_intervals)
        if exposure_intervals is not None
        else [
            {
                "study_id": s["study_id"],
                "subject_id": s["subject_id"],
                "usubjid": s.get("usubjid", f"{s['study_id']}-{s['subject_id']}"),
                "exseq": "1",
                "extrt": s.get("extrt", "0.5% METHOCEL"),
                "exdose": s.get("exdose", "0"),
                "exdosfrq": s.get("exdosfrq", "QD"),
                "exroute": s.get("exroute", "ORAL GAVAGE"),
                "extrtv": s.get("extrtv", "0.5% METHOCEL"),
                "exstdy": s.get("exstdy", 1),
                "exendy": s.get("exendy", s.get("study_length", 28)),
                "exstdtc": s.get("exstdtc", ""),
                "exendtc": s.get("exendtc", ""),
            }
            for s in subjects
        ]
    )
    for interval in interval_rows:
        con.execute(
            "INSERT INTO ex VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                interval["study_id"],
                interval.get(
                    "usubjid",
                    f"{interval['study_id']}-{interval.get('subject_id', '')}",
                ),
                str(interval.get("exseq", "1")),
                interval.get("extrt", "0.5% METHOCEL"),
                interval.get("exdose", "0"),
                "",
                "MG/KG",
                "SOLUTION",
                interval.get("exdosfrq", "QD"),
                interval.get("exroute", "ORAL GAVAGE"),
                "GAVAGE",
                interval.get("extrtv", "0.5% METHOCEL"),
                str(interval.get("exstdy", "")),
                str(interval.get("exendy", "")),
                str(interval.get("exstdtc", "")),
                str(interval.get("exendtc", "")),
            ),
        )

    con.execute(
        "CREATE TABLE ta (STUDYID VARCHAR, ARMCD VARCHAR, ARM VARCHAR, TAETORD VARCHAR, ETCD VARCHAR, ELEMENT VARCHAR, TABRANCH VARCHAR)"
    )
    for study in studies:
        con.execute(
            "INSERT INTO ta VALUES (?,?,?,?,?,?,?)",
            (study, "CTRL", "CONTROL", "1", "CTRL", "CONTROL", ""),
        )

    con.execute(
        "CREATE TABLE tx (STUDYID VARCHAR, SETCD VARCHAR, TXSEQ VARCHAR, SET VARCHAR, TXPARMCD VARCHAR, TXPARM VARCHAR, TXVAL VARCHAR)"
    )
    con.execute(
        "CREATE TABLE se (STUDYID VARCHAR, USUBJID VARCHAR, SESEQ VARCHAR, ETCD VARCHAR, ELEMENT VARCHAR, SESTDTC VARCHAR, SEENDTC VARCHAR)"
    )
    for s in subjects:
        con.execute(
            "INSERT INTO se VALUES (?,?,?,?,?,?,?)",
            (
                s["study_id"],
                s.get("usubjid", f"{s['study_id']}-{s['subject_id']}"),
                "1",
                "CTRL",
                "CONTROL",
                "2020-01-01",
                "2020-02-15",
            ),
        )

    _create_bw(con, subjects, body_weight)
    _create_lb(con, subjects, lab)
    _create_vs(con, vital_signs)
    if dispositions is not None:
        _create_ds(con, dispositions)
    create_derived_views(con)
    return con


def _create_ds(con, dispositions):
    con.execute(
        """
        CREATE TABLE ds (
            STUDYID VARCHAR, DOMAIN VARCHAR, USUBJID VARCHAR, DSSEQ VARCHAR,
            DSTERM VARCHAR, DSDECOD VARCHAR, DSUSCHFL VARCHAR, DSSTDTC VARCHAR,
            DSSTDY VARCHAR
        )
        """
    )
    for row in dispositions:
        con.execute(
            "INSERT INTO ds VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["study_id"],
                "DS",
                row.get("usubjid", f"{row['study_id']}-{row['subject_id']}"),
                str(row.get("sequence", "1")),
                row.get("term", row.get("decode", "")),
                row["decode"],
                "",
                row.get("datetime", ""),
                str(row["day"]) if row.get("day") is not None else None,
            ),
        )


def _create_bw(con, subjects, body_weight):
    con.execute(
        """
        CREATE TABLE bw (
            STUDYID VARCHAR, USUBJID VARCHAR, BWTESTCD VARCHAR, BWTEST VARCHAR,
            BWDY VARCHAR, BWDTC VARCHAR, BWSTRESN VARCHAR, BWSTRESU VARCHAR,
            BWORRES VARCHAR, BWORRESU VARCHAR, BWBLFL VARCHAR
        )
        """
    )
    rows = body_weight if body_weight is not None else _default_bw(subjects)
    for r in rows:
        con.execute(
            "INSERT INTO bw VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["study_id"],
                r.get("usubjid", f"{r['study_id']}-{r['subject_id']}"),
                r.get("test_code", "BW"),
                r.get("test_name", "BODY WEIGHT"),
                str(r["day"]) if r.get("day") is not None else None,
                r.get("datetime", "2019-12-31T08:00:00"),
                str(r["value"]),
                r.get("unit", "G"),
                str(r["value"]),
                r.get("unit", "G"),
                r.get("baseline_flag", ""),
            ),
        )


def _default_bw(subjects):
    rows = []
    for s in subjects:
        for day, value in (("1", "250"), ("7", "270"), ("14", "290"), ("28", "320")):
            rows.append(
                {
                    "study_id": s["study_id"],
                    "subject_id": s["subject_id"],
                    "day": day,
                    "value": value,
                }
            )
    return rows


def _default_lb(subjects):
    rows = []
    for s in subjects:
        for day, value in (("1", "0.45"), ("28", "0.48")):
            rows.append(
                {
                    "study_id": s["study_id"],
                    "subject_id": s["subject_id"],
                    "day": day,
                    "value": value,
                }
            )
    return rows


def _create_lb(con, subjects, lab):
    con.execute(
        """
        CREATE TABLE lb (
            STUDYID VARCHAR, USUBJID VARCHAR, LBTESTCD VARCHAR, LBTEST VARCHAR,
            LBDY VARCHAR, LBDTC VARCHAR, LBSEQ VARCHAR, LBBLFL VARCHAR,
            LBEXCLFL VARCHAR, LBSTRESN VARCHAR, LBSTRESU VARCHAR, LBORRES VARCHAR,
            LBORRESU VARCHAR, LBSPEC VARCHAR
        )
        """
    )
    rows = lab if lab is not None else _default_lb(subjects)
    for r in rows:
        con.execute(
            "INSERT INTO lb VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["study_id"],
                r.get("usubjid", f"{r['study_id']}-{r['subject_id']}"),
                r.get("test_code", "HCT"),
                r.get("test_name", "HEMATOCRIT"),
                str(r["day"]) if r.get("day") is not None else None,
                r.get("datetime", "2019-12-31T08:00:00"),
                str(r.get("sequence", "")),
                r.get("baseline_flag", ""),
                r.get("excluded", ""),
                str(r["value"]),
                r.get("unit", "L/L"),
                str(r["value"]),
                r.get("unit", "L/L"),
                r.get("specimen", "BLOOD"),
            ),
        )


def _create_vs(con, vital_signs):
    con.execute(
        """
        CREATE TABLE vs (
            STUDYID VARCHAR, USUBJID VARCHAR, VSTESTCD VARCHAR, VSTEST VARCHAR,
            VSDY VARCHAR, VSDTC VARCHAR, VSSEQ VARCHAR, VSEXCLFL VARCHAR,
            VSSTRESN VARCHAR, VSSTRESU VARCHAR, VSORRES VARCHAR, VSORRESU VARCHAR
        )
        """
    )
    for row in vital_signs or ():
        con.execute(
            "INSERT INTO vs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["study_id"],
                row.get("usubjid", f"{row['study_id']}-{row['subject_id']}"),
                row.get("test_code", "TEMP"),
                row.get("test_name", "BODY TEMPERATURE"),
                str(row["day"]) if row.get("day") is not None else None,
                row.get("datetime", "2019-12-31T08:00:00"),
                str(row.get("sequence", "")),
                row.get("excluded", ""),
                str(row["value"]),
                row.get("unit", "C"),
                str(row["value"]),
                row.get("unit", "C"),
            ),
        )


@pytest.fixture
def basic_subjects():
    """A small multi-study rat cohort with one mouse and varied years."""

    return [
        {"study_id": "S1", "subject_id": "0001", "sex": "M", "rfstdtc": "2020-01-15"},
        {"study_id": "S1", "subject_id": "0002", "sex": "F", "rfstdtc": "2020-01-15"},
        {"study_id": "S2", "subject_id": "0001", "sex": "M", "rfstdtc": "2021-03-10"},
        {"study_id": "S3", "subject_id": "0001", "sex": "M", "rfstdtc": "2019-05-01"},
        {
            "study_id": "S4",
            "subject_id": "0001",
            "sex": "M",
            "rfstdtc": "2020-06-01",
            "species": "MOUSE",
        },
    ]
