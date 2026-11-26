"""Data decisions: recorded source-defect decisions (`vcgen.data_decisions.v3`) that the query applies.

Failure modes (written before the implementation):

- an imputation relabels a row of another sample matrix (a urine glucose row
  in a chemistry cell becomes serum glucose), or a class that the decision
  does not name;
- a change of the decision file does not change the query receipt identity;
- a malformed decision (unknown finding, unknown panel, a value outside the
  panel classes, two conflicting decisions for one cell) is accepted;
- a method decision removes rows of another endpoint, rows of the named
  endpoint with another method, or rows of another study; it matches the
  method text by case; it names an endpoint outside its panel, or no method;
- a row with an unknown specimen whose LBCAT is a category of another panel
  takes a decision of this panel (urine pH became blood pH, plan C31), or a
  row whose LBCAT is of this panel, of no panel, or blank does not.

Curated decisions (plan C26 to C28):

- the runtime reads a decision file that the lock does not name: a changed
  byte, a missing file, or a lock without `data_decisions.yaml` (C27);
- a decision names an endpoint that the catalogue does not have, a panel that
  the endpoint does not have, a reading that the endpoint cannot convert, or
  no source or two sources (C26);
- `reinterpret_unit` does not give the record the reading as its unit, or changes
  rows of another study or another unit; `exclude_endpoint` removes rows of
  another study or endpoint, or the trace does not count them (C28).
"""

from __future__ import annotations

from copy import deepcopy

import pytest

import hashlib

import yaml

from conftest import run_query
from vcgen_query import CohortQuery, QueryError, parse_subject_query
from vcgen_query.data_decisions import DataDecisions, data_decisions_from_mapping, load_locked_decisions
from vcgen_query.endpoint_catalogue import catalogue_location

REVIEWED = "dvc-md5:" + "a" * 32


def _document(*decisions: dict[str, object]) -> dict[str, object]:
    """A curated document; a decision without a source gets the rule source of the curate step."""

    return {
        "contract": "vcgen.data_decisions.v3",
        "source_snapshot_identity": REVIEWED,
        "exceptions_sha256": "e" * 64,
        "findings": [
            {"id": "SPEC-001", "title": "Imputed", "upstream": "Record LBSPEC.", "evidence": "eda/x.md#spec-001"},
            {"id": "SPEC-004", "title": "Excluded", "upstream": "Record LBSPEC.", "evidence": "eda/x.md#spec-004"},
            {"id": "SPEC-005", "title": "Dropped", "upstream": "Check LBSPEC.", "evidence": "eda/x.md#spec-005"},
            {"id": "TCODE-001", "title": "Code", "upstream": "Check LBTESTCD.", "evidence": "eda/y.md#tcode-001"},
        ],
        "decisions": [decision if {"rule", "exception"} & set(decision) else {**decision, "rule": "specimen_resolution"}
                      for decision in decisions],
    }


def _decisions(*decisions: dict[str, object]) -> DataDecisions:
    return data_decisions_from_mapping(_document(*decisions), md5="d" * 32)


def _lab(study: str, specimen_alt: str, *, extra: tuple[dict[str, object], ...] = ()) -> list[dict[str, object]]:
    rows = [
        {"study_id": study, "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
         "unit": "µkat/L", "day": 28, "value": 0.6, "specimen": specimen_alt, "sequence": 1},
        {"study_id": study, "subject_id": "1", "test_code": "HCT", "test_name": "HEMATOCRIT",
         "unit": "%", "day": 28, "value": 44, "specimen": "WHOLE BLOOD", "sequence": 2},
    ]
    return rows + list(extra)


def _run(make_connection, lab, decisions: DataDecisions, *, source_identity: str = REVIEWED):
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}], lab=lab)
    subject_query = parse_subject_query({
        "contract": "vcgen.subject_query.v1",
        "query_id": "decisions",
        "predicate": {"field": {"kind": "send_column", "domain": "DM", "column": "SEX", "value_type": "string"},
                      "operator": "eq", "value": "M"},
    })
    query = CohortQuery.compose(subject_query, ["LB_ALT_BLOOD", "LB_HCT_BLOOD"])
    return run_query(con, query, source_data_identity=source_identity, data_decisions=decisions)


def _stage(result):
    return next(item for item in result.trace if item.info.id == "endpoint_observations")


def test_impute_resolves_unknown_rows_and_keeps_other_panels(make_connection) -> None:
    decisions = _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                            "specimen_panel": "blood_chemistry", "value": "SERUM"})
    result = _run(make_connection, _lab("S", "no data"), decisions)
    records = {row["endpoint_name"]: row for row in result.observations.to_dict("records")}
    assert records["LB_ALT_BLOOD"]["specimen"] == "SERUM"
    assert records["LB_HCT_BLOOD"]["specimen"] == "WHOLE_BLOOD"
    applied = _stage(result).details["data_decisions"]["applied"]
    assert applied == {"SPEC-001": {"action": "impute_specimen", "rows": 1, "studies": 1}}
    assert not [drop for drop in _stage(result).drops if drop.reason.startswith("data_decision:")]


@pytest.mark.parametrize(("category", "imputed"), [
    ("URINALYSIS", False), ("CLINICAL CHEMISTRY", True), ("IMMUNOASSAY", True), (None, True),
])
def test_impute_skips_unknown_rows_of_another_panel_category(make_connection, category, imputed) -> None:
    decisions = _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                            "specimen_panel": "blood_chemistry", "value": "SERUM"})
    lab = [{**row, "category": category} if row["test_code"] == "ALT" else row for row in _lab("S", "no data")]
    result = _run(make_connection, lab, decisions)
    names = {row["endpoint_name"] for row in result.observations.to_dict("records")}
    assert ("LB_ALT_BLOOD" in names) is imputed
    assert "LB_HCT_BLOOD" in names


def test_impute_never_overwrites_a_real_allowed_class(make_connection) -> None:
    decisions = _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                            "specimen_panel": "blood_chemistry", "value": "SERUM"})
    result = _run(make_connection, _lab("S", "PLASMA"), decisions)
    alt = next(row for row in result.observations.to_dict("records") if row["endpoint_name"] == "LB_ALT_BLOOD")
    assert alt["specimen"] == "PLASMA"
    assert _stage(result).details["data_decisions"]["applied"] == {}


def test_impute_fills_unknown_rows_and_only_the_named_relabels(make_connection) -> None:
    urine = {"study_id": "S", "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
             "unit": "µkat/L", "day": 14, "value": 0.1, "specimen": "URINE", "sequence": 3}
    whole_blood = {**urine, "specimen": "WHOLE BLOOD", "day": 7, "sequence": 4}
    fill_only = _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                            "specimen_panel": "blood_chemistry", "value": "SERUM"})
    result = _run(make_connection, _lab("S", "no data", extra=(urine, whole_blood)), fill_only)
    alt = result.observations.loc[result.observations["endpoint_name"] == "LB_ALT_BLOOD"]
    assert list(alt["specimen"]) == ["SERUM"] and list(alt["study_day"]) == [28]
    relabel = _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                          "specimen_panel": "blood_chemistry", "value": "SERUM", "relabel": ["WHOLE_BLOOD"]})
    result = _run(make_connection, _lab("S", "no data", extra=(urine, whole_blood)), relabel)
    alt = result.observations.loc[result.observations["endpoint_name"] == "LB_ALT_BLOOD"]
    assert sorted(alt["study_day"]) == [7, 28] and set(alt["specimen"]) == {"SERUM"}
    with pytest.raises(ValueError, match="matrix"):
        _decisions({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
                    "specimen_panel": "blood_chemistry", "value": "SERUM", "relabel": ["URINE"]})


def test_exclusion_removes_only_its_panel_and_is_counted_once(make_connection) -> None:
    decisions = _decisions({"finding": "SPEC-004", "action": "exclude_panel", "study_id": "S",
                            "specimen_panel": "blood_chemistry"})
    second_alt = {"study_id": "S", "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
                  "unit": "µkat/L", "day": 14, "value": 0.5, "specimen": "no data", "sequence": 3}
    urine_alt = {**second_alt, "specimen": "URINE", "day": 7, "sequence": 4}
    result = _run(make_connection, _lab("S", "no data", extra=(second_alt, urine_alt)), decisions)
    assert set(result.observations["endpoint_name"]) == {"LB_HCT_BLOOD"}
    drops = [drop for drop in _stage(result).drops if drop.reason == "data_decision:SPEC-004"]
    assert len(drops) == 1 and drops[0].unit == "observations" and drops[0].count == 2
    assert dict(drops[0].by_study) == {"S": 2}
    assert result.receipt["observations"]["counts"]["source_rows_data_decision"] == 2


def test_drop_rows_removes_only_the_named_classes(make_connection) -> None:
    decisions = _decisions({"finding": "SPEC-005", "action": "drop_rows", "study_id": "S",
                            "specimen_panel": "blood_chemistry", "specimens": ["WHOLE_BLOOD"]})
    serum = {"study_id": "S", "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
             "unit": "µkat/L", "day": 14, "value": 0.5, "specimen": "SERUM", "sequence": 3}
    result = _run(make_connection, _lab("S", "WHOLE BLOOD", extra=(serum,)), decisions)
    alt = result.observations.loc[result.observations["endpoint_name"] == "LB_ALT_BLOOD"]
    assert list(alt["specimen"]) == ["SERUM"]
    drops = [drop for drop in _stage(result).drops if drop.reason == "data_decision:SPEC-005"]
    assert [drop.count for drop in drops] == [1]


METHOD_DROP = {"finding": "TCODE-001", "action": "drop_method_rows", "study_id": "S",
               "specimen_panel": "blood_chemistry", "endpoint": "LB_ALT_BLOOD",
               "method_contains": ["aspartate aminotransferase"]}


def test_method_rows_drop_only_the_named_endpoint_and_method(make_connection) -> None:
    mislabelled = {"study_id": "S", "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
                   "unit": "µkat/L", "day": 28, "value": 1.77, "specimen": "SERUM", "sequence": 3,
                   "method": "Cobas 8000:Aspartate Aminotransferase with P5P"}
    alt = {**mislabelled, "value": 0.45, "sequence": 4, "method": "Cobas 8000:Alanine Aminotransferase with P5P"}
    ast = {**mislabelled, "test_code": "AST", "test_name": "ASPARTATE AMINOTRANSFERASE", "sequence": 5}
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}], lab=[mislabelled, alt, ast])
    subject_query = parse_subject_query({
        "contract": "vcgen.subject_query.v1", "query_id": "method",
        "predicate": {"field": {"kind": "send_column", "domain": "DM", "column": "SEX", "value_type": "string"},
                      "operator": "eq", "value": "M"},
    })
    query = CohortQuery.compose(subject_query, ["LB_ALT_BLOOD", "LB_AST_BLOOD"])
    result = run_query(con, query, source_data_identity=REVIEWED,
                           data_decisions=_decisions(METHOD_DROP))
    values = {row["endpoint_name"]: row["numeric_value"] for row in result.observations.to_dict("records")}
    assert values == {"LB_ALT_BLOOD": 0.45, "LB_AST_BLOOD": 1.77}
    drops = [drop for drop in _stage(result).drops if drop.reason == "data_decision:TCODE-001"]
    assert [(drop.count, dict(drop.by_study)) for drop in drops] == [(1, {"S": 1})]
    decisions = _decisions(METHOD_DROP)
    method = mislabelled["method"]
    assert decisions.resolve("S", "blood_chemistry", "SERUM", ("PLASMA", "SERUM"),
                             endpoint="LB_ALT_BLOOD", method=method).outcome == "removed"
    assert decisions.resolve("T", "blood_chemistry", "SERUM", ("PLASMA", "SERUM"),
                             endpoint="LB_ALT_BLOOD", method=method).outcome == "source"
    assert decisions.resolve("S", "blood_chemistry", "SERUM", ("PLASMA", "SERUM"),
                             endpoint="LB_ALT_BLOOD", method=None).outcome == "source"


def test_receipt_identity_follows_the_decision_file(make_connection) -> None:
    lab = _lab("S", "SERUM")
    first = _run(make_connection, lab, _decisions())
    changed = data_decisions_from_mapping(_document(), md5="e" * 32)
    second = _run(make_connection, lab, changed)
    assert first.receipt["data_decisions"] != second.receipt["data_decisions"]
    assert first.metadata["data_decisions"] == first.receipt["data_decisions"]


def test_warning_names_a_source_that_was_not_reviewed(make_connection) -> None:
    lab = _lab("S", "SERUM")
    reviewed = _run(make_connection, lab, _decisions())
    other = _run(make_connection, lab, _decisions(), source_identity="dvc-md5:" + "f" * 32)
    assert not [w for w in _stage(reviewed).warnings if w.code == "data_decisions_source_not_reviewed"]
    assert [w.code for w in _stage(other).warnings] == ["data_decisions_source_not_reviewed"]


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ({"finding": "SPEC-999", "action": "exclude_panel", "study_id": "S", "specimen_panel": "blood_chemistry"},
         "unknown finding"),
        ({"finding": "SPEC-004", "action": "exclude_panel", "study_id": "S", "specimen_panel": "no_panel"},
         "unknown specimen panel"),
        ({"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S", "specimen_panel": "haematology",
          "value": "SERUM"}, "not a class of"),
        ({"finding": "SPEC-004", "action": "exclude_panel", "study_id": "S", "specimen_panel": "blood_chemistry",
          "value": "SERUM"}, "unsupported fields"),
        ({"finding": "SPEC-005", "action": "drop_rows", "study_id": "S", "specimen_panel": "blood_chemistry",
          "specimens": []}, "specimens"),
        ({**METHOD_DROP, "endpoint": "LB_NOPE"}, "unknown endpoint"),
        ({**METHOD_DROP, "endpoint": "LB_HCT_BLOOD"}, "is not the specimen panel"),
        ({**METHOD_DROP, "method_contains": []}, "method_contains"),
    ],
)
def test_malformed_decisions_are_rejected(decision: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _decisions(decision)


def test_conflicting_decisions_for_one_cell_are_rejected() -> None:
    exclude = {"finding": "SPEC-004", "action": "exclude_panel", "study_id": "S", "specimen_panel": "blood_chemistry"}
    impute = {"finding": "SPEC-001", "action": "impute_specimen", "study_id": "S",
              "specimen_panel": "blood_chemistry", "value": "SERUM"}
    with pytest.raises(ValueError, match="conflicting decisions"):
        _decisions(exclude, impute)
    with pytest.raises(ValueError, match="conflicting decisions"):
        _decisions(exclude, deepcopy(exclude))
    with pytest.raises(ValueError, match="conflicting decisions"):
        _decisions(METHOD_DROP, deepcopy(METHOD_DROP))
    _decisions(METHOD_DROP, {**METHOD_DROP, "endpoint": "LB_AST_BLOOD", "method_contains": ["alanine"]})


def test_a_query_specimen_subset_narrows_the_panel(make_connection) -> None:
    # Failure modes: a row of a class outside the query subset is extracted; a
    # subset that names a panel without a user choice, an unknown class, or an
    # empty list is accepted; the subset does not change the observation
    # fingerprint; replacing the endpoint selection drops the subset.
    from vcgen_query import CohortQuery, EndpointObservations, QueryError
    from vcgen_query.fingerprint import fingerprint_observation_contract

    plasma = {"study_id": "S", "subject_id": "1", "test_code": "ALT", "test_name": "ALANINE AMINOTRANSFERASE",
              "unit": "µkat/L", "day": 14, "value": 0.5, "specimen": "PLASMA", "sequence": 3}
    con = make_connection([{"study_id": "S", "subject_id": "1", "sex": "M"}], lab=_lab("S", "SERUM", extra=(plasma,)))
    subject_query = parse_subject_query({
        "contract": "vcgen.subject_query.v1", "query_id": "subset",
        "predicate": {"field": {"kind": "send_column", "domain": "DM", "column": "SEX", "value_type": "string"},
                      "operator": "eq", "value": "M"},
    })
    narrow = CohortQuery.compose(subject_query, ["LB_ALT_BLOOD", "LB_HCT_BLOOD"], specimens={"blood_chemistry": ["SERUM"]})
    result = run_query(con, narrow, source_data_identity=REVIEWED, data_decisions=_decisions())
    alt = result.observations.loc[result.observations["endpoint_name"] == "LB_ALT_BLOOD"]
    assert list(alt["specimen"]) == ["SERUM"]
    broad = CohortQuery.compose(subject_query, ["LB_ALT_BLOOD", "LB_HCT_BLOOD"])
    assert fingerprint_observation_contract(narrow.endpoint_stage) != fingerprint_observation_contract(broad.endpoint_stage)
    assert narrow.with_requested_endpoints(["LB_ALT_BLOOD"]).endpoint_stage.specimens == {"blood_chemistry": ("SERUM",)}
    assert CohortQuery.from_mapping(narrow.to_dict()).endpoint_stage == narrow.endpoint_stage
    for bad in ({"haematology": ["WHOLE_BLOOD"]}, {"blood_chemistry": ["URINE"]}, {"blood_chemistry": []},
                {"no_panel": ["SERUM"]}):
        with pytest.raises(QueryError):
            EndpointObservations(("LB_ALT_BLOOD",), bad)


SCALE = {"finding": "SCALE-001", "action": "exclude_endpoint", "study_id": "S", "specimen_panel": "blood_chemistry",
         "endpoint": "LB_ALT_BLOOD", "rule": "study_scale", "rows": 1, "study_median": 600.0, "centre": 0.6}
READING = {"finding": "HCT-T01", "action": "reinterpret_unit", "study_id": "S", "specimen_panel": "haematology",
           "endpoint": "LB_HCT_BLOOD", "unit": "%", "reading": "1", "exception": "EXC-0001", "rows": 1}


def _curated(*decisions: dict[str, object]) -> dict[str, object]:
    return {
        "contract": "vcgen.data_decisions.v3", "source_snapshot_identity": REVIEWED, "exceptions_sha256": "e" * 64,
        "findings": [
            {"id": "SCALE-001", "title": "Scale", "upstream": "Check the unit.", "evidence": "docs/x.md#scale"},
            {"id": "HCT-T01", "title": "Fraction as %", "upstream": "Check LBSTRESU.", "evidence": "eda/z.md"},
        ],
        "decisions": list(decisions),
    }


def test_curated_actions_change_only_their_study_and_unit(make_connection) -> None:
    subjects = [{"study_id": study, "subject_id": "1", "sex": "M"} for study in ("S", "T")]
    lab = [*_lab("S", "SERUM"), *_lab("T", "SERUM")]
    lab[1] = {**lab[1], "value": 0.44}
    lab[2] = {**lab[2], "value": 0.5}
    con = make_connection(subjects, lab=lab)
    subject_query = parse_subject_query({
        "contract": "vcgen.subject_query.v1", "query_id": "curated",
        "predicate": {"field": {"kind": "send_column", "domain": "DM", "column": "SEX", "value_type": "string"},
                      "operator": "eq", "value": "M"},
    })
    decisions = data_decisions_from_mapping(_curated(SCALE, READING), md5="d" * 32)
    result = run_query(con, CohortQuery.compose(subject_query, ["LB_ALT_BLOOD", "LB_HCT_BLOOD"]),
                           source_data_identity=REVIEWED, data_decisions=decisions)
    records = {(row["study_id"], row["endpoint_name"]): row for row in result.observations.to_dict("records")}
    assert set(records) == {("S", "LB_HCT_BLOOD"), ("T", "LB_ALT_BLOOD"), ("T", "LB_HCT_BLOOD")}
    assert (records["S", "LB_HCT_BLOOD"]["unit"], records["T", "LB_HCT_BLOOD"]["unit"]) == ("1", "%")
    drops = [drop for drop in _stage(result).drops if drop.reason == "data_decision:SCALE-001"]
    assert [(drop.count, dict(drop.by_study)) for drop in drops] == [(1, {"S": 1})]
    details = _stage(result).details["data_decisions"]
    assert details["contract"] == "vcgen.data_decisions.v3"
    assert details["applied"] == {"SCALE-001": {"action": "exclude_endpoint", "rows": 1, "studies": 1},
                                  "HCT-T01": {"action": "reinterpret_unit", "rows": 1, "studies": 1}}
    assert not [w for w in _stage(result).warnings if w.code == "data_decisions_source_not_reviewed"]


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ({**SCALE, "endpoint": "LB_NOPE"}, "unknown endpoint"),
        ({**SCALE, "specimen_panel": "haematology"}, "specimen panel"),
        ({**READING, "reading": "g/L"}, "reading"),
        ({**READING, "unit": "g/L"}, "unit"),
        ({**SCALE, "exception": "EXC-0002"}, "rule or an exception"),
        ({key: value for key, value in SCALE.items() if key != "rule"}, "rule or an exception"),
    ],
)
def test_malformed_curated_decisions_are_rejected(decision: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        data_decisions_from_mapping(_curated(decision), md5="d" * 32)


def test_locked_decisions_must_be_the_locked_bytes(tmp_path) -> None:
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_bytes(catalogue_location()[0].read_bytes())
    path = tmp_path / "data_decisions.yaml"
    path.write_text(yaml.safe_dump(_curated(SCALE, READING), sort_keys=False), encoding="utf-8")
    lock = tmp_path / "lock.yaml"
    outputs = {name: hashlib.md5((tmp_path / name).read_bytes()).hexdigest()
               for name in ("catalogue.json", "data_decisions.yaml")}
    lock.write_text(yaml.safe_dump({"outputs": outputs}), encoding="utf-8")
    locked = load_locked_decisions(path, lock)
    assert locked.removal("S", "LB_ALT_BLOOD", None) is not None and locked.removal("T", "LB_ALT_BLOOD", None) is None
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(QueryError, match="differs from the lock"):
        load_locked_decisions(path, lock)
    with pytest.raises(QueryError, match="missing"):
        load_locked_decisions(tmp_path / "absent.yaml", lock)
    lock.write_text(yaml.safe_dump({"outputs": {"catalogue.json": outputs["catalogue.json"]}}), encoding="utf-8")
    with pytest.raises(QueryError, match="names no data_decisions.yaml"):
        load_locked_decisions(path, lock)
