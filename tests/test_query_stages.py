"""Grit42 subject-query layer and staged cohort-query execution.

Failure modes that the end-to-end service tests cannot identify:

1. The subject query that Grit42 sends is not kept as written. A changed key
   order, child order, list order or duplicate child breaks the evidence that
   we executed the submitted document.
2. The predicate fingerprint depends on child order, or it changes for an
   unchanged predicate across the v5 -> v6 cutover. Both break the cache and
   membership identities.
3. Explanation node paths use a sorted order, so a dashboard count cannot be
   mapped back to the submitted document.
4. A subject query that contains endpoint selection is accepted. Membership
   and endpoint selection must stay in separate layers.
5. The trace does not mark the subject-query stage as the Grit42 parity
   checkpoint, or its membership differs from the membership that the parity
   tool compares.
6. Stage counts do not chain: a stage input differs from the previous output.
7. A study exclusion changes the Grit42 membership or its hash instead of acting
   after the parity checkpoint, or an excluded subject still reaches subject
   context, observations or the subjects table, or the drops do not explain the
   change in subjects and studies.
8. An exclusion reference or note repeats the study identifier it hides.
9. An endpoint exclusion rewrites the submitted selection without a record, or a
   stored document disagrees with its own record of the removal, or the stage
   order is not enforced.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest
import yaml

from vcgen_query import (
    CohortQuery,
    EndpointExclusionEntry,
    EndpointObservations,
    QueryError,
    StudyExclusionEntry,
    SubjectQuery,
    evaluate_query_membership,
    execute_query,
    fingerprint_predicate,
    parse_cohort_query,
    parse_subject_query,
)

SEX = {"kind": "send_column", "domain": "DM", "column": "SEX", "value_type": "string"}
STRAIN = {"kind": "derived", "name": "strain", "value_type": "string"}


def _document() -> dict:
    return {
        "contract": "vcgen.subject_query.v1",
        "query_id": "stage-order",
        "predicate": {
            "group": "and",
            "children": [
                {"field": STRAIN, "operator": "in", "value": ["WISTAR HAN", "SPRAGUE DAWLEY"]},
                {"field": SEX, "operator": "eq", "value": "M"},
                {"field": SEX, "operator": "eq", "value": "M"},
            ],
        },
    }


def _source(make_connection):
    return make_connection(
        [
            {"study_id": "S1", "subject_id": "1", "sex": "M"},
            {"study_id": "S1", "subject_id": "2", "sex": "F"},
            {"study_id": "S2", "subject_id": "3", "sex": "M", "strain": "CD-1"},
            {"study_id": "S2", "subject_id": "4", "sex": "M"},
            {"study_id": "S2", "subject_id": "5", "sex": "F"},
        ],
        body_weight=[
            {"study_id": "S1", "subject_id": "1", "day": 1, "value": 200.0},
            {"study_id": "S2", "subject_id": "4", "day": 1, "value": 210.0},
        ],
    )


def test_subject_query_serializes_exactly_as_submitted() -> None:
    document = _document()
    query = parse_subject_query(document)

    assert json.dumps(query.to_dict()) == json.dumps(document)
    assert [child for child in query.predicate.children][0].field.name == "strain"


def test_predicate_fingerprint_ignores_child_order_and_is_stable_across_cutover() -> None:
    document = _document()
    reordered = {**document, "predicate": {**document["predicate"], "children": document["predicate"]["children"][::-1]}}

    assert fingerprint_predicate(parse_subject_query(document).predicate) == fingerprint_predicate(
        parse_subject_query(reordered).predicate
    )
    packaged = yaml.safe_load(
        resources.files("vcgen_query").joinpath("data/examples/wistar_han_28d_cohort.yaml").read_text()
    )
    # Recorded from vcgen.cohort_query.v5 before the cutover.
    assert fingerprint_predicate(parse_subject_query(packaged).predicate) == (
        "93ffba22187a004ce8de8b91adf65fdc8db4f652e100d0e31222501556ac517e"
    )


@pytest.mark.parametrize("field", ["requested_endpoints", "endpoint_catalogue"])
def test_subject_query_rejects_endpoint_selection(field: str) -> None:
    with pytest.raises(QueryError):
        parse_subject_query({**_document(), field: ["BW"]})


def test_cohort_query_composes_the_subject_query_with_later_stages() -> None:
    subject = parse_subject_query(_document())
    query = CohortQuery(subject, (EndpointObservations(("BW",)),))

    assert isinstance(query.subject_query, SubjectQuery)
    assert query.requested_endpoints == ("BW",)
    assert parse_cohort_query(query.to_dict()) == query
    assert query.to_dict()["subject_query"] == _document()


def test_trace_marks_the_grit42_checkpoint_and_chains_counts(make_connection) -> None:
    subject = parse_subject_query(_document())
    query = CohortQuery(subject, (EndpointObservations(("BW",)),))

    result = execute_query(_source(make_connection), query)
    stages = [record.to_dict() for record in result.trace]

    assert [stage["id"] for stage in stages] == [
        "source", "subject_query", "subject_context", "endpoint_observations",
    ]
    checkpoint = stages[1]
    assert checkpoint["origin"] == "grit42"
    assert checkpoint["details"]["parity_checkpoint"] == "grit42"
    assert checkpoint["n_in"]["subjects"] == 5
    assert checkpoint["n_out"] == {"subjects": 2, "studies": 2}
    membership = evaluate_query_membership(_source(make_connection), subject)
    assert checkpoint["details"]["membership_hash"] == membership["membership_hash"]
    for before, after in zip(stages, stages[1:]):
        assert after["n_in"] == {key: before["n_out"][key] for key in after["n_in"]}
    assert stages[-1]["n_out"]["observations"] == 2
    assert result.receipt["stages"] == stages


def test_explanation_paths_follow_the_submitted_child_order(make_connection) -> None:
    subject = parse_subject_query(_document())
    result = execute_query(
        _source(make_connection), CohortQuery(subject, (EndpointObservations(("BW",)),))
    )
    nodes = {node["path"]: node["count"] for node in result.trace[1].details["nodes"]}

    # Child 0 is the strain rule (subjects 1, 2, 4, 5) and child 1 is the sex
    # rule (subjects 1, 3, 4). Sorting would put the send_column rule first.
    assert nodes["predicate.children[0]"] == 4
    assert nodes["predicate.children[1]"] == 3
    assert nodes["predicate"] == 2


def test_extraction_accounts_for_every_source_row_and_record_unit(make_connection) -> None:
    # Failure modes: a source row ends in no outcome or in two; a record has no unit status.
    source = make_connection(
        [
            {"study_id": "S1", "subject_id": "1", "sex": "M"},
            {"study_id": "S1", "subject_id": "2", "sex": "M", "strain": "CD-1"},
            {"study_id": "S1", "subject_id": "3", "sex": "M"},
        ],
        body_weight=[
            {"study_id": "S1", "subject_id": "1", "day": 1, "value": 200.0},
            # Matching accepts only catalogue source units, so kg is not an endpoint match.
            {"study_id": "S1", "subject_id": "1", "day": 8, "value": 210.0, "unit": "kg"},
            {"study_id": "S1", "subject_id": "2", "day": 1, "value": 190.0},  # not selected
            {"study_id": "S1", "subject_id": "3", "day": 1, "value": 5.0, "test_code": "XX"},
        ],
    )
    subject = parse_subject_query({**_document(), "predicate": {
        "field": STRAIN, "operator": "eq", "value": "WISTAR HAN"}})

    result = execute_query(source, CohortQuery(subject, (EndpointObservations(("BW",)),)))
    counts = result.trace[-1].details["counts"]

    assert counts["source_rows_read"] == 4
    assert counts["source_rows_read"] == (
        counts["skipped_not_selected"] + counts["source_rows_matched"]
        + counts["source_rows_no_endpoint_match"] + counts["source_rows_sex_not_applicable"]
        + counts["source_rows_outside_day_window"]
    )
    assert counts["skipped_not_selected"] == 1
    assert counts["source_rows_no_endpoint_match"] == 2
    assert counts["record_count"] == counts["records_unit_canonical"] + counts[
        "records_unit_not_canonical"] + counts["records_unit_missing"] + counts[
        "records_without_canonical_unit"]
    assert (counts["records_unit_canonical"], counts["records_unit_not_canonical"]) == (1, 0)
    assert [drop.reason for drop in result.trace[-1].drops] == ["no_observations"]


def _stage_ids(result) -> list[str]:
    return [record.info.id for record in result.trace]


def test_study_exclusion_acts_after_the_grit42_checkpoint(make_connection) -> None:
    subject = parse_subject_query(_document())
    query = CohortQuery.compose(
        subject,
        ["BW"],
        study_exclusions=[StudyExclusionEntry("S2", "study-a", "Source reported a dosing error.")],
    )

    result = execute_query(_source(make_connection), query)
    stages = [record.to_dict() for record in result.trace]

    assert _stage_ids(result) == [
        "source", "subject_query", "study_exclusion", "subject_context", "endpoint_observations",
    ]
    membership = evaluate_query_membership(_source(make_connection), subject)
    assert stages[1]["details"]["membership_hash"] == membership["membership_hash"]
    assert stages[1]["n_out"] == {"subjects": 2, "studies": 2}
    exclusion = stages[2]
    assert (exclusion["n_in"], exclusion["n_out"]) == (
        {"subjects": 2, "studies": 2}, {"subjects": 1, "studies": 1},
    )
    assert {(drop["unit"], drop["count"]) for drop in exclusion["drops"]} == {
        ("subjects", 1), ("studies", 1),
    }
    assert exclusion["details"]["exclusions"] == [
        {"study_id": "S2", "ref": "study-a", "note": "Source reported a dosing error.", "subjects": 1}
    ]
    for before, after in zip(stages, stages[1:]):
        assert after["n_in"] == {key: before["n_out"][key] for key in after["n_in"]}
    assert set(result.subjects["study_id"]) == {"S1"}
    assert set(result.observations["study_id"]) == {"S1"}
    assert stages[3]["details"]["subjects_by_study"] == {"S1": 1}
    assert result.membership.subject_keys == tuple(membership["subject_keys"])
    assert result.metadata["membership_hash"] == membership["membership_hash"]
    assert result.metadata["subject_count"] == 1
    assert result.receipt["subject_count"] == 1 and result.receipt["study_count"] == 1


def test_an_empty_study_exclusion_list_records_the_review_and_removes_nothing(make_connection) -> None:
    query = CohortQuery.compose(
        parse_subject_query(_document()), ["BW"], study_exclusions=[], endpoint_exclusions=[]
    )

    result = execute_query(_source(make_connection), query)
    records = {record.info.id: record for record in result.trace}

    assert records["study_exclusion"].n_in == records["study_exclusion"].n_out
    assert records["study_exclusion"].drops == () and records["endpoint_exclusion"].drops == ()
    assert records["study_exclusion"].params == {"entries_reviewed": 0}
    assert len(result.subjects) == 2


def test_endpoint_exclusion_keeps_the_submitted_selection_and_records_the_removal(
    make_connection,
) -> None:
    query = CohortQuery.compose(
        parse_subject_query(_document()),
        ["BW", "LB_HCT"],
        endpoint_exclusions=[
            EndpointExclusionEntry("LB_HCT", "Duplicate channels cannot be resolved."),
            EndpointExclusionEntry("LB_RBC", "Not requested here."),
        ],
    )

    result = execute_query(_source(make_connection), query)
    record = next(item for item in result.trace if item.info.id == "endpoint_exclusion")

    assert query.submitted_endpoints == ("BW", "LB_HCT")
    assert query.requested_endpoints == ("BW",)
    assert _stage_ids(result)[-2:] == ["endpoint_exclusion", "endpoint_observations"]
    assert [(drop.reason, drop.unit, drop.count) for drop in record.drops] == [
        ("endpoint_excluded", "endpoints", 1)
    ]
    assert record.details["exclusions"] == [
        {"endpoint": "LB_HCT", "note": "Duplicate channels cannot be resolved."}
    ]
    assert record.params["requested_endpoints"] == ["BW", "LB_HCT"]
    assert parse_cohort_query(query.to_dict()) == query


def test_exclusions_fail_closed() -> None:
    subject = parse_subject_query(_document())
    study = StudyExclusionEntry("S2", "study-a", "Reason.")

    with pytest.raises(QueryError, match="every requested endpoint is excluded"):
        CohortQuery.compose(subject, ["LB_HCT"], endpoint_exclusions=[EndpointExclusionEntry("LB_HCT", "x")])
    with pytest.raises(QueryError, match="must not repeat the identifier"):
        StudyExclusionEntry("RAT14", "study-a", "Study rat14 had a dosing error.")
    with pytest.raises(QueryError, match="must not contain the study identifier"):
        StudyExclusionEntry("rat14", "study-rat14", "Dosing error.")
    with pytest.raises(QueryError, match="unique study identifiers"):
        CohortQuery.compose(subject, ["BW"], study_exclusions=[study, StudyExclusionEntry("S2", "study-b", "Again.")])
    with pytest.raises(QueryError, match="unique references"):
        CohortQuery.compose(subject, ["BW"], study_exclusions=[study, StudyExclusionEntry("S3", "study-a", "Other.")])
    with pytest.raises(QueryError, match="non-empty"):
        EndpointExclusionEntry("LB_HCT", "  ")
    with pytest.raises(QueryError):
        EndpointExclusionEntry("NOT_AN_ENDPOINT", "Reason.")

    stored = CohortQuery.compose(
        subject, ["BW", "LB_HCT"], endpoint_exclusions=[EndpointExclusionEntry("LB_HCT", "x")]
    ).to_dict()
    stored["stages"][-1]["requested_endpoints"] = ["BW", "LB_HCT"]
    with pytest.raises(QueryError, match="none that it excludes"):
        parse_cohort_query(stored)
    reordered = CohortQuery.compose(
        subject, ["BW"], study_exclusions=[study], endpoint_exclusions=[]
    ).to_dict()
    reordered["stages"] = [reordered["stages"][1], reordered["stages"][0], reordered["stages"][2]]
    with pytest.raises(QueryError, match="optional study_exclusion"):
        parse_cohort_query(reordered)
def test_a_refused_stage_carries_one_coded_user_description() -> None:
    # Failure modes: a refusal without a code or description reaches the wire; a
    # code outside the enum reaches /metrics as an unbounded label; a refused
    # record still reads as passed.
    from vcgen_query import REFUSAL_CODES, StageInfo, StageRecord, StageRefusal

    info = StageInfo("donor_design", "Donor design", "select", "vcgen", "Choose donors.")
    refusal = StageRefusal("too_few_donor_studies", "Only 1 study qualifies. At least 2 are needed.")
    record = StageRecord(info, {}, {"subjects": 4}, {"subjects": 4}, refusal=refusal)
    payload = record.to_dict()
    assert payload["outcome"] == "refused"
    assert payload["refusal"] == {"code": "too_few_donor_studies",
                                  "description": "Only 1 study qualifies. At least 2 are needed."}
    assert StageRecord(info, {}, {}, {}).to_dict()["outcome"] == "passed"
    assert StageRecord(info, {}, {}, {}).to_dict()["refusal"] is None
    assert "too_few_donor_studies" in REFUSAL_CODES
    with pytest.raises(ValueError, match="refusal code"):
        StageRefusal("not_a_code", "Text.")
    with pytest.raises(ValueError, match="description"):
        StageRefusal("too_few_donor_studies", " ")
