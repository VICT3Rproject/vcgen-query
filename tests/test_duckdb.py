"""DuckDB layer tests: derived views, content-aware extraction, helpers."""

from __future__ import annotations

from zipfile import ZipFile

import pytest

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
        SELECT study_length_days, study_length_values, dose_duration_day_values,
               supplier_values, study_type_values, test_facility_values,
               glp_status_values
        FROM study_design_summary
        WHERE STUDYID = 'S1'
        """
    ).fetchone()
    assert row == (
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
        SELECT study_length_days, study_length_values, dose_duration_day_values
        FROM study_design_summary
        WHERE STUDYID = 'S1'
        """
    ).fetchone()

    assert row == (None, "P14D | P28D", [14.0, 28.0])


def test_register_csv_views_creates_view_from_csv(tmp_path):
    (tmp_path / "dm.csv").write_text("STUDYID,USUBJID\nS1,S1-1\n")
    con = db.connect(":memory:")
    db.register_csv_views(con, tmp_path, domains=["DM"])
    assert "STUDYID" in db.table_columns(con, "dm")
    assert con.execute("SELECT count(*) FROM dm WHERE \"STUDYID\" = 'S1'").fetchone()[0] == 1
