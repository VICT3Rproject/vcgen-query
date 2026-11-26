"""Source identity validation and cardinality regression tests."""

from __future__ import annotations

import pytest

from vcgen_query import (
    SourceDataError,
    CohortQuery,
    execute_query,
    parse_subject_query,
    to_daily_frame,
)


def _field(domain: str, column: str, value_type: str = "string") -> dict[str, str]:
    return {
        "kind": "send_column",
        "domain": domain,
        "column": column,
        "value_type": value_type,
    }


def _rule(domain: str, column: str, value: object, value_type: str = "string") -> dict:
    return {
        "field": _field(domain, column, value_type),
        "operator": "eq",
        "value": value,
    }


def _query(
    predicate: dict | None = None,
    *,
    endpoints: list[dict[str, object]] | None = None,
):
    raw: dict[str, object] = {
        "contract": "vcgen.subject_query.v1",
        "query_id": "source-validation",
        "predicate": predicate or _rule("DM", "SEX", "M"),
    }
    requested = ["LB_HCT"] if endpoints is not None else ["BW"]
    return CohortQuery.compose(parse_subject_query(raw), requested)


@pytest.mark.parametrize(
    "predicate",
    [
        _rule("LB", "LBTESTCD", "HCT"),
        {
            "group": "and",
            "children": [
                _rule("LB", "LBTESTCD", "HCT"),
                _rule("LB", "LBSTRESN", 0.4, "number"),
            ],
        },
    ],
)
def test_subject_grain_predicate_requires_usubjid(make_connection, predicate) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("ALTER TABLE lb DROP COLUMN USUBJID")

    with pytest.raises(SourceDataError, match="LB.*USUBJID"):
        execute_query(con, _query(predicate))


def test_present_observation_domain_requires_usubjid(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("ALTER TABLE lb DROP COLUMN USUBJID")

    with pytest.raises(SourceDataError, match="LB.*USUBJID"):
        execute_query(
            con,
            _query(endpoints=[{"name": "HCT", "domain": "LB", "test_codes": ["HCT"]}]),
        )


def test_absent_observation_domain_keeps_warning(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("DROP TABLE lb")

    result = execute_query(
        con,
        _query(endpoints=[{"name": "HCT", "domain": "LB", "test_codes": ["HCT"]}]),
    )

    assert result.observations.empty
    assert any("LB: configured endpoint domain is not available" in item for item in result.warnings)


@pytest.mark.parametrize(
    ("domain", "statement"),
    [
        ("DM", "UPDATE dm SET USUBJID = ' S-1 '"),
        ("LB", "UPDATE lb SET USUBJID = ' S-1 '"),
    ],
)
def test_active_subject_domains_reject_padded_identity_values(
    make_connection, domain, statement
) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute(statement)
    query = _query(
        endpoints=[{"name": "HCT", "domain": "LB", "test_codes": ["HCT"]}]
        if domain == "LB"
        else None
    )

    with pytest.raises(SourceDataError, match=f"{domain}.*surrounding whitespace"):
        execute_query(con, query)


def test_active_subject_domain_rejects_blank_identity_values(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("UPDATE lb SET USUBJID = ''")

    with pytest.raises(SourceDataError, match="LB.*whitespace-only"):
        execute_query(
            con,
            _query(endpoints=[{"name": "HCT", "domain": "LB", "test_codes": ["HCT"]}]),
        )


@pytest.mark.parametrize(
    "duplicate_sql",
    [
        "INSERT INTO dm SELECT * FROM dm",
        "INSERT INTO dm SELECT * REPLACE ('F' AS SEX) FROM dm",
    ],
)
def test_duplicate_dm_subject_keys_reject_execution(make_connection, duplicate_sql) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute(duplicate_sql)

    with pytest.raises(SourceDataError, match="duplicate STUDYID/USUBJID"):
        execute_query(con, _query())


def test_declared_study_grain_ignores_unexpected_usubjid_column(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "1", "sex": "M", "study_length": 28},
            {"study_id": "S", "subject_id": "2", "sex": "F", "study_length": 28},
        ]
    )
    con.execute("ALTER TABLE ts ADD COLUMN USUBJID VARCHAR")
    con.execute("UPDATE ts SET USUBJID = 'not-a-DM-subject'")

    result = execute_query(con, _query(_rule("TS", "TSVAL", "28")))

    assert result.subjects["subject_id"].tolist() == ["S-1", "S-2"]


def test_pooldef_is_declared_subject_grain(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute("CREATE TABLE pooldef (STUDYID VARCHAR, POOLID VARCHAR)")
    con.execute("INSERT INTO pooldef VALUES ('S', 'P1')")

    with pytest.raises(SourceDataError, match="POOLDEF.*USUBJID"):
        execute_query(con, _query(_rule("POOLDEF", "POOLID", "P1")))


def test_duplicate_subject_context_rows_reject_execution(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])
    con.execute(
        """
        CREATE OR REPLACE VIEW subject_demographics AS
        SELECT STUDYID, USUBJID, SPECIES, STRAIN, SEX, NULL::DOUBLE AS age_days FROM dm
        UNION ALL
        SELECT STUDYID, USUBJID, SPECIES, STRAIN, SEX, NULL::DOUBLE AS age_days FROM dm
        """
    )

    with pytest.raises(SourceDataError, match="context assembly produced duplicate"):
        execute_query(con, _query())


# Failure mode: pruning context construction to selected subjects hides a
# duplicate or missing context for an unselected DM subject.
@pytest.mark.parametrize("duplicate", [True, False])
def test_unselected_subject_context_integrity_still_rejects_execution(make_connection, duplicate):
    con = make_connection([
        {"study_id": "S", "subject_id": "1", "sex": "M"},
        {"study_id": "S", "subject_id": "2", "sex": "F"},
    ])
    projection = "SELECT STUDYID, USUBJID, SPECIES, STRAIN, SEX, NULL::DOUBLE AS age_days FROM dm"
    changed = (projection + " UNION ALL " + projection + " WHERE USUBJID = 'S-2'") if duplicate else (projection + " WHERE USUBJID = 'S-1'")
    con.execute("CREATE OR REPLACE VIEW subject_demographics AS " + changed)
    with pytest.raises(SourceDataError, match="context assembly produced duplicate|context keys do not match"):
        execute_query(con, _query(_rule("DM", "USUBJID", "S-1")))


def test_repeated_observations_remain_available_for_daily_resolution(make_connection) -> None:
    row = {
        "study_id": "S",
        "subject_id": "1",
        "test_code": "HCT",
        "day": 1,
        "sequence": "1",
        "value": 0.45,
        "unit": "%",
        "specimen": "BLOOD",
    }
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "sex": "M"}],
        lab=[row, row],
    )
    result = execute_query(
        con,
        _query(endpoints=[{"name": "HCT", "domain": "LB", "test_codes": ["HCT"]}]),
    )

    assert len(result.observations) == 2
    daily = to_daily_frame(result)
    assert daily.receipt["duplicate_count"] == 1
    assert daily.cell_audit.loc[0, "resolution"] == "equivalent_duplicate_collapsed"
