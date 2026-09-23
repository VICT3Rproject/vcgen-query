"""Offline structural schema checks for all query-contract layers."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from vcgen_query import (
    COHORT_QUERY_CONTRACT,
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
    SUBJECT_PREDICATE_CONTRACT,
    QueryError,
    endpoint_catalogue_identity,
    import_grit42_form_subject_query,
    load_query_mapping,
    schema_dict,
    schema_resources,
)


def _validator(contract: str) -> Draft202012Validator:
    schemas = schema_resources()
    registry = Registry().with_resources(
        (identifier, Resource.from_contents(schema)) for identifier, schema in schemas.items()
    )
    return Draft202012Validator(schema_dict(contract), registry=registry)


def _rule() -> dict[str, object]:
    return {
        "field": {"kind": "derived", "name": "sex", "value_type": "string"},
        "operator": "in",
        "value": ["M", "F"],
    }


def _advanced() -> dict[str, object]:
    return {
        "id": 1,
        "columns": ["sex"],
        "condition_tree": {
            "type": "group",
            "properties": {"conjunction": "AND", "not": False},
            "children1": [
                {
                    "type": "rule",
                    "properties": {
                        "field": "sex",
                        "operator": "in",
                        "value": ["M", "F"],
                        "valueType": ["select"],
                        "valueSrc": ["value"],
                    },
                }
            ],
        },
    }


def test_source_schemas_accept_their_complete_saved_record_envelopes() -> None:
    form = {
        "id": 1,
        "query_type": "form",
        "form_values": {"sex": ["M"], "future_inactive_key": []},
        "columns": {"opaque": True},
        "condition_tree": {"opaque": True},
        "condition_string": "opaque and never executed",
    }
    _validator(GRIT42_FORM_SUBJECT_QUERY_CONTRACT).validate(form)
    _validator(GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT).validate(_advanced())


def test_form_and_advanced_source_schemas_cross_reject() -> None:
    form = {"query_type": "form", "form_values": {"sex": ["M"]}}
    assert list(_validator(GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT).iter_errors(form))
    assert list(_validator(GRIT42_FORM_SUBJECT_QUERY_CONTRACT).iter_errors(_advanced()))


@pytest.mark.parametrize(
    "payload",
    [
        {**_advanced(), "columns": []},
        {
            **_advanced(),
            "condition_tree": {
                "type": "group",
                "properties": {"conjunction": "AND"},
                "children1": [],
            },
        },
    ],
)
def test_advanced_schema_rejects_empty_inventory_or_group(payload: dict[str, object]) -> None:
    assert list(_validator(GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT).iter_errors(payload))


def test_unknown_active_form_selector_fails_closed_in_importer() -> None:
    with pytest.raises(QueryError, match="unsupported active"):
        import_grit42_form_subject_query(
            {"query_type": "form", "form_values": {"future_selector": ["active"]}},
            ["BW"],
        )


def test_import_predicate_and_cross_schema_cohort_composition() -> None:
    provenance = {
        "contract": GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
        "source_contract": GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
        "source_payload_sha256": "a" * 64,
    }
    predicate = {"group": "and", "children": [_rule()]}
    cohort = {
        "contract": COHORT_QUERY_CONTRACT,
        "query_id": "schema-synthetic",
        "requested_endpoints": ["BW"],
        "endpoint_catalogue": endpoint_catalogue_identity(),
        "predicate": predicate,
        "import_provenance": provenance,
    }

    _validator(GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT).validate(provenance)
    _validator(SUBJECT_PREDICATE_CONTRACT).validate(predicate)
    _validator(COHORT_QUERY_CONTRACT).validate(cohort)


@pytest.mark.parametrize(
    "observation_contract",
    [
        {"options": {"future": True}},
        {"endpoints": [{"name": "hct", "domain": "LB", "test_codes": ["HCT"]}]},
        {
            "endpoints": [
                {
                    "name": "HCT",
                    "domain": "LB",
                    "test_codes": ["HCT"],
                    "day_window": {"max": 28.5},
                }
            ]
        },
    ],
)
def test_observation_schema_rejects_removed_or_noncanonical_structure(
    observation_contract: dict[str, object],
) -> None:
    payload = {
        "contract": COHORT_QUERY_CONTRACT,
        "query_id": "invalid-observation",
        "observation_contract": observation_contract,
    }

    assert list(_validator(COHORT_QUERY_CONTRACT).iter_errors(payload))


def test_v4_and_removed_schema_resource_are_rejected() -> None:
    old = {"contract": "vcgen.cohort_query.v4", "query_id": "old"}
    assert list(_validator(COHORT_QUERY_CONTRACT).iter_errors(old))
    with pytest.raises(QueryError):
        schema_dict("vcgen.cohort_query.v4")
    data_root = Path(__file__).resolve().parents[1] / "src" / "vcgen_query" / "data"
    assert not data_root.joinpath("vcgen.cohort_query.v4.schema.json").exists()


def test_every_bundled_yaml_declares_v5_and_loads_through_the_schema() -> None:
    data_root = Path(__file__).resolve().parents[1] / "src" / "vcgen_query" / "data"
    paths = [
        *data_root.joinpath("examples").glob("*.yaml"),
        *data_root.joinpath("queries").glob("*.yaml"),
    ]

    assert paths
    validator = _validator(COHORT_QUERY_CONTRACT)
    for path in paths:
        text = path.read_text()
        assert "vcgen.cohort_query.v5.schema.json" in text.splitlines()[0]
        assert "contract: vcgen.cohort_query.v5" in text
        validator.validate(load_query_mapping(path))


def test_bundled_v5_yaml_has_no_source_endpoint_definitions() -> None:
    data_root = Path(__file__).resolve().parents[1] / "src" / "vcgen_query" / "data"
    paths = [
        *data_root.joinpath("examples").glob("*.yaml"),
        *data_root.joinpath("queries").glob("*.yaml"),
    ]
    forbidden = {
        "observation_contract", "test_codes", "test_names", "source_units",
        "specimens", "target_unit", "specimen_match",
    }
    for path in paths:
        payload = load_query_mapping(path)
        assert not (forbidden & set(payload)), path
        assert payload["requested_endpoints"]
