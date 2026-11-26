"""Route aliases: one canonical route selects every reviewed spelling of its mechanism.

Failure modes recorded before implementation. The E2E evidence is the
population count per canonical route on the real archive (``tmp/route_aliases/``);
these checks cover the cases that a count cannot identify:

1. A spelling variant (case or whitespace) of a listed pair does not map, so its
   subjects drop out of the route.
2. A pair that is not listed (an unknown method, or a reviewed unmapped pair)
   maps by its route alone, so another mechanism enters the cohort.
3. A missing EXMETHOD acts as a wildcard, or a missing EXROUTE gets a route.
4. A subject with several EX rows matches only on one row, or matches a route
   that none of its rows has.
5. The population choices and the filter disagree: a choice selects no subject,
   or a raw value is offered.
6. The alias table changes and the predicate fingerprint does not, so a cached
   cohort is reused with other semantics.
7. The loader accepts a table that maps one pair twice or lists a value that is
   not in normalized form.
8. A route value that is not a canonical route (a raw spelling such as ORAL) is
   accepted and silently selects no subject, or a negation of it silently
   selects everyone. A spelling of a canonical route that differs only in case
   or spacing is refused, or a filter that is not a value match (contains,
   is_empty) is refused.
9. The review inventory reads EX only, so a TS ROUTE spelling that the route
   field resolves for a study without EX rows is not listed and cannot be
   reviewed.

Failure modes of the TS ROUTE review (written before the change):

10. The inventory adds EX and TS subjects in one count, so a reviewer cannot
    see whether a pair comes from EX rows or from a TS ROUTE value.
11. A reviewed TS spelling of a canonical route (INTRAVENOUS DRIP) does not
    select that route for a subject of a study without EX rows.
12. A TS value that is not a route (NO TREATMENT), that names several routes
    (ORAL GAVAGE, INHALATION), or that names another mechanism (ORAL FEEDING)
    selects a route, or the inventory does not mark it as reviewed.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import run_query
from vcgen_query import CohortQuery, QueryError, parse_subject_query
from vcgen_query.fingerprint import predicate_semantic_json, predicate_semantic_payload
from vcgen_query.population import population_vocabulary
from vcgen_query.route_aliases import (
    load_route_aliases,
    route_alias_inventory,
    route_aliases_from_mapping,
)


def _interval(study: str, subject: str, route: str | None, method: str | None, sequence: int = 1) -> dict:
    return {"study_id": study, "subject_id": subject, "usubjid": f"{study}-{subject}", "exseq": str(sequence),
            "exroute": route, "exmethod": method, "exstdy": 1, "exendy": 28}


CASES = [
    # (study, subject, EX rows as (route, method))
    ("A", "1", [("ORAL GAVAGE", None)]),
    ("A", "2", [("Oral  Gavage ", "gavage")]),
    ("A", "3", [("Oral", "GAVAGE")]),
    ("B", "1", [("ORAL", None)]),
    ("B", "2", [("ORAL", "DIET")]),
    ("B", "3", [("INTRAVENOUS BOLUS", "INFUSION")]),
    ("C", "1", [(None, "GAVAGE")]),
    ("C", "2", [("SUBCUTANEOUS", "INJECTION"), ("Intravenous", "Bolus")]),
]


def _connection(make_connection):
    subjects = [{"study_id": study, "subject_id": subject} for study, subject, _ in CASES]
    intervals = [
        _interval(study, subject, route, method, sequence)
        for study, subject, rows in CASES
        for sequence, (route, method) in enumerate(rows, start=1)
    ]
    return make_connection(subjects, exposure_intervals=intervals)


def _query(route: str) -> dict:
    return {"contract": "vcgen.subject_query.v1", "query_id": "route",
            "predicate": {"field": {"kind": "derived", "name": "route", "value_type": "string"},
                          "operator": "eq", "value": route}}


def _members(con, route: str) -> set[str]:
    result = run_query(con, CohortQuery.compose(parse_subject_query(_query(route)), ["BW_BW"]))
    return {row["subject_id"] for row in result.subjects.to_dict("records")}


def test_a_canonical_route_selects_its_spellings_and_nothing_else(make_connection) -> None:
    con = _connection(make_connection)

    # An oral route without a method is gavage by review decision; another method is not.
    assert _members(con, "ORAL GAVAGE") == {"A-1", "A-2", "A-3", "B-1"}
    assert _members(con, "SUBCUTANEOUS") == {"C-2"}
    assert _members(con, "INTRAVENOUS BOLUS") == {"C-2"}
    assert _members(con, "INTRAVENOUS INFUSION") == set()
    # A spelling of a canonical route is the route.
    assert _members(con, " oral  gavage ") == _members(con, "ORAL GAVAGE")

    vocabulary = population_vocabulary(con)
    assert vocabulary["routes"] == ["ORAL GAVAGE", "INTRAVENOUS BOLUS", "SUBCUTANEOUS"]
    for route in vocabulary["routes"]:
        assert _members(con, route)

    inventory = {(row["exroute"], row["exmethod"]): row for row in route_alias_inventory(con)}
    assert inventory[("ORAL GAVAGE", "GAVAGE")]["route"] == "ORAL GAVAGE"
    assert inventory[("ORAL", "DIET")]["status"] == "unknown"
    assert inventory[("INTRAVENOUS BOLUS", "INFUSION")]["status"] == "unmapped"
    assert inventory[(None, "GAVAGE")]["status"] == "missing_route"
    assert inventory[("ORAL", "DIET")]["ex_subjects"] == 1
    assert inventory[("ORAL", "DIET")]["ts_subjects"] == 0


def test_the_inventory_lists_an_unknown_ts_route_of_a_study_without_ex_rows(make_connection) -> None:
    """The inventory reads EX only, so an unknown TS ROUTE spelling is not listed."""

    con = make_connection(
        [{"study_id": "D", "subject_id": "1"}],
        exposure_intervals=[],
        study_metadata={"D": {"ROUTE": ["Intravesical"]}},
    )

    inventory = {(row["exroute"], row["exmethod"]): row for row in route_alias_inventory(con)}
    assert inventory[("INTRAVESICAL", None)]["status"] == "unknown"
    assert (inventory[("INTRAVESICAL", None)]["ex_subjects"], inventory[("INTRAVESICAL", None)]["ts_subjects"]) == (0, 1)


def test_a_reviewed_ts_route_selects_its_canonical_route_and_the_others_select_none(make_connection) -> None:
    routes = {"E": "Intravenous drip", "F": "NO TREATMENT", "G": "ORAL GAVAGE, INHALATION", "H": "Oral feeding"}
    con = make_connection([{"study_id": study, "subject_id": "1"} for study in routes], exposure_intervals=[],
                          study_metadata={study: {"ROUTE": route} for study, route in routes.items()})

    assert _members(con, "INTRAVENOUS INFUSION") == {"E-1"}
    assert _members(con, "ORAL GAVAGE") == set()
    assert population_vocabulary(con)["routes"] == ["INTRAVENOUS INFUSION"]
    inventory = {row["exroute"]: row for row in route_alias_inventory(con)}
    assert {route: row["status"] for route, row in inventory.items()} == {
        "INTRAVENOUS DRIP": "mapped", "NO TREATMENT": "unmapped", "ORAL GAVAGE, INHALATION": "unmapped",
        "ORAL FEEDING": "unmapped"}
    assert all((row["ex_subjects"], row["ts_subjects"], row["ts_studies"]) == (0, 1, 1) for row in inventory.values())


def test_the_table_is_part_of_the_route_predicate_fingerprint() -> None:
    route = parse_subject_query(_query("ORAL GAVAGE")).predicate
    species = parse_subject_query({**_query("RAT"), "predicate": {
        "field": {"kind": "derived", "name": "species", "value_type": "string"}, "operator": "eq", "value": "RAT"}}).predicate

    assert predicate_semantic_payload(route)["route_aliases"] == load_route_aliases().identity()
    assert load_route_aliases().identity()["sha256"] in predicate_semantic_json(route)
    assert "route_aliases" not in predicate_semantic_payload(species)


def test_the_loader_rejects_an_ambiguous_or_unnormalized_table() -> None:
    import yaml
    from importlib.resources import files

    document = yaml.safe_load(files("vcgen_query").joinpath("data", "route_aliases.ex.v1.yaml").read_bytes())
    route_aliases_from_mapping(document, sha256="a" * 64)

    twice = deepcopy(document)
    twice["routes"][1]["sources"].append(deepcopy(twice["routes"][0]["sources"][0]))
    with pytest.raises(RuntimeError, match="more than once"):
        route_aliases_from_mapping(twice, sha256="a" * 64)

    mapped_and_unmapped = deepcopy(document)
    mapped_and_unmapped["unmapped"].append({**deepcopy(document["unmapped"][0]), "exroute": "ORAL GAVAGE",
                                            "exmethod": None})
    with pytest.raises(RuntimeError, match="more than once"):
        route_aliases_from_mapping(mapped_and_unmapped, sha256="a" * 64)

    lower = deepcopy(document)
    lower["routes"][0]["sources"][0]["exroute"] = "Oral Gavage"
    with pytest.raises(RuntimeError, match="normalized"):
        route_aliases_from_mapping(lower, sha256="a" * 64)


@pytest.mark.parametrize("operator, value", [
    ("eq", "ORAL"), ("ne", "ORAL"), ("in", ["ORAL GAVAGE", "ORAL"]), ("not_in", ["ORAL"]),
])
def test_a_route_that_is_not_canonical_is_refused_not_matched_to_nothing(operator, value) -> None:
    query = _query("ORAL GAVAGE")
    query["predicate"] = {**query["predicate"], "operator": operator, "value": value}

    with pytest.raises(QueryError, match="ORAL.*not a canonical route"):
        parse_subject_query(query)


def test_other_route_filters_are_not_value_matches() -> None:
    for operator, value in (("contains", "GAVAGE"), ("starts_with", "ORAL"), ("is_not_empty", None)):
        query = _query("ORAL GAVAGE")
        query["predicate"] = {**query["predicate"], "operator": operator}
        if value is None:
            query["predicate"].pop("value")
        else:
            query["predicate"]["value"] = value
        parse_subject_query(query)


def test_a_grit42_form_with_a_raw_route_is_refused_on_import() -> None:
    from vcgen_query import import_grit42_subject_query

    def form(route: str) -> dict:
        return {"id": "form", "query_type": "form", "form_values": {"route": [route]}}

    import_grit42_subject_query(form("ORAL GAVAGE"))
    with pytest.raises(QueryError, match="not a canonical route"):
        import_grit42_subject_query(form("ORAL"))
