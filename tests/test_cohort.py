"""Synthetic subject-set execution tests for v3 predicates."""

from __future__ import annotations

import pytest

from vcgen_query import execute_query, parse_query


def _send(
    column: str,
    value_type: str = "string",
    domain: str = "DM",
) -> dict[str, str]:
    return {
        "kind": "send_column",
        "domain": domain,
        "column": column,
        "value_type": value_type,
    }


def _rule(
    operator: str,
    value: object = None,
    *,
    field: dict[str, str] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {"field": field or _send("SEX"), "operator": operator}
    if value is not None or operator not in {"is_empty", "is_not_empty"}:
        rule["value"] = value
    return rule


def _query(predicate: dict[str, object] | None, **extra: object) -> object:
    raw: dict[str, object] = {
        "contract": "vcgen.cohort_query.v4",
        "query_id": "synthetic-execution",
        **extra,
    }
    if predicate is not None:
        raw["predicate"] = predicate
    return parse_query(raw)


def _members(result) -> set[tuple[str, str]]:
    return {(row["study_id"], row["subject_id"]) for row in result.subjects.to_dict("records")}


def _run(con, predicate: dict[str, object] | None):
    return execute_query(con, _query(predicate))


@pytest.fixture
def operator_source(make_connection):
    con = make_connection(
        [
            {
                "study_id": "S1",
                "subject_id": "1",
                "sex": "M",
                "age": 10,
                "rfstdtc": "2020-01-01",
            },
            {
                "study_id": "S2",
                "subject_id": "2",
                "sex": "F",
                "age": 20,
                "rfstdtc": "2021-01-01T08:00:00",
            },
            {
                "study_id": "S3",
                "subject_id": "3",
                "sex": "M",
                "age": 30,
                "rfstdtc": "2022-01-01T00:00:00Z",
            },
            {
                "study_id": "S4",
                "subject_id": "4",
                "sex": "X",
                "age": "not-a-number",
                "rfstdtc": "not-a-timestamp",
            },
            {
                "study_id": "S5",
                "subject_id": "5",
                "sex": "X",
                "age": 0,
                "rfstdtc": "2023-01-01",
            },
        ]
    )
    con.execute("UPDATE dm SET STRAIN = NULL WHERE STUDYID IN (?, ?, ?)", ["S3", "S4", "S5"])
    con.execute("UPDATE dm SET SEX = NULL, AGE = NULL, RFSTDTC = NULL WHERE STUDYID = ?", ["S5"])
    con.execute("UPDATE dm SET SEX = NULL WHERE STUDYID = ?", ["S4"])
    return con


@pytest.mark.parametrize(
    ("operator", "value", "field", "studies"),
    [
        ("eq", "M", _send("SEX"), {"S1", "S3"}),
        ("ne", "M", _send("SEX"), {"S2"}),
        ("in", ["M", "F"], _send("SEX"), {"S1", "S2", "S3"}),
        ("not_in", ["M"], _send("SEX"), {"S2"}),
        ("contains", "IST", _send("STRAIN"), {"S1", "S2"}),
        ("not_contains", "MOUSE", _send("STRAIN"), {"S1", "S2"}),
        ("starts_with", "WIS", _send("STRAIN"), {"S1", "S2"}),
        ("not_starts_with", "MOU", _send("STRAIN"), {"S1", "S2"}),
        ("ends_with", "HAN", _send("STRAIN"), {"S1", "S2"}),
        ("not_ends_with", "MOUSE", _send("STRAIN"), {"S1", "S2"}),
        ("gt", 10, _send("AGE", "number"), {"S2", "S3"}),
        ("gte", 20, _send("AGE", "number"), {"S2", "S3"}),
        ("lt", 20, _send("AGE", "number"), {"S1"}),
        ("lte", 20, _send("AGE", "number"), {"S1", "S2"}),
        ("between", [10, 20], _send("AGE", "number"), {"S1", "S2"}),
        ("not_between", [10, 20], _send("AGE", "number"), {"S3"}),
        ("eq", "2021-01-01T08:00:00", _send("RFSTDTC", "datetime"), {"S2"}),
    ],
)
def test_operator_truth_table_uses_typed_values_and_never_matches_missing_values(
    operator_source, operator, value, field, studies
) -> None:
    result = _run(operator_source, _rule(operator, value, field=field))

    assert {study_id for study_id, _ in _members(result)} == studies


def test_empty_operators_distinguish_zero_length_strings_from_null(
    make_connection,
) -> None:
    con = make_connection(
        [
            {"study_id": "VALUE", "subject_id": "1"},
            {"study_id": "EMPTY", "subject_id": "2"},
            {"study_id": "NULL", "subject_id": "3"},
            {"study_id": "WHITESPACE", "subject_id": "4"},
        ]
    )
    con.execute("UPDATE dm SET STRAIN = '' WHERE STUDYID = ?", ["EMPTY"])
    con.execute("UPDATE dm SET STRAIN = NULL WHERE STUDYID = ?", ["NULL"])
    con.execute("UPDATE dm SET STRAIN = ' ' WHERE STUDYID = ?", ["WHITESPACE"])

    assert {
        study for study, _ in _members(_run(con, _rule("is_empty", field=_send("STRAIN"))))
    } == {"EMPTY", "NULL"}
    assert {
        study for study, _ in _members(_run(con, _rule("is_not_empty", field=_send("STRAIN"))))
    } == {"VALUE", "WHITESPACE"}


def test_offset_aware_datetime_rules_compare_normalized_instants(make_connection) -> None:
    con = make_connection(
        [
            {
                "study_id": "OFFSET",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T04:00:00.000+02:00",
            },
            {
                "study_id": "UTC",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T02:00:00Z",
            },
            {
                "study_id": "OTHER",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T04:00:00Z",
            },
        ]
    )

    result = _run(
        con,
        _rule(
            "eq",
            "2020-06-16T04:00:00+02:00",
            field=_send("RFSTDTC", "datetime"),
        ),
    )

    assert _members(result) == {("OFFSET", "OFFSET-1"), ("UTC", "UTC-1")}


def test_aware_datetime_rule_does_not_coerce_naive_source_timestamp(
    make_connection,
) -> None:
    con = make_connection(
        [
            {
                "study_id": "AWARE",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T02:00:00Z",
            },
            {
                "study_id": "NAIVE",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T02:00:00",
            },
        ]
    )

    result = _run(
        con,
        _rule(
            "eq",
            "2020-06-16T02:00:00Z",
            field=_send("RFSTDTC", "datetime"),
        ),
    )

    assert _members(result) == {("AWARE", "AWARE-1")}


@pytest.mark.parametrize(
    ("operator", "value", "matches"),
    [
        ("eq", "2020-06-16T00:00:00", True),
        ("eq", "2020-06-16T02:00:00", False),
        ("gt", "2020-06-15T04:00:00", True),
        ("gt", "2020-06-16T03:00:00", False),
        ("lte", "2020-06-16T01:00:00", True),
    ],
)
def test_imported_grit42_rfstdtc_uses_catalogued_calendar_date_source(
    make_connection, operator: str, value: str, matches: bool
) -> None:
    con = make_connection(
        [
            {
                "study_id": "S",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T04:00:00.000+02:00",
            }
        ]
    )
    query = _query(
        _rule(operator, value, field=_send("RFSTDTC", "datetime")),
        import_provenance={
            "contract": "grit42.subject_query_import.v1",
            "source_contract": "grit42.advanced_subject_query.v1",
            "source_payload_sha256": "0" * 64,
        },
    )

    result = execute_query(con, query)

    assert bool(_members(result)) is matches
    assert any("calendar_date_midnight" in item for item in result.receipt["assumptions"])


def test_aware_datetime_ranges_normalize_each_bound(make_connection) -> None:
    con = make_connection(
        [
            {
                "study_id": "LOW",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T04:00:00+02:00",
            },
            {
                "study_id": "HIGH",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T03:00:00Z",
            },
            {
                "study_id": "OUTSIDE",
                "subject_id": "1",
                "rfstdtc": "2020-06-16T03:00:00-01:00",
            },
        ]
    )

    result = _run(
        con,
        _rule(
            "between",
            ["2020-06-16T02:00:00Z", "2020-06-16T05:00:00+02:00"],
            field=_send("RFSTDTC", "datetime"),
        ),
    )

    assert _members(result) == {("LOW", "LOW-1"), ("HIGH", "HIGH-1")}


def test_datetime_rule_rejects_mixed_aware_and_naive_values(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "rfstdtc": "2020-06-16T02:00:00Z"}])
    predicate = _rule(
        "in",
        ["2020-06-16T02:00:00Z", "2020-06-16T02:00:00"],
        field=_send("RFSTDTC", "datetime"),
    )

    with pytest.raises(ValueError, match="must not mix aware and naive"):
        _run(con, predicate)


def test_nested_boolean_algebra_uses_dm_universe_for_not(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S1", "subject_id": "1", "sex": "M"},
            {"study_id": "S1", "subject_id": "2", "sex": "F"},
            {"study_id": "S2", "subject_id": "3", "sex": "M"},
        ]
    )
    predicate = {
        "group": "and",
        "children": [
            {"group": "or", "children": [_rule("eq", "M"), _rule("eq", "F")]},
            {"group": "or", "negated": True, "children": [_rule("eq", "F")]},
        ],
    }

    assert _members(_run(con, predicate)) == {("S1", "S1-1"), ("S2", "S2-3")}


def test_study_grain_rule_propagates_to_every_dm_subject(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "MATCH", "subject_id": "1", "study_length": 28},
            {"study_id": "MATCH", "subject_id": "2", "study_length": 28},
            {"study_id": "MISS", "subject_id": "3", "study_length": 14},
        ]
    )

    result = _run(con, _rule("eq", "28", field=_send("TSVAL", "string", "TS")))

    assert _members(result) == {("MATCH", "MATCH-1"), ("MATCH", "MATCH-2")}


def test_dose_duration_selector_respects_source_duration_units(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "DAYS", "subject_id": "1", "study_length": "P28D"},
            {"study_id": "WEEKS_EQUIVALENT", "subject_id": "1", "study_length": "P4W"},
            {"study_id": "TWENTY_EIGHT_WEEKS", "subject_id": "1", "study_length": "P28W"},
            {"study_id": "MULTIPLE", "subject_id": "1", "study_length": "P14D"},
        ],
        study_metadata={"MULTIPLE": {"DOSDUR": ["P28D"]}},
    )
    predicate = _rule(
        "eq",
        28,
        field={
            "kind": "derived",
            "name": "dose_duration_days",
            "value_type": "number",
        },
    )

    assert _members(_run(con, predicate)) == {
        ("DAYS", "DAYS-1"),
        ("WEEKS_EQUIVALENT", "WEEKS_EQUIVALENT-1"),
        ("MULTIPLE", "MULTIPLE-1"),
    }


def test_cross_domain_rules_compose_as_subject_key_sets(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S1", "subject_id": "1", "sex": "M", "exroute": "ORAL GAVAGE"},
            {"study_id": "S1", "subject_id": "2", "sex": "M", "exroute": "DERMAL"},
            {"study_id": "S2", "subject_id": "3", "sex": "F", "exroute": "ORAL GAVAGE"},
        ]
    )
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "M", field=_send("SEX")),
            _rule("eq", "ORAL GAVAGE", field=_send("EXROUTE", "string", "EX")),
        ],
    }

    assert _members(_run(con, predicate)) == {("S1", "S1-1")}


def test_same_group_same_domain_rules_must_match_one_source_row(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "HGB", "value": 10},
            {"study_id": "S", "subject_id": "1", "test_code": "HCT", "value": 20},
        ],
    )
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "HGB", field=_send("LBTESTCD", "string", "LB")),
            _rule("gte", 20, field=_send("LBSTRESN", "number", "LB")),
        ],
    }

    assert _members(_run(con, predicate)) == set()


def test_nested_same_domain_groups_lift_to_independent_subject_sets(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "HGB", "value": 10},
            {"study_id": "S", "subject_id": "1", "test_code": "HCT", "value": 20},
        ],
    )
    predicate = {
        "group": "and",
        "children": [
            {
                "group": "and",
                "children": [_rule("eq", "HGB", field=_send("LBTESTCD", "string", "LB"))],
            },
            {
                "group": "and",
                "children": [_rule("gte", 20, field=_send("LBSTRESN", "number", "LB"))],
            },
        ],
    }

    assert _members(_run(con, predicate)) == {("S", "S-1")}


def test_direct_rule_and_nested_same_domain_group_are_independent_subject_sets(
    make_connection,
) -> None:
    con = make_connection(
        [{"study_id": "S", "subject_id": "1"}],
        lab=[
            {"study_id": "S", "subject_id": "1", "test_code": "AST", "value": 1.0},
            {"study_id": "S", "subject_id": "1", "test_code": "ALT", "value": 2.0},
        ],
    )
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "AST", field=_send("LBTESTCD", "string", "LB")),
            {
                "group": "and",
                "children": [_rule("gt", 1.8, field=_send("LBSTRESN", "number", "LB"))],
            },
        ],
    }

    assert _members(_run(con, predicate)) == {("S", "S-1")}


def test_same_domain_or_group_matches_any_source_row(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "AST"},
            {"study_id": "S", "subject_id": "ALT"},
            {"study_id": "S", "subject_id": "OTHER"},
        ],
        lab=[
            {"study_id": "S", "subject_id": "AST", "test_code": "AST", "value": 1.0},
            {"study_id": "S", "subject_id": "ALT", "test_code": "ALT", "value": 1.0},
            {"study_id": "S", "subject_id": "OTHER", "test_code": "HGB", "value": 1.0},
        ],
    )
    predicate = {
        "group": "or",
        "children": [
            _rule("eq", "AST", field=_send("LBTESTCD", "string", "LB")),
            _rule("eq", "ALT", field=_send("LBTESTCD", "string", "LB")),
        ],
    }

    assert _members(_run(con, predicate)) == {("S", "S-AST"), ("S", "S-ALT")}


def test_study_grain_same_domain_group_requires_one_ts_row(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "MATCH", "subject_id": "1"},
            {"study_id": "MATCH", "subject_id": "2"},
            {"study_id": "SPLIT", "subject_id": "1"},
        ]
    )
    con.execute(
        "INSERT INTO ts VALUES (?, ?, ?, ?, ?)",
        ["MATCH", "2", "SSTYP", "Study Type", "REPEAT DOSE TOXICITY"],
    )
    con.execute(
        "INSERT INTO ts VALUES (?, ?, ?, ?, ?)",
        ["SPLIT", "2", "SSTYP", "Study Type", "OTHER"],
    )
    con.execute(
        "INSERT INTO ts VALUES (?, ?, ?, ?, ?)",
        ["SPLIT", "3", "OTHER", "Other", "REPEAT DOSE TOXICITY"],
    )
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "SSTYP", field=_send("TSPARMCD", "string", "TS")),
            _rule(
                "eq",
                "REPEAT DOSE TOXICITY",
                field=_send("TSVAL", "string", "TS"),
            ),
        ],
    }

    assert _members(_run(con, predicate)) == {
        ("MATCH", "MATCH-1"),
        ("MATCH", "MATCH-2"),
    }


def test_same_row_scope_pairs_measurement_name_and_value(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "MATCH"},
            {"study_id": "S", "subject_id": "SPLIT"},
        ],
        lab=[
            {"study_id": "S", "subject_id": "MATCH", "test_code": "AST", "value": 2.0},
            {"study_id": "S", "subject_id": "SPLIT", "test_code": "AST", "value": 1.0},
            {"study_id": "S", "subject_id": "SPLIT", "test_code": "ALT", "value": 2.0},
        ],
    )
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "AST", field=_send("LBTESTCD", "string", "LB")),
            _rule("gt", 1.8, field=_send("LBSTRESN", "number", "LB")),
        ],
    }

    assert _members(_run(con, predicate)) == {("S", "S-MATCH")}


def test_negated_same_row_group_negates_each_row_before_subject_lift(make_connection) -> None:
    con = make_connection(
        [
            {"study_id": "S", "subject_id": "MATCH"},
            {"study_id": "S", "subject_id": "SPLIT"},
            {"study_id": "S", "subject_id": "NONE"},
        ],
        lab=[
            {"study_id": "S", "subject_id": "MATCH", "test_code": "AST", "value": 2.0},
            {"study_id": "S", "subject_id": "SPLIT", "test_code": "AST", "value": 1.0},
            {"study_id": "S", "subject_id": "SPLIT", "test_code": "ALT", "value": 2.0},
        ],
    )
    predicate = {
        "group": "and",
        "negated": True,
        "children": [
            _rule("eq", "AST", field=_send("LBTESTCD", "string", "LB")),
            _rule("gt", 1.8, field=_send("LBSTRESN", "number", "LB")),
        ],
    }

    assert _members(_run(con, predicate)) == {("S", "S-SPLIT")}


def test_injected_value_is_bound_as_data_not_executed_sql(make_connection) -> None:
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}])

    result = _run(con, _rule("eq", "M' OR 1=1 --"))

    assert _members(result) == set()
    assert con.execute("SELECT count(*) FROM dm").fetchone()[0] == 1


def test_absent_predicate_returns_the_dm_universe(make_connection, basic_subjects) -> None:
    result = _run(make_connection(basic_subjects), None)

    assert len(_members(result)) == len(basic_subjects)


def test_null_exposure_cells_never_crash_and_never_match(make_connection) -> None:
    """EX rows with NULL route/frequency/treatment must not raise QueryError.

    ``subject_exposure_summary`` aggregates raw EX columns as lists without
    NULL guards; the regression is a ``None`` element reaching the categorical
    normalizer. Missing cells fail the selector instead of crashing execution.
    """

    con = make_connection(
        [
            {
                "study_id": "S1",
                "subject_id": "1",
                "exroute": None,
                "exdosfrq": None,
                "extrt": None,
            },
            {"study_id": "S2", "subject_id": "1"},
        ]
    )

    for name, expected in (
        ("route", {("S2", "S2-1")}),
        ("dose_frequency", {("S2", "S2-1")}),
        ("treatment_vehicle", {("S2", "S2-1")}),
    ):
        result = _run(
            con,
            {
                "group": "and",
                "children": [
                    {
                        "field": {
                            "kind": "derived",
                            "name": name,
                            "value_type": "string",
                        },
                        "operator": "eq",
                        "value": "ORAL GAVAGE"
                        if name == "route"
                        else "QD"
                        if name == "dose_frequency"
                        else "0.5% METHOCEL",
                    }
                ],
            },
        )
        assert _members(result) == expected
