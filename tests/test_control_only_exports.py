"""Control-only exports and dose frequency aliases.

Many data partners export only the control animals of a study, without DM ARM,
EX and DS. Such a subject takes its route,
dose frequency and vehicle from TS, and its terminal disposition from its
necropsy day. The dose frequency field resolves spellings through the reviewed
frequency aliases. These checks cover what a population count cannot identify.

Failure modes (written after the implementation, against the cases the count
cannot show):

1. A subject with EX rows takes TS values, so a study's declared frequency
   overrides the frequency its animals were dosed with.
2. A subject without EX rows gets no TS values, or a TS route gets a method.
3. A frequency spelling of a canonical frequency (case, a synonym, a TS
   spelling) does not select it; a count of administrations (TWICE) selects
   twice daily (BID); an unlisted spelling selects a frequency.
4. A raw frequency spelling in a query is accepted and selects no subject.
5. A subject with a DS record also takes a necropsy-dated record, so pathology
   overrides DS; a DS record with an unlisted decode gets a necropsy-dated
   record; a subject without DS and pathology gets a terminal record; a
   recovery necropsy gets an in-window offset.
6. A study has EX rows, but one of its animals has none (the export lost them).
   The animal takes the TS route, frequency and vehicle, so a dosed animal passes
   the control rule "no exposure row and a known TS vehicle" as a vehicle control.

Failure modes of the arm check of the TS fallback (written before the change):

9. A study without EX rows is not a control-only export: it has dosed arms in
   DM ARM. A dosed animal (`ACC-2000 mg/kg`) takes the TS vehicle and passes the
   control rule as a vehicle control.
10. The arm check is too wide: a positive control, an arm with a control word
   and a nonzero dose (`Vehicle G 10mg/kg`, `Control 0,5 mg/kg`), or an arm
   text without a control word or a zero dose (`ACC-None`, `250 MG/KG`) passes.
11. The arm check is too narrow: a subject without ARM, or a control arm
   spelling of the archive (`ACC-0 mg/kg/day`, `0.0 mg/kg`, `Vehicle control1`,
   `ACC-Placebo`, `Kont`, `0 mg/kg, A1`, `Water control`) gets no TS values.

Failure modes of the necropsy fallback date and its warnings (written before the
change to the earliest of the latest days of each pathology domain):

7. A late MI day (an evaluation or reporting date) dates the necropsy, so a
   main-study animal fails the terminal window. An early row of a domain dates
   it, not the latest day of the domain. A domain without a readable day counts.
8. The subject context stage does not report the subjects that the fallback
   dates, or the subjects whose pathology domains do not agree. It counts a
   subject with a DS record, a subject outside the request population, or a
   spread of exactly 4 days. A per-study count is wrong.
"""

from __future__ import annotations

import pytest

from conftest import run_query

from vcgen_query import CohortQuery, EndpointObservations, QueryError, evaluate_membership, parse_subject_query
from vcgen_query.duckdb import create_derived_views
from vcgen_query.population import population_vocabulary


def _frequency_query(operator: str, value) -> dict:
    return {"contract": "vcgen.subject_query.v1", "query_id": "frequency",
            "predicate": {"field": {"kind": "derived", "name": "dose_frequency", "value_type": "string"},
                          "operator": operator, "value": value}}


def _members(con, document: dict) -> set[tuple[str, str]]:
    result = evaluate_membership(con, parse_subject_query(document), close_connection=False)
    return {(str(study), str(subject)) for study, subject in result.subject_keys}


def _interval(study: str, subject: str, frequency: str) -> dict:
    return {"study_id": study, "subject_id": subject, "usubjid": f"{study}-{subject}", "exdosfrq": frequency,
            "exstdy": 1, "exendy": 28}


@pytest.fixture
def exposure(make_connection):
    subjects = [{"study_id": study, "subject_id": subject}
                for study, subject in (("A", "1"), ("A", "2"), ("A", "3"), ("A", "4"), ("B", "1"), ("C", "1"))]
    intervals = [_interval("A", "1", "QD"), _interval("A", "2", "QD – Once daily"), _interval("A", "3", "TWICE"),
                 _interval("A", "4", "QD 1-7, QW")]
    # Study A declares BID in TS, but its animals have EX rows; B and C have no EX rows.
    metadata = {"A": {"PDOSFRQ": "BID", "ROUTE": "INTRAVENOUS"},
                "B": {"PDOSFRQ": ["Once daily", "BID"], "ROUTE": "ORAL", "TRTV": "0.5% METHYLCELLULOSE"},
                "C": {"PDOSFRQ": "SEE PROTOCOLE"}}
    return make_connection(subjects, exposure_intervals=intervals, study_metadata=metadata)


def test_a_subject_without_ex_takes_ts_exposure_and_ex_keeps_its_own(exposure) -> None:
    rows = {subject: (routes, frequencies, vehicles) for subject, routes, frequencies, vehicles in exposure.execute(
        'SELECT "USUBJID", route_method_values, dose_frequency_values, treatment_values '
        "FROM subject_exposure_summary").fetchall()}
    assert rows["B-1"] == ([{"route": "ORAL", "method": None}], ["BID", "Once daily"], ["0.5% METHYLCELLULOSE"])
    assert rows["A-1"][1] == ["QD"]
    assert rows["A-1"][0] == [{"route": "ORAL GAVAGE", "method": "GAVAGE"}]
    assert rows["C-1"] == (None, ["SEE PROTOCOLE"], None)


def test_a_canonical_frequency_selects_its_spellings_and_nothing_else(exposure) -> None:
    assert _members(exposure, _frequency_query("eq", "QD")) == {("A", "A-1"), ("A", "A-2"), ("B", "B-1")}
    # TWICE counts administrations; study A's TS BID does not reach animals with EX rows.
    assert _members(exposure, _frequency_query("eq", "bid")) == {("B", "B-1")}
    assert _members(exposure, _frequency_query("eq", "TWICE")) == {("A", "A-3")}
    # The population offers the canonical frequencies, not the raw spellings.
    assert population_vocabulary(exposure)["dose_frequencies"] == ["QD", "BID", "TWICE"]


def test_a_subject_without_ex_in_a_study_with_ex_takes_no_ts_exposure(make_connection) -> None:
    subjects = [{"study_id": study, "subject_id": subject} for study, subject in (("D", "1"), ("D", "2"), ("E", "1"))]
    declared = {"PDOSFRQ": "QD", "ROUTE": "ORAL", "TRTV": "0.5% METHYLCELLULOSE"}
    # Study D has EX rows for D-1 only; study E is a control-only export without EX.
    con = make_connection(subjects, exposure_intervals=[_interval("D", "1", "QD")],
                          study_metadata={"D": declared, "E": declared})
    rows = {subject: (routes, frequencies, vehicles) for subject, routes, frequencies, vehicles in con.execute(
        'SELECT "USUBJID", route_method_values, dose_frequency_values, treatment_values '
        "FROM subject_exposure_summary").fetchall()}
    assert rows["D-2"] == (None, None, None)
    assert rows["E-1"] == ([{"route": "ORAL", "method": None}], ["QD"], ["0.5% METHYLCELLULOSE"])
    # The control rule of the study design for a subject without exposure rows.
    no_exposure_row = {"group": "and", "negated": True, "children": [{"group": "or", "children": [
        {"field": {"kind": "send_column", "domain": "EX", "column": "EXTRT", "value_type": "string"},
         "operator": operator} for operator in ("is_empty", "is_not_empty")]}]}
    known_vehicle = {"field": {"kind": "derived", "name": "treatment_vehicle", "value_type": "string"},
                     "operator": "not_in", "value": ["UNKNOWN"]}
    document = {"contract": "vcgen.subject_query.v1", "query_id": "control_without_ex",
                "predicate": {"group": "and", "children": [no_exposure_row, known_vehicle]}}
    assert _members(con, document) == {("E", "E-1")}


CONTROL_ARMS = (None, "Vehicle Control", "ACC-0 mg/kg/day", "0.0 mg/kg", "Vehicle control1", "ACC-Placebo",
                "Kont", "0 mg/kg, A1", "Water control", "ACC-0 mg/kg/day, Vehicle control", "ACC-Vehicle 0mg/kg")
DOSED_ARMS = ("ACC-2000 mg/kg", "Positive control", "Vehicle G 10mg/kg", "Control 0,5 mg/kg", "ACC-None",
              "250 MG/KG", "ACC-1 MG/KG/DAY")


def test_the_ts_fallback_reaches_only_subjects_of_a_control_arm(make_connection) -> None:
    arms = (*CONTROL_ARMS, *DOSED_ARMS)
    subjects = [{"study_id": "F", "subject_id": str(index), "arm": arm} for index, arm in enumerate(arms)]
    con = make_connection(subjects, exposure_intervals=[], study_metadata={
        "F": {"PDOSFRQ": "QD", "ROUTE": "ORAL", "TRTV": "0.5% METHYLCELLULOSE"}})
    vehicles = dict(con.execute(
        'SELECT dm."ARM", summary.treatment_values FROM subject_exposure_summary AS summary '
        'JOIN dm USING ("STUDYID", "USUBJID")').fetchall())
    assert {arm: vehicles[arm] for arm in arms} == {
        **{arm: ["0.5% METHYLCELLULOSE"] for arm in CONTROL_ARMS}, **{arm: None for arm in DOSED_ARMS}}
    known_vehicle = {"contract": "vcgen.subject_query.v1", "query_id": "vehicle", "predicate": {
        "field": {"kind": "derived", "name": "treatment_vehicle", "value_type": "string"},
        "operator": "not_in", "value": ["UNKNOWN"]}}
    assert _members(con, known_vehicle) == {("F", f"F-{index}") for index in range(len(CONTROL_ARMS))}


@pytest.mark.parametrize("operator, value", [("eq", "Once daily"), ("in", ["QD", "QD – ONCE DAILY"])])
def test_a_frequency_that_is_not_canonical_is_refused(operator, value) -> None:
    with pytest.raises(QueryError, match="not a canonical dose frequency"):
        parse_subject_query(_frequency_query(operator, value))


def _necropsy_source(make_connection):
    """Study S has one DS record (S-4); study T has a DS terminal record (T-1). All other dates are pathology days."""

    subjects = [{"study_id": "S", "subject_id": str(index), "dose_duration": 28, "sex": "F" if index == 8 else "M"}
                for index in range(1, 9)]
    subjects += [{"study_id": "T", "subject_id": str(index), "dose_duration": 28, "sex": "M"} for index in (1, 2)]
    con = make_connection(subjects, dispositions=[
        {"study_id": "S", "subject_id": "4", "decode": "RECOVERY SACRIFICE", "day": 57},
        {"study_id": "T", "subject_id": "1", "decode": "TERMINAL SACRIFICE", "day": 29}])
    con.execute("CREATE TABLE mi (STUDYID VARCHAR, USUBJID VARCHAR, MIDY VARCHAR)")
    con.execute("CREATE TABLE ma (STUDYID VARCHAR, USUBJID VARCHAR, MADY VARCHAR)")
    con.execute("CREATE TABLE om (STUDYID VARCHAR, USUBJID VARCHAR, OMDY VARCHAR)")
    con.executemany("INSERT INTO mi VALUES (?, ?, ?)", [
        # S-1 has an early MI row and a row without a day; the latest MI day is 29.
        ("S", "S-1", "15"), ("S", "S-1", "29"), ("S", "S-1", None), ("S", "S-2", "57"), ("S", "S-4", "57"),
        # S-6, S-8 and T-2 have an MI day long after the necropsy; S-7 is 4 days late; T-1 has a DS record.
        ("S", "S-6", "60"), ("S", "S-7", "33"), ("S", "S-8", "70"), ("T", "T-1", "90"), ("T", "T-2", "80")])
    con.executemany("INSERT INTO ma VALUES (?, ?, ?)", [("T", "T-2", "29"), ("S", "S-2", "")])
    # S-3 is weighed at necropsy but has no MI; S-5 has no pathology (a satellite animal).
    con.executemany("INSERT INTO om VALUES (?, ?, ?)", [
        ("S", "S-3", "30"), ("S", "S-1", "28"), ("S", "S-6", "29"), ("S", "S-7", "29"), ("S", "S-8", "29")])
    create_derived_views(con)
    return con


def test_a_subject_without_ds_is_dated_by_its_necropsy(make_connection) -> None:
    con = _necropsy_source(make_connection)
    rows = {subject: values for subject, *values in con.execute(
        'SELECT "USUBJID", disposition_category_values, terminal_sacrifice_offset_day_values, '
        "terminal_record_source, pathology_day_spread_days FROM subject_disposition_summary").fetchall()}
    assert rows == {
        # The earliest of the latest MI day (29) and the latest OM day (28).
        "S-1": [["TERMINAL"], [0], "necropsy", 1],
        "S-2": [["TERMINAL"], [29], "necropsy", 0],
        "S-3": [["TERMINAL"], [2], "necropsy", 0],
        "S-4": [["RECOVERY"], None, None, None],
        "S-6": [["TERMINAL"], [1], "necropsy", 31],
        "S-7": [["TERMINAL"], [1], "necropsy", 4],
        "S-8": [["TERMINAL"], [1], "necropsy", 41],
        "T-1": [["TERMINAL"], [1], "disposition", None],
        "T-2": [["TERMINAL"], [1], "necropsy", 51],
    }


def test_the_subject_context_stage_reports_the_necropsy_fallback(make_connection) -> None:
    con = _necropsy_source(make_connection)
    males = {"contract": "vcgen.subject_query.v1", "query_id": "males",
             "predicate": {"field": {"kind": "derived", "name": "sex", "value_type": "string"},
                           "operator": "eq", "value": "M"}}
    query = CohortQuery(parse_subject_query(males), (EndpointObservations(("BW_BW",)),))
    result = run_query(con, query, close_connection=False)
    stage = next(record.to_dict() for record in result.trace if record.info.id == "subject_context")
    warnings = {warning["code"]: (warning["count"], warning["by_study"]) for warning in stage["warnings"]}
    # S-8 is not in the request population; T-1 and S-4 have a DS record; S-5 has no terminal record.
    assert warnings["terminal_day_from_necropsy"] == (6, {"S": 5, "T": 1})
    # S-7 differs by 4 days, which the terminal window tolerates.
    assert warnings["pathology_days_inconsistent"] == (2, {"S": 1, "T": 1})


def test_a_subject_with_an_unlisted_ds_decode_gets_no_necropsy_record(make_connection) -> None:
    """A DS row with a decode outside the recognized terms gets a made-up TERMINAL record from its necropsy day."""
    con = make_connection(
        [{"study_id": "S", "subject_id": "1", "dose_duration": 28}],
        dispositions=[{"study_id": "S", "subject_id": "1", "decode": "EUTHANIZED", "day": 30}])
    con.execute("CREATE TABLE mi (STUDYID VARCHAR, USUBJID VARCHAR, MIDY VARCHAR)")
    con.execute("INSERT INTO mi VALUES ('S', 'S-1', '30')")
    create_derived_views(con)
    assert con.execute(
        "SELECT USUBJID FROM subject_disposition_summary "
        "WHERE list_contains(disposition_category_values, 'TERMINAL')").fetchall() == []
