"""Synthetic collaborator payload tests for the Grit42 import compiler."""

from __future__ import annotations

import pandas as pd

import pytest

from vcgen_query import (
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    QueryError,
    compare_membership,
    fingerprint_predicate,
    import_grit42_advanced_subject_query,
    import_grit42_form_subject_query,
    import_grit42_subject_query,
)


def _advanced_tree(*, field: str = "DM.SEX", operator: str = "equal", value=None):
    return {
        "columns": [{"field": field}],
        "condition_tree": {
            "type": "group",
            "properties": {"conjunction": "AND", "not": False},
            "children1": [
                {
                    "type": "rule",
                    "properties": {
                        "field": field,
                        "operator": operator,
                        "value": ["M"] if value is None else value,
                        "valueType": ["select"],
                    },
                }
            ],
        },
    }


def _saved_advanced_query_shape() -> dict[str, object]:
    """Synthetic saved advanced-query payload returned by Grit42."""

    return {
        "id": 10321464,
        "columns": {
            "DM": ["species"],
            "TS": ["tsparmcd", "tsval"],
            "BW": ["studyid"],
        },
        "condition_tree": {
            "type": "group",
            "properties": {"conjunction": "AND", "not": False},
            "children1": [
                {
                    "type": "rule",
                    "properties": {
                        "field": "send_domain_dm.species",
                        "operator": "select_equals",
                        "value": ["RAT"],
                        "valueType": ["select"],
                        "valueSrc": ["value"],
                    },
                },
                {
                    "type": "rule",
                    "properties": {
                        "field": "send_domain_ts.tsval",
                        "operator": "like",
                        "value": ["Y"],
                        "valueType": ["text"],
                        "valueSrc": ["value"],
                    },
                },
                {
                    "type": "rule",
                    "properties": {
                        "field": "send_domain_bw.studyid",
                        "operator": "is_not_empty",
                        "value": [],
                        "valueType": [],
                        "valueSrc": [],
                    },
                },
            ],
        },
    }


def _saved_simple_query_shape() -> dict[str, object]:
    """Synthetic saved simple-query payload returned by Grit42."""

    return {
        "id": 10326436,
        "query_type": "form",
        "form_values": {
            "species": ["RAT"],
            "strain": ["WISTAR HAN"],
            "sstyp": ["REPEAT DOSE TOXICITY"],
            "route": ["ORAL GAVAGE"],
            "trtvcat": [],
            "ecg_init_min": None,
        },
        # Grit42 also includes generated columns and condition_tree, but the
        # fixed form_values are authoritative for this query type.
        "columns": {"DM": ["species", "strain"]},
        "condition_tree": {
            "type": "group",
            "properties": {"conjunction": "AND", "not": False},
            "children1": [],
        },
    }


def test_saved_simple_query_dispatches_to_form_importer() -> None:
    payload = _saved_simple_query_shape()
    query = import_grit42_subject_query(payload)

    assert query.query_id == "grit42:subject:10326436"
    assert query.import_provenance is not None
    assert query.import_provenance.source_contract == GRIT42_FORM_SUBJECT_QUERY_CONTRACT
    children = query.to_canonical_dict()["predicate"]["children"]
    assert {child["field"]["name"] for child in children} == {
        "species",
        "strain",
        "study_type",
        "route",
    }

    changed_payload = {**payload, "name": "changed saved record metadata"}
    changed_query = import_grit42_subject_query(changed_payload)
    assert changed_query.import_provenance is not None
    assert (
        changed_query.import_provenance.source_payload_sha256
        != query.import_provenance.source_payload_sha256
    )


def test_saved_advanced_query_payload_imports_to_canonical_predicate() -> None:
    query = import_grit42_subject_query(_saved_advanced_query_shape())

    assert query.query_id == "grit42:subject:10321464"
    children = query.to_canonical_dict()["predicate"]["children"]

    assert {
        (child["field"]["domain"], child["field"]["column"], child["operator"])
        for child in children
    } == {
        ("BW", "STUDYID", "is_not_empty"),
        ("DM", "SPECIES", "eq"),
        ("TS", "TSVAL", "contains"),
    }


def test_saved_advanced_query_like_wildcards_fail_closed() -> None:
    payload = _saved_advanced_query_shape()
    payload["condition_tree"]["children1"][1]["properties"]["value"] = ["Y%"]

    with pytest.raises(QueryError, match="like wildcard patterns are unsupported"):
        import_grit42_subject_query(payload)


def test_saved_advanced_query_maps_not_like_to_not_contains() -> None:
    payload = _advanced_tree(field="LB.LBMETHOD", operator="not_like", value=["XYZ"])
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["text"]

    query = import_grit42_subject_query(payload)

    predicate = query.to_canonical_dict()["predicate"]["children"][0]
    assert predicate["operator"] == "not_contains"
    assert predicate["value"] == "XYZ"


def test_saved_advanced_query_unwraps_observed_multiselect_values() -> None:
    payload = _advanced_tree(operator="select_any_in", value=[["F", "M"]])
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["multiselect"]

    query = import_grit42_subject_query(payload)

    predicate = query.to_canonical_dict()["predicate"]["children"][0]
    assert predicate["operator"] == "in"
    assert predicate["value"] == ["F", "M"]


def test_saved_advanced_query_rejects_empty_multiselect_wrapper() -> None:
    payload = _advanced_tree(operator="select_any_in", value=[[]])
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["multiselect"]

    with pytest.raises(QueryError, match="must not be empty"):
        import_grit42_subject_query(payload)


def test_saved_advanced_query_accepts_repeated_range_metadata() -> None:
    payload = _advanced_tree(
        field="LB.LBSTRESN",
        operator="between",
        value=["1.640942", "1.870901"],
    )
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["number", "number"]
    rule["valueSrc"] = ["value", "value"]

    query = import_grit42_subject_query(payload)

    predicate = query.to_canonical_dict()["predicate"]["children"][0]
    assert predicate["operator"] == "between"
    assert predicate["value"] == [1.640942, 1.870901]


def test_saved_advanced_query_normalizes_observed_minute_timestamp() -> None:
    payload = _advanced_tree(
        field="DM.RFSTDTC",
        operator="equal",
        value=["2020-06-16T04:00"],
    )
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["datetime"]

    query = import_grit42_subject_query(payload)

    predicate = query.to_canonical_dict()["predicate"]["children"][0]
    assert predicate["value"] == "2020-06-16T04:00:00"


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("valueType", ["number", "text"], "valueType must identify one type"),
        ("valueSrc", ["value", "field"], "valueSrc must identify one source"),
    ],
)
def test_saved_advanced_query_rejects_conflicting_range_metadata(
    key: str, value: list[str], error: str
) -> None:
    payload = _advanced_tree(
        field="LB.LBSTRESN",
        operator="between",
        value=["1.640942", "1.870901"],
    )
    rule = payload["condition_tree"]["children1"][0]["properties"]
    rule["valueType"] = ["number", "number"]
    rule["valueSrc"] = ["value", "value"]
    rule[key] = value

    with pytest.raises(QueryError, match=error):
        import_grit42_subject_query(payload)


def _advanced_tree_with_field_absent_from_inventory() -> dict[str, object]:
    payload = _advanced_tree()
    payload["columns"] = [{"field": "DM.SPECIES"}]
    return payload


def test_advanced_grit42_tree_imports_to_a_canonical_predicate_and_retains_raw_hash() -> None:
    query = import_grit42_subject_query(_advanced_tree())

    predicate = query.to_canonical_dict()["predicate"]
    assert predicate["group"] == "and"
    assert predicate["children"][0]["field"] == {
        "kind": "send_column",
        "domain": "DM",
        "column": "SEX",
        "value_type": "string",
    }
    assert predicate["children"][0]["operator"] == "eq"
    assert predicate["children"][0]["value"] == "M"
    assert query.import_provenance is not None
    assert query.import_provenance.source_contract == GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT
    assert query.source_identity is None


def test_advanced_import_rejects_inventory_type_mismatch_and_nonliteral_values() -> None:
    mismatched_type = _advanced_tree()
    mismatched_type["columns"] = [{"field": "DM.SEX", "valueType": ["number"]}]
    with pytest.raises(QueryError, match="does not match columns inventory"):
        import_grit42_subject_query(mismatched_type)

    field_reference = _advanced_tree()
    rule = field_reference["condition_tree"]["children1"][0]["properties"]
    rule["valueSrc"] = ["field"]
    with pytest.raises(QueryError, match="valueSrc must be 'value'"):
        import_grit42_subject_query(field_reference)


def test_untrusted_condition_string_never_changes_imported_membership_predicate() -> None:
    safe = import_grit42_subject_query(_advanced_tree())
    hostile_payload = _advanced_tree()
    hostile_payload["condition_string"] = "1 = 1; DROP TABLE dm"
    hostile = import_grit42_subject_query(hostile_payload)

    assert hostile.to_canonical_dict()["predicate"] == safe.to_canonical_dict()["predicate"]
    assert hostile.import_provenance is not None
    assert safe.import_provenance is not None
    assert (
        hostile.import_provenance.source_payload_sha256
        != safe.import_provenance.source_payload_sha256
    )


@pytest.mark.parametrize(
    "payload",
    [
        _advanced_tree(field="DM.SEX; DROP TABLE dm"),
        _advanced_tree(operator="sql"),
        {
            "columns": [{"field": "DM.SEX"}],
            "condition_tree": {
                "type": "group",
                "properties": {"conjunction": "AND"},
                "children1": [],
            },
        },
        {
            "columns": [],
            "condition_tree": {
                "type": "rule",
                "properties": {"field": "DM.SEX", "operator": "equal", "value": ["M"]},
            },
        },
        _advanced_tree_with_field_absent_from_inventory(),
    ],
)
def test_malformed_advanced_grit42_trees_fail_closed(payload) -> None:
    with pytest.raises(QueryError):
        import_grit42_subject_query(payload)


def test_form_values_imports_to_derived_predicates() -> None:
    query = import_grit42_form_subject_query(
        {
            "query_type": "form",
            "form_values": {
                "sex": ["M", "F"],
                "dose_duration_min": "28",
                "study_start_year_max": "2025",
            },
        }
    )
    children = query.to_canonical_dict()["predicate"]["children"]

    assert {child["field"]["name"] for child in children} == {
        "sex",
        "dose_duration_days",
        "study_start_year",
    }
    assert all(child["field"]["kind"] == "derived" for child in children)


def test_form_values_reject_unknown_or_unsupported_selectors() -> None:
    with pytest.raises(QueryError):
        import_grit42_form_subject_query(
            {"query_type": "form", "form_values": {"condition_string": "SEX = 'M'"}}
        )
    with pytest.raises(QueryError):
        import_grit42_form_subject_query({"query_type": "form", "form_values": {"lbtest": ["HCT"]}})


def test_equivalent_form_and_advanced_sources_share_predicate_not_import_provenance() -> None:
    form = import_grit42_form_subject_query(
        {"id": 1, "query_type": "form", "form_values": {"species": ["RAT"]}}
    )
    advanced_payload = _advanced_tree(field="species", operator="in", value=["RAT"])
    advanced_payload["id"] = 2
    advanced = import_grit42_advanced_subject_query(advanced_payload)

    assert form.predicate is not None
    assert advanced.predicate is not None
    assert form.predicate.to_canonical_dict() == advanced.predicate.to_canonical_dict()
    assert fingerprint_predicate(form.predicate) == fingerprint_predicate(advanced.predicate)
    assert form.import_provenance is not None
    assert advanced.import_provenance is not None
    assert form.import_provenance.source_contract != advanced.import_provenance.source_contract
    assert (
        form.import_provenance.source_payload_sha256
        != advanced.import_provenance.source_payload_sha256
    )


def test_compare_membership_reports_deterministic_missing_and_extra_subject_keys() -> None:
    comparison = compare_membership(
        [("S2", "S2-2"), ("S1", "S1-1")],
        [("S1", "S1-1"), ("S3", "S3-3")],
    )

    assert comparison["matches"] is False
    assert comparison["missing_keys"] == (("S3", "S3-3"),)
    assert comparison["extra_keys"] == (("S2", "S2-2"),)
    assert comparison["local_count"] == comparison["reference_count"] == 2
    assert comparison["local_hash"] != comparison["reference_hash"]


def test_compare_membership_reads_dataframe_rows_not_column_labels() -> None:
    local = pd.DataFrame([{"study_id": "S", "subject_id": "1"}])
    reference = pd.DataFrame([{"STUDYID": "S", "USUBJID": "1"}])
    assert compare_membership(local, reference)["matches"] is True

    with pytest.raises(QueryError, match="must contain STUDYID/USUBJID"):
        compare_membership(local, pd.DataFrame([{"wrong": "S"}]))
