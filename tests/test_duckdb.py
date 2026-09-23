"""DuckDB layer tests: derived views, content-aware extraction, helpers."""

from __future__ import annotations

from zipfile import ZipFile

import pytest

from vcgen_query import SourceDataError
from vcgen_query import duckdb as db


def test_table_columns_returns_empty_for_missing_view():
    con = db.connect(":memory:")
    assert db.table_columns(con, "does_not_exist") == ()


def test_normalize_and_quote_helpers():
    assert db.normalize_domain("bw") == "BW"
    assert db.domain_view_name("BW") == "bw"
    assert db.quote_ident('a"b') == '"a""b"'
    assert db.quote_literal("o'reilly") == "'o''reilly'"
    with pytest.raises(ValueError):
        db.normalize_domain("bad-domain!")


def test_archive_sha256_is_stable(tmp_path):
    archive = tmp_path / "a.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("dm.csv", "STUDYID\nS1\n")
    h1 = db.archive_sha256(archive)
    h2 = db.archive_sha256(archive)
    assert h1 == h2 and len(h1) == 64


def test_ensure_extracted_re_extracts_when_archive_changes(tmp_path):
    archive = tmp_path / "a.zip"
    cache = tmp_path / "cache"
    with ZipFile(archive, "w") as zf:
        zf.writestr("dm.csv", "STUDYID\nS1\n")
    db.ensure_extracted(archive, cache, domains=["DM"])
    extracted = (cache / "dm.csv").read_text()
    assert extracted == "STUDYID\nS1\n"
    marker = (cache / ".archive_sha256").read_text().strip()
    assert marker == db.archive_sha256(archive)

    # Same archive, different cache target -> reuses marker logic, no error.
    db.ensure_extracted(archive, cache, domains=["DM"])
    assert (cache / "dm.csv").read_text() == "STUDYID\nS1\n"

    # Archive content changes -> marker mismatch forces re-extraction.
    with ZipFile(archive, "w") as zf:
        zf.writestr("dm.csv", "STUDYID\nS1\nS2\n")
    db.ensure_extracted(archive, cache, domains=["DM"])
    assert (cache / "dm.csv").read_text() == "STUDYID\nS1\nS2\n"
    assert (cache / ".archive_sha256").read_text().strip() == db.archive_sha256(archive)


def test_ensure_extracted_repairs_corrupted_member(tmp_path):
    archive = tmp_path / "a.zip"
    cache = tmp_path / "cache"
    with ZipFile(archive, "w") as zf:
        zf.writestr("dm.csv", "STUDYID\nS1\nS2\n")
    db.ensure_extracted(archive, cache, domains=["DM"])
    # Corrupt the cached file (wrong size) without changing the archive hash.
    (cache / "dm.csv").write_text("truncated")
    db.ensure_extracted(archive, cache, domains=["DM"])
    assert (cache / "dm.csv").read_text() == "STUDYID\nS1\nS2\n"


def test_ensure_extracted_repairs_same_size_corruption(tmp_path):
    archive = tmp_path / "a.zip"
    cache = tmp_path / "cache"
    original = "STUDYID\nS1\nS2\n"
    with ZipFile(archive, "w") as zf:
        zf.writestr("dm.csv", original)
    db.ensure_extracted(archive, cache, domains=["DM"])
    # Same-size corruption: size check passes, CRC-32 check must catch it.
    (cache / "dm.csv").write_text("X" * len(original))
    db.ensure_extracted(archive, cache, domains=["DM"])
    assert (cache / "dm.csv").read_text() == original


def test_register_csv_views_and_derived_views(tmp_path, make_connection, basic_subjects):
    # Smoke: the conftest builder registers derived views for canonical cohorts.
    con = make_connection(basic_subjects)
    assert "route_values" in db.table_columns(con, "subject_exposure_summary")
    rows = con.execute("SELECT count(*) FROM subject_demographics").fetchone()[0]
    assert rows == len(basic_subjects)


@pytest.mark.parametrize(
    ("raw_duration", "expected_days"),
    [
        ("P28D", 28),
        ("P4W", 28),
        ("P28W", 196),
        ("28 days", 28),
        ("4 weeks", 28),
        ("P1M", None),
        ("4 months", None),
        ("invalid", None),
    ],
)
def test_study_design_summary_normalizes_duration_units(
    make_connection,
    raw_duration,
    expected_days,
):
    con = make_connection([{"study_id": "S1", "subject_id": "0001", "study_length": raw_duration}])

    actual = con.execute(
        "SELECT study_length_days FROM study_design_summary WHERE STUDYID = 'S1'"
    ).fetchone()[0]

    if expected_days is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected_days)


def test_study_design_summary_aggregates_deterministic_metadata_lists(make_connection):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "0001", "study_length": 28}],
        study_metadata={
            "S1": {
                "SPLRNAM": ["B SUPPLIER", "A SUPPLIER", "A SUPPLIER", ""],
                "SSTYP": ["Z TYPE", "A TYPE"],
                "TSTFNAM": ["FACILITY B", "FACILITY A"],
                "GLPFL": ["TRUE", "FALSE", "TRUE"],
            }
        },
    )
    row = con.execute(
        """
        SELECT study_length_days, study_length_values,
               study_planned_dose_duration_days,
               declared_dose_duration_values, planned_dose_duration_day_values,
               supplier_values, study_type_values, test_facility_values,
               glp_status_values
        FROM study_design_summary
        WHERE STUDYID = 'S1'
        """
    ).fetchone()
    assert row == (
        28,
        "28",
        28,
        "28",
        [28.0],
        ["A SUPPLIER", "B SUPPLIER"],
        ["A TYPE", "Z TYPE"],
        ["FACILITY A", "FACILITY B"],
        ["FALSE", "TRUE"],
    )


def test_study_design_summary_preserves_ambiguous_duration_values(make_connection):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "0001", "study_length": "P14D"}],
        study_metadata={"S1": {"DOSDUR": ["P28D"]}},
    )

    row = con.execute(
        """
        SELECT study_length_days, study_length_values,
               study_planned_dose_duration_days,
               declared_dose_duration_values, planned_dose_duration_day_values
        FROM study_design_summary
        WHERE STUDYID = 'S1'
        """
    ).fetchone()

    assert row == (14, "P14D", None, "P14D | P28D", [14.0, 28.0])


def test_subject_exposure_resolves_ambiguous_planned_duration(make_connection):
    con = make_connection(
        [
            {
                "study_id": "S1",
                "subject_id": "0001",
                "study_length": "P28D",
                "overall_study_length": "P57D",
                "exstdy": 1,
                "exendy": 28,
            }
        ],
        study_metadata={"S1": {"DOSDUR": ["P1D"]}},
    )

    row = con.execute(
        """
        SELECT observed_exposure_span_days, planned_dose_duration_days,
               dose_duration_evidence_status
        FROM subject_exposure_summary
        WHERE STUDYID = 'S1' AND USUBJID = 'S1-0001'
        """
    ).fetchone()

    assert row == (28, 28, "corroborated")


@pytest.mark.parametrize(
    ("intervals", "expected_span", "expected_interval_status"),
    [
        ([(1, 14), (15, 28)], 28, "complete_contiguous"),
        ([(1, 14), (10, 28)], 28, "complete_contiguous"),
        ([(1, 14), (16, 28)], None, "gapped"),
        ([(1, 28)], 28, "complete_contiguous"),
        ([(1, 1), (8, 8), (15, 15), (22, 22)], None, "gapped"),
    ],
)
def test_subject_exposure_merges_only_continuous_interval_coverage(
    make_connection,
    intervals,
    expected_span,
    expected_interval_status,
):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": 28}],
        exposure_intervals=[
            {
                "study_id": "S1",
                "subject_id": "1",
                "exseq": index,
                "exstdy": start,
                "exendy": end,
            }
            for index, (start, end) in enumerate(intervals, start=1)
        ],
    )

    row = con.execute(
        """
        SELECT observed_exposure_span_days, exposure_interval_status
        FROM subject_exposure_summary
        """
    ).fetchone()

    assert row == (expected_span, expected_interval_status)


@pytest.mark.parametrize(
    ("end_day", "expected_status"),
    [(28, "corroborated"), (14, "shorter_than_planned")],
)
def test_numeric_planned_duration_survives_unknown_declaration(
    make_connection,
    end_day,
    expected_status,
):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": 28, "exendy": end_day}],
        study_metadata={"S1": {"DOSDUR": ["UNKNOWN"]}},
    )

    row = con.execute(
        """
        SELECT planned_dose_duration_days, observed_exposure_span_days,
               dose_duration_evidence_status, dose_duration_declaration_incomplete
        FROM subject_exposure_summary
        """
    ).fetchone()

    assert row == (28, end_day, expected_status, True)


def test_multiple_planned_durations_require_complete_matching_exposure(make_connection):
    con = make_connection(
        [
            {"study_id": "S1", "subject_id": "MATCH", "dose_duration": 28},
            {"study_id": "S1", "subject_id": "UNRESOLVED", "dose_duration": 28},
        ],
        study_metadata={"S1": {"DOSDUR": ["P56D"]}},
        exposure_intervals=[
            {
                "study_id": "S1",
                "subject_id": "MATCH",
                "exstdy": 1,
                "exendy": 28,
            },
            {
                "study_id": "S1",
                "subject_id": "UNRESOLVED",
                "exseq": 1,
                "exstdy": 1,
                "exendy": 14,
            },
            {
                "study_id": "S1",
                "subject_id": "UNRESOLVED",
                "exseq": 2,
                "exstdy": 16,
                "exendy": 28,
            },
        ],
    )

    rows = con.execute(
        """
        SELECT USUBJID, planned_dose_duration_days, observed_exposure_span_days,
               dose_duration_evidence_status
        FROM subject_exposure_summary
        ORDER BY USUBJID
        """
    ).fetchall()

    assert rows == [
        ("S1-MATCH", 28, 28, "corroborated"),
        ("S1-UNRESOLVED", None, None, "ambiguous"),
    ]


def test_unknown_only_does_not_infer_planned_duration_from_ex(make_connection):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": None, "exendy": 28}],
        study_metadata={"S1": {"DOSDUR": ["UNKNOWN"]}},
    )

    row = con.execute(
        """
        SELECT planned_dose_duration_days, observed_exposure_span_days,
               dose_duration_evidence_status
        FROM subject_exposure_summary
        """
    ).fetchone()

    assert row == (None, 28, "unresolved")


@pytest.mark.parametrize(
    ("start_day", "end_day", "expected_issue"),
    [(28, 1, "inverted_study_day_bounds"), (-1, 1, "crosses_missing_study_day_zero")],
)
def test_invalid_study_day_intervals_do_not_resolve_observed_span(
    make_connection,
    start_day,
    end_day,
    expected_issue,
):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": 28}],
        exposure_intervals=[
            {
                "study_id": "S1",
                "subject_id": "1",
                "exstdy": start_day,
                "exendy": end_day,
            }
        ],
    )

    interval = con.execute(
        "SELECT interval_source, interval_issue FROM subject_exposure_intervals"
    ).fetchone()
    summary = con.execute(
        "SELECT observed_exposure_span_days FROM subject_exposure_summary"
    ).fetchone()

    assert interval == (None, expected_issue)
    assert summary == (None,)


def test_date_intervals_are_preferred_and_can_cross_the_reference_date(make_connection):
    con = make_connection(
        [
            {
                "study_id": "DATES",
                "subject_id": "1",
                "dose_duration": 3,
                "rfstdtc": "2020-01-01",
            },
            {"study_id": "DAYS", "subject_id": "1", "dose_duration": 28},
        ],
        exposure_intervals=[
            {
                "study_id": "DATES",
                "subject_id": "1",
                "exstdy": 1,
                "exendy": 1,
                "exstdtc": "2019-12-31",
                "exendtc": "2020-01-02",
            },
            {
                "study_id": "DAYS",
                "subject_id": "1",
                "exstdy": 1,
                "exendy": 28,
            },
        ],
    )

    intervals = con.execute(
        """
        SELECT STUDYID, interval_source, interval_duration_days,
               crosses_reference_date
        FROM subject_exposure_intervals
        ORDER BY STUDYID
        """
    ).fetchall()
    summaries = con.execute(
        """
        SELECT STUDYID, observed_exposure_span_days
        FROM subject_exposure_summary
        ORDER BY STUDYID
        """
    ).fetchall()

    assert intervals == [
        ("DATES", "date", 3, True),
        ("DAYS", "study_day", 28, False),
    ]
    assert summaries == [("DATES", 3), ("DAYS", 28)]


def test_partial_date_bounds_fall_back_to_valid_study_days(make_connection):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": 28}],
        exposure_intervals=[
            {
                "study_id": "S1",
                "subject_id": "1",
                "exstdy": 1,
                "exendy": 28,
                "exstdtc": "2020-01-01",
            }
        ],
    )

    interval = con.execute(
        "SELECT interval_source, interval_duration_days, interval_issue "
        "FROM subject_exposure_intervals"
    ).fetchone()
    summary = con.execute(
        "SELECT observed_exposure_span_days FROM subject_exposure_summary"
    ).fetchone()

    assert interval == ("study_day", 28, None)
    assert summary == (28,)


def test_complete_invalid_dates_do_not_fall_back_to_study_days(make_connection):
    con = make_connection(
        [{"study_id": "S1", "subject_id": "1", "dose_duration": 28}],
        exposure_intervals=[
            {
                "study_id": "S1",
                "subject_id": "1",
                "exstdy": 1,
                "exendy": 28,
                "exstdtc": "2020-01-28",
                "exendtc": "2020-01-01",
            }
        ],
    )

    interval = con.execute(
        "SELECT interval_source, interval_issue FROM subject_exposure_intervals"
    ).fetchone()
    summary = con.execute(
        "SELECT observed_exposure_span_days FROM subject_exposure_summary"
    ).fetchone()

    assert interval == (None, "inverted_date_bounds")
    assert summary == (None,)


def test_register_csv_views_creates_view_from_csv(tmp_path):
    (tmp_path / "dm.csv").write_text("STUDYID,USUBJID\nS1,S1-1\n")
    con = db.connect(":memory:")
    db.register_csv_views(con, tmp_path, domains=["DM"])
    assert "STUDYID" in db.table_columns(con, "dm")
    assert con.execute("SELECT count(*) FROM dm WHERE \"STUDYID\" = 'S1'").fetchone()[0] == 1


def test_open_directory_adds_core_domains_to_active_domain_selection(tmp_path):
    _write_minimal_source(tmp_path)

    con = db.open_directory(tmp_path, domains=["LB"])

    assert db.table_columns(con, "dm")
    assert db.table_columns(con, "ts")
    assert db.table_columns(con, "ex")
    assert not db.table_columns(con, "om")


def test_open_directory_reports_identity_schema_error_before_derived_views(tmp_path):
    _write_minimal_source(tmp_path, dm_subject_column=False)

    with pytest.raises(SourceDataError, match="DM.*USUBJID"):
        db.open_directory(tmp_path)


def _write_minimal_source(tmp_path, *, dm_subject_column: bool = True) -> None:
    dm_columns = [
        "STUDYID",
        *(["USUBJID"] if dm_subject_column else []),
        "SPECIES",
        "STRAIN",
        "SEX",
        "AGE",
        "AGEU",
        "RFSTDTC",
    ]
    dm_values = [
        "S",
        *(["S-1"] if dm_subject_column else []),
        "RAT",
        "WISTAR HAN",
        "M",
        "8",
        "WEEK",
        "2020-01-01",
    ]
    (tmp_path / "DM.csv").write_text(
        f"{','.join(dm_columns)}\n{','.join(dm_values)}\n",
        encoding="utf-8",
    )
    (tmp_path / "TS.csv").write_text(
        "STUDYID,TSPARMCD,TSVAL\nS,DOSDUR,28\n",
        encoding="utf-8",
    )
    (tmp_path / "EX.csv").write_text(
        "STUDYID,USUBJID,EXSTDY,EXROUTE,EXDOSFRQ,EXTRT\n"
        "S,S-1,1,ORAL GAVAGE,QD,VEHICLE\n",
        encoding="utf-8",
    )
