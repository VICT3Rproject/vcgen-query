"""Behavioral tests for the v3 predicate query contract."""

from __future__ import annotations

import math

import pytest
import vcgen_query

from vcgen_query import (
    COHORT_QUERY_CONTRACT,
    DayWindow,
    DerivedField,
    EndpointSpec,
    ObservationContract,
    PreStudyEndpointField,
    PredicateGroup,
    PredicateRule,
    Query,
    QueryError,
    SendColumnField,
    fingerprint_observation_contract,
    fingerprint_predicate,
    parse_query,
)


def _field(
    *,
    domain: str = "DM",
    column: str = "SEX",
    value_type: str = "string",
) -> dict[str, str]:
    return {
        "kind": "send_column",
        "domain": domain,
        "column": column,
        "value_type": value_type,
    }


def _rule(
    operator: str = "eq",
    value: object = "M",
    *,
    field: dict[str, str] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "field": field or _field(),
        "operator": operator,
    }
    if value is not None or operator not in {"is_empty", "is_not_empty"}:
        rule["value"] = value
    return rule


def _query(predicate: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract": COHORT_QUERY_CONTRACT,
        "query_id": "synthetic-v3",
        **extra,
    }
    if predicate is not None:
        payload["predicate"] = predicate
    return payload


def test_contract_id_and_absent_predicate_are_canonical_all_subjects() -> None:
    query = parse_query(_query())

    assert query.contract == "vcgen.cohort_query.v4"
    assert query.predicate is None
    assert query.import_provenance is None
    assert "predicate" not in query.to_canonical_dict()
    assert "import_provenance" not in query.to_canonical_dict()


def test_manual_conversion_types_are_not_public() -> None:
    assert not hasattr(vcgen_query, "UnitConversion")
    assert not hasattr(vcgen_query, "UnitConversionRule")


def test_direct_endpoint_construction_enforces_and_normalizes_parser_invariants() -> None:
    endpoint = EndpointSpec(
        name="HCT",
        domain="LB",
        test_codes=("ｈｃｔ", "HCT"),
        test_names=(" hematocrit ",),
        source_units=("g/L", "G/L"),
        specimens=(" blood ",),
        specimen_match="contains",
        sexes=("m", "M"),
        day_window=DayWindow(1, 28),
    )

    assert endpoint.test_codes == ("HCT",)
    assert endpoint.test_names == ("HEMATOCRIT",)
    assert endpoint.source_units == ("G/L", "g/L")
    assert endpoint.specimens == ("BLOOD",)
    assert endpoint.sexes == ("M",)

    with pytest.raises(QueryError, match="specimen_match"):
        EndpointSpec("HCT", "LB", ("HCT",), specimen_match="substring")
    with pytest.raises(QueryError, match="non-empty"):
        EndpointSpec("HCT", "LB", ())


def test_valid_direct_and_parsed_observation_contracts_are_identical() -> None:
    direct = ObservationContract(
        (
            EndpointSpec(
                "LB_HCT",
                "LB",
                ("ｈｃｔ",),
                (" hematocrit ",),
                ("g/L", "g/dL"),
                (" blood ",),
                "exact",
                ("m",),
                DayWindow(1, 28),
                "g/dL",
            ),
        )
    )
    parsed = parse_query(
        _query(
            observation_contract={
                "endpoints": [
                    {
                        "name": "LB_HCT",
                        "domain": "LB",
                        "test_codes": ["ｈｃｔ"],
                        "test_names": [" hematocrit "],
                        "source_units": ["g/L", "g/dL"],
                        "specimens": [" blood "],
                        "sexes": ["m"],
                        "day_window": {"min": 1, "max": 28},
                        "target_unit": "g/dL",
                    }
                ]
            }
        )
    )

    assert parsed.observation_contract == direct
    assert parsed.observation_contract.to_canonical_dict() == direct.to_canonical_dict()


def test_day_window_is_integer_only_inclusive_and_ordered() -> None:
    window = DayWindow(-1, 28)

    assert window.contains(-1)
    assert window.contains(28)
    assert not window.contains(29)
    assert not window.contains(None)
    with pytest.raises(QueryError, match="at least one"):
        DayWindow()
    with pytest.raises(QueryError, match="integers"):
        DayWindow(1.5, 28)
    with pytest.raises(QueryError, match="must not exceed"):
        DayWindow(29, 28)


def test_direct_field_construction_rejects_noncanonical_variants() -> None:
    with pytest.raises(QueryError, match="uppercase SEND identifier"):
        SendColumnField("dm", "SEX", "string")
    with pytest.raises(QueryError, match="unsupported"):
        DerivedField("unknown", "string")
    with pytest.raises(QueryError, match="must be 'number'"):
        PreStudyEndpointField("HCT", value_type="string")


def test_direct_predicate_construction_enforces_operator_and_value_coupling() -> None:
    field = SendColumnField("DM", "SEX", "string")
    rule = PredicateRule(field, "in", ["M", "F", "M"])

    assert rule.value == ("F", "M")
    with pytest.raises(QueryError, match="operator"):
        PredicateRule(field, "equals", "M")
    with pytest.raises(QueryError, match="scalar"):
        PredicateRule(field, "eq", ["M"])
    with pytest.raises(QueryError, match="omitted or null"):
        PredicateRule(field, "is_empty", "")


def test_direct_group_and_query_construction_require_typed_entities() -> None:
    rule = PredicateRule(SendColumnField("DM", "SEX", "string"), "eq", "M")
    group = PredicateGroup("and", [rule, rule])

    assert group.children == (rule,)
    with pytest.raises(QueryError, match="non-empty"):
        PredicateGroup("and", ())
    with pytest.raises(QueryError, match="typed predicates"):
        PredicateGroup("and", ({"operator": "eq"},))
    with pytest.raises(QueryError, match="typed predicate"):
        Query("direct", predicate={"operator": "eq"})
    with pytest.raises(QueryError, match="ObservationContract"):
        Query("direct", observation_contract={})
    with pytest.raises(QueryError, match="unsupported contract"):
        Query("direct", contract="vcgen.cohort_query.v3")


def test_observation_container_rejects_invalid_members() -> None:
    with pytest.raises(QueryError, match="EndpointSpec"):
        ObservationContract(endpoints=({},))


@pytest.mark.parametrize("name", ["hct", "HCT value", "1_HCT", "STUDY_ID"])
def test_endpoint_names_must_be_non_reserved_uppercase_machine_ids(name: str) -> None:
    with pytest.raises(QueryError):
        EndpointSpec(name, "LB", ("HCT",))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_codes", ("UNKNOWN",)),
        ("test_names", ("N/A",)),
        ("specimens", (" ",)),
        ("sexes", ("NOT AVAILABLE",)),
    ],
)
def test_endpoint_selectors_reject_missing_categories(field: str, value: tuple[str, ...]) -> None:
    arguments = {"name": "HCT", "domain": "LB", "test_codes": ("HCT",), field: value}
    with pytest.raises(QueryError, match="non-missing category"):
        EndpointSpec(**arguments)


def test_observation_contract_rejects_removed_options_and_duplicate_endpoint_ids() -> None:
    with pytest.raises(QueryError, match="unsupported key"):
        parse_query(_query(observation_contract={"options": {"future": True}}))
    with pytest.raises(QueryError, match="unique"):
        ObservationContract(
            (EndpointSpec("HCT", "LB", ("HCT",)), EndpointSpec("HCT", "LB", ("HGB",)))
        )


def test_target_unit_compiles_all_exact_source_labels() -> None:
    endpoint = EndpointSpec(
        "MASS", "BW", ("BW",), source_units=("g", "G", "kg"), target_unit="g"
    )

    assert endpoint.source_units == ("G", "g", "kg")
    assert [(rule.source_unit, rule.source_ucum, rule.scale, rule.offset) for rule in endpoint.compiled_unit_rules] == [
        ("G", "g", 1.0, 0.0),
        ("g", "g", 1.0, 0.0),
        ("kg", "kg", 1000.0, 0.0),
    ]
    with pytest.raises(QueryError, match="source_units must be non-empty"):
        EndpointSpec("HCT", "LB", ("HCT",), target_unit="g/dL")


@pytest.mark.parametrize(
    ("source", "target", "message"),
    [
        ("S", "s", "unknown unit"),
        ("arbitrary", "1", "unknown unit"),
        ("[pH]", "1", "unknown unit"),
        ("g", "mol", "incompatible"),
        ("g/L", "mol/L", "incompatible"),
        ("s", "g", "incompatible"),
    ],
)
def test_unit_catalogue_rejects_unknown_nonlinear_and_incompatible_units(
    source: str, target: str, message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=(source,), target_unit=target)


def test_unit_labels_are_trimmed_but_not_casefolded_or_rewritten() -> None:
    endpoint = EndpointSpec(
        "VALUE", "LB", ("VALUE",), source_units=(" g ", "G"), target_unit="g"
    )
    assert endpoint.source_units == ("G", "g")
    with pytest.raises(QueryError, match="duplicate exact labels"):
        EndpointSpec(
            "VALUE", "LB", ("VALUE",), source_units=("g", " g "), target_unit="g"
        )
    with pytest.raises(QueryError, match="exact case-sensitive code"):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=("g",), target_unit="G")
    with pytest.raises(QueryError, match="exact case-sensitive code"):
        EndpointSpec("VALUE", "LB", ("VALUE",), source_units=("g",), target_unit=" g ")


def test_legacy_manual_conversion_rules_are_rejected() -> None:
    with pytest.raises(QueryError, match="unsupported key"):
        parse_query(
            _query(
                observation_contract={
                    "endpoints": [
                        {
                            "name": "VALUE",
                            "domain": "LB",
                            "test_codes": ["VALUE"],
                            "source_units": ["g/L"],
                            "unit_conversion": {
                                "target_unit": "g/dL",
                                "rules": [{"source_unit": "g/L", "scale": 0.1}],
                            },
                        }
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"query_id": "missing-contract"},
        {"contract": "vcgen.cohort_query.v3", "query_id": "old-contract"},
    ],
)
def test_native_parser_requires_explicit_v4_contract(payload: dict[str, object]) -> None:
    with pytest.raises(QueryError):
        parse_query(payload)


@pytest.mark.parametrize(
    ("operator", "value", "value_type"),
    [
        ("eq", "M", "string"),
        ("ne", "F", "string"),
        ("in", ["M", "F", "M"], "string"),
        ("not_in", ["F", "M", "F"], "string"),
        ("contains", "IST", "string"),
        ("not_contains", "MOUSE", "string"),
        ("starts_with", "WI", "string"),
        ("not_starts_with", "MO", "string"),
        ("ends_with", "HAN", "string"),
        ("not_ends_with", "MOUSE", "string"),
        ("is_empty", None, "string"),
        ("is_not_empty", None, "string"),
        ("gt", 1, "number"),
        ("gte", 1, "number"),
        ("lt", 2, "number"),
        ("lte", 2, "number"),
        ("between", [1, 2], "number"),
        ("not_between", [1, 2], "number"),
        ("eq", "2020-01-01", "datetime"),
    ],
)
def test_every_canonical_operator_parses_typed_value(
    operator: str, value: object, value_type: str
) -> None:
    field = {
        "string": _field(),
        "number": _field(column="AGE", value_type="number"),
        "datetime": _field(column="RFSTDTC", value_type="datetime"),
    }[value_type]
    parsed = parse_query(_query(_rule(operator, value, field=field)))

    assert parsed.predicate is not None
    canonical = parsed.to_canonical_dict()["predicate"]
    assert canonical["operator"] == operator
    if operator in {"is_empty", "is_not_empty"}:
        assert "value" not in canonical
    else:
        expected = sorted(set(value)) if operator in {"in", "not_in"} else value
        assert canonical["value"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        _query({"group": "and", "children": []}),
        _query({"group": "xor", "children": [_rule()]}),
        _query({"group": "and", "negated": "true", "children": [_rule()]}),
        _query(_rule("unknown", "M")),
        _query(_rule("between", [1], field=_field(column="AGE", value_type="number"))),
        _query(_rule("in", "M")),
        _query(_rule("eq", math.nan, field=_field(column="AGE", value_type="number"))),
        _query(_rule("eq", math.inf, field=_field(column="AGE", value_type="number"))),
        _query(
            _rule(
                "eq",
                "not-a-timestamp",
                field=_field(column="RFSTDTC", value_type="datetime"),
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "send_column",
                    "domain": "DM; DROP TABLE dm",
                    "column": "SEX",
                    "value_type": "string",
                },
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "send_column",
                    "domain": "DM",
                    "column": "SEX; DROP TABLE dm",
                    "value_type": "string",
                },
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={"kind": "derived", "name": "unknown", "value_type": "string"},
            )
        ),
        _query(
            _rule(
                "eq",
                "M",
                field={
                    "kind": "pre_study_endpoint",
                    "endpoint": "UNKNOWN",
                    "value_type": "number",
                },
            )
        ),
    ],
)
def test_malformed_predicates_fail_closed(raw: dict[str, object]) -> None:
    with pytest.raises(QueryError):
        parse_query(raw)


def test_send_column_fields_are_closed_by_the_versioned_grit42_catalogue() -> None:
    assert SendColumnField("LB", "LBSTRESN", "number").value_type == "number"

    with pytest.raises(QueryError, match="not available in grit42 catalogue"):
        SendColumnField("DM", "NOT_A_COLUMN", "string")
    with pytest.raises(QueryError, match="must be 'number'.*LB.LBSTRESN"):
        SendColumnField("LB", "LBSTRESN", "string")


def test_normalization_sorts_commutative_children_and_removes_exact_duplicates() -> None:
    female = _rule("eq", "F")
    male = _rule("eq", "M")
    left = parse_query(_query({"group": "or", "children": [male, female, male]}))
    right = parse_query(_query({"group": "or", "children": [female, male]}))

    assert left.to_canonical_dict()["predicate"] == right.to_canonical_dict()["predicate"]
    assert fingerprint_predicate(left.predicate) == fingerprint_predicate(right.predicate)


def test_predicate_fingerprint_ignores_query_and_import_provenance() -> None:
    predicate = {"group": "and", "children": [_rule("eq", "M")]}
    first = parse_query(_query(predicate, query_id="first"))
    second = parse_query(
        _query(
            predicate,
            query_id="second",
            import_provenance={
                "contract": "grit42.subject_query_import.v1",
                "source_contract": "grit42.form_subject_query.v1",
                "source_payload_sha256": "b" * 64,
            },
            observation_contract={
                "endpoints": [{"name": "BW", "domain": "BW", "test_codes": ["BW"]}]
            },
        )
    )

    assert fingerprint_predicate(first.predicate) == fingerprint_predicate(second.predicate)
    assert first.import_provenance is None
    assert second.import_provenance is not None
    assert parse_query(second.to_canonical_dict()) == second


@pytest.mark.parametrize(
    "provenance",
    [
        {
            "contract": "wrong",
            "source_contract": "grit42.form_subject_query.v1",
            "source_payload_sha256": "a" * 64,
        },
        {
            "contract": "grit42.subject_query_import.v1",
            "source_contract": "wrong",
            "source_payload_sha256": "a" * 64,
        },
        {
            "contract": "grit42.subject_query_import.v1",
            "source_contract": "grit42.form_subject_query.v1",
            "source_payload_sha256": "A" * 64,
        },
    ],
)
def test_import_provenance_fails_closed(provenance: dict[str, str]) -> None:
    with pytest.raises(QueryError):
        parse_query(_query(import_provenance=provenance))


def test_nested_groups_and_negation_round_trip() -> None:
    predicate = {
        "group": "and",
        "children": [
            _rule("eq", "RAT", field=_field(column="SPECIES")),
            {
                "group": "or",
                "negated": True,
                "children": [_rule("eq", "F"), _rule("eq", "M")],
            },
        ],
    }

    parsed = parse_query(_query(predicate))

    canonical = parsed.to_canonical_dict()["predicate"]
    nested = next(child for child in canonical["children"] if child.get("group") == "or")
    assert canonical["group"] == "and"
    assert nested["negated"] is True


def test_observation_fingerprint_includes_compiled_endpoint_conversion() -> None:
    base = {
        "observation_contract": {
            "endpoints": [
                {
                    "name": "HCT",
                    "domain": "LB",
                    "test_codes": ["HCT"],
                    "source_units": ["g/L"],
                    "target_unit": "g/dL",
                }
            ]
        }
    }
    first = parse_query(_query(**base))
    changed = parse_query(
        _query(
            observation_contract={
                "endpoints": [
                    {
                        **base["observation_contract"]["endpoints"][0],
                        "target_unit": "g/L",
                    }
                ]
            }
        )
    )

    assert fingerprint_observation_contract(first) != fingerprint_observation_contract(changed)


def test_observation_fingerprint_is_normalization_stable_and_endpoint_order_sensitive() -> None:
    first = parse_query(
        _query(
            observation_contract={
                "endpoints": [
                    {"name": "LB_HCT", "domain": "LB", "test_codes": ["ｈｃｔ"]},
                    {"name": "BW", "domain": "BW", "test_codes": ["bw"]},
                ]
            }
        )
    )
    equivalent = parse_query(
        _query(
            observation_contract={
                "endpoints": [
                    {"name": "LB_HCT", "domain": "LB", "test_codes": ["HCT", "hct"]},
                    {"name": "BW", "domain": "BW", "test_codes": ["BW"]},
                ]
            }
        )
    )
    reordered = parse_query(
        _query(
            observation_contract={
                "endpoints": [
                    {"name": "BW", "domain": "BW", "test_codes": ["BW"]},
                    {"name": "LB_HCT", "domain": "LB", "test_codes": ["HCT"]},
                ]
            }
        )
    )

    assert fingerprint_observation_contract(first) == fingerprint_observation_contract(equivalent)
    assert fingerprint_observation_contract(first) != fingerprint_observation_contract(reordered)


@pytest.mark.parametrize(
    "endpoint",
    [
        {
            "name": "HCT",
            "domain": "LB",
            "test_codes": ["HCT"],
            "source_units": ["g/dL"],
            "unit_conversion": {
                "target_unit": "g/dL",
                "rules": [{"source_unit": "g/dL", "scale": 2}],
            },
        },
        {"name": "study_id", "domain": "LB", "test_codes": ["HCT"]},
    ],
)
def test_daily_projection_contract_rejects_ambiguous_endpoint_metadata(
    endpoint: dict[str, object],
) -> None:
    with pytest.raises(QueryError):
        parse_query(_query(observation_contract={"endpoints": [endpoint]}))
