"""DS disposition evidence: view classification and selector semantics."""

from __future__ import annotations

from conftest import members, run_query
from test_cohort import _query, _rule


def _derived(name: str, value_type: str) -> dict[str, str]:
    return {"kind": "derived", "name": name, "value_type": value_type}


def _subjects():
    return [
        {"study_id": "S1", "subject_id": str(i), "dose_duration": 28}
        for i in range(1, 7)
    ]


def _dispositions():
    return [
        # In-window terminal sacrifice (offset +1).
        {"study_id": "S1", "subject_id": "1", "decode": "TERMINAL SACRIFICE", "day": 29},
        # Lower-case variant of the same decode.
        {"study_id": "S1", "subject_id": "2", "decode": "Terminal Sacrifice", "day": 27},
        # Terminal sacrifice beyond the window.
        {"study_id": "S1", "subject_id": "3", "decode": "SCHEDULED SACRIFICE", "day": 40},
        # Recovery arm.
        {"study_id": "S1", "subject_id": "4", "decode": "RECOVERY SACRIFICE", "day": 56},
        # Unscheduled death without any terminal record.
        {"study_id": "S1", "subject_id": "5", "decode": "FOUND DEAD", "day": 12},
        # No DS record at all.
    ]


def test_disposition_view_classifies_categories_and_offsets(make_connection) -> None:
    con = make_connection(_subjects(), dispositions=_dispositions())

    rows = con.execute(
        'SELECT "STUDYID", "USUBJID", disposition_category_values, '
        "terminal_sacrifice_offset_day_values FROM subject_disposition_summary "
        'ORDER BY "USUBJID"'
    ).fetchall()
    by_subject = {usubjid: (categories, offsets) for _, usubjid, categories, offsets in rows}

    assert by_subject["S1-1"] == (["TERMINAL"], [1])
    assert by_subject["S1-2"] == (["TERMINAL"], [-1])
    assert by_subject["S1-3"] == (["TERMINAL"], [12])
    assert by_subject["S1-4"] == (["RECOVERY"], None)
    assert by_subject["S1-5"] == (["UNSCHEDULED_DEATH"], None)
    # The subject without DS records has no view row.
    assert "S1-6" not in by_subject


def test_unknown_decodes_are_ignored_by_the_disposition_view(make_connection) -> None:
    con = make_connection(
        _subjects(),
        dispositions=[
            {"study_id": "S1", "subject_id": "1", "decode": "", "day": 29},
            {"study_id": "S1", "subject_id": "2", "decode": "RANDOM TEST", "day": 29},
        ],
    )

    rows = con.execute(
        "SELECT count(*) FROM subject_disposition_summary"
    ).fetchone()

    assert rows[0] == 0


def test_terminal_offset_window_requires_an_in_window_value(make_connection) -> None:
    con = make_connection(_subjects(), dispositions=_dispositions())
    window = _rule(
        "between",
        [-1, 4],
        field=_derived("terminal_sacrifice_offset_days", "number"),
    )

    assert members(run_query(con, _query(window))) == {("S1", "S1-1"), ("S1", "S1-2")}


def test_excluded_categories_drop_the_whole_subject(make_connection) -> None:
    con = make_connection(_subjects(), dispositions=_dispositions())
    excluded = _rule(
        "in",
        ["RECOVERY", "INTERIM", "UNSCHEDULED_DEATH", "REMOVED_ALIVE"],
        field=_derived("disposition_categories", "string"),
    )

    assert members(run_query(con, _query(excluded))) == {("S1", "S1-4"), ("S1", "S1-5")}

    not_excluded = {
        "group": "and",
        "children": [
            {
                "group": "or",
                "negated": True,
                "children": [excluded],
            }
        ],
    }
    assert members(run_query(con, _query(not_excluded))) == {
        ("S1", "S1-1"),
        ("S1", "S1-2"),
        ("S1", "S1-3"),
        ("S1", "S1-6"),
    }


def test_terminal_window_group_matches_committed_query_semantics(
    make_connection,
) -> None:
    con = make_connection(_subjects(), dispositions=_dispositions())
    excluded_rule = _rule(
        "in",
        ["RECOVERY", "INTERIM", "UNSCHEDULED_DEATH", "REMOVED_ALIVE"],
        field=_derived("disposition_categories", "string"),
    )
    has_terminal = _rule("eq", "TERMINAL", field=_derived("disposition_categories", "string"))
    window = _rule(
        "between",
        [-1, 4],
        field=_derived("terminal_sacrifice_offset_days", "number"),
    )
    predicate = {
        "group": "and",
        "children": [
            {
                "group": "or",
                "negated": True,
                "children": [excluded_rule],
            },
            {
                "group": "or",
                "children": [
                    {"group": "and", "negated": True, "children": [has_terminal]},
                    window,
                ],
            },
        ],
    }

    # Recovery/unscheduled subjects drop via the exclusion arm; terminal
    # subjects survive only with an in-window offset; subjects with no
    # terminal record pass through.
    assert members(run_query(con, _query(predicate))) == {
        ("S1", "S1-1"),
        ("S1", "S1-2"),
        ("S1", "S1-6"),
    }


def test_subjects_without_a_ds_table_match_no_disposition_rules(
    make_connection,
) -> None:
    con = make_connection(_subjects())
    excluded = _rule(
        "in",
        ["RECOVERY", "INTERIM", "UNSCHEDULED_DEATH", "REMOVED_ALIVE"],
        field=_derived("disposition_categories", "string"),
    )

    assert members(run_query(con, _query(excluded))) == set()
