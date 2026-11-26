"""Population vocabulary: the choices a client offers for top-level query fields.

Failure modes recorded before implementation:

1. A strain is listed under the wrong species, so the narrowed strain choice
   selects no subject of the selected species.
2. A listed dose frequency differs from the value the derived filter compares
   (the filter upper-cases list values), so the choice matches no subject.
3. The year range uses a different start-year rule than the
   ``study_start_year`` filter, so a study inside the offered range is not
   selectable, or a study that the filter cannot match widens the range.
4. Empty or missing source values become a choice that matches no subject.
5. Choices are not in prevalence order, so the common choice is not first.
6. A listed route or planned dose duration differs from the value the derived
   filter compares, so a default such as ORAL GAVAGE or 28 days matches no subject.
"""

from __future__ import annotations

from conftest import run_query
from vcgen_query import CohortQuery, parse_subject_query
from vcgen_query.population import population_vocabulary


def _members(con, name: str, value_type: str, operator: str, value: object) -> set[str]:
    predicate = {"field": {"kind": "derived", "name": name, "value_type": value_type},
                 "operator": operator, "value": value}
    subject = parse_subject_query({"contract": "vcgen.subject_query.v1", "query_id": "population", "predicate": predicate})
    result = run_query(con, CohortQuery.compose(subject, ["BW_BW"]))
    return {row["subject_id"] for row in result.subjects.to_dict("records")}


def _connection(make_connection):
    subjects = [
        {"study_id": "A", "subject_id": str(index), "strain": "WISTAR HAN", "rfstdtc": "2018-03-01", "exdosfrq": "qd"}
        for index in range(3)
    ] + [
        {"study_id": "B", "subject_id": "1", "strain": "SPRAGUE DAWLEY", "rfstdtc": "2021-05-01", "exdosfrq": "BID",
         "exroute": "subcutaneous", "exmethod": "", "dose_duration": 14},
        {"study_id": "B", "subject_id": "2", "strain": "SPRAGUE DAWLEY", "rfstdtc": "2021-05-02", "exdosfrq": ""},
        {"study_id": "C", "subject_id": "1", "species": "MOUSE", "strain": "CD-1", "rfstdtc": "2019-01-01"},
        # Mixed years: the filter cannot match this study, so it must not widen the range.
        {"study_id": "D", "subject_id": "1", "strain": "", "rfstdtc": "2010-01-01"},
        {"study_id": "D", "subject_id": "2", "strain": "", "rfstdtc": "2025-01-01"},
    ]
    body_weight = [{"study_id": s["study_id"], "subject_id": s["subject_id"], "day": 0, "value": 200 + index * 10}
                   for index, s in enumerate(subjects)]
    return make_connection(subjects, body_weight=body_weight)


def test_every_choice_selects_subjects_through_the_query_filter(make_connection) -> None:
    con = _connection(make_connection)
    vocabulary = population_vocabulary(con)

    assert [item["value"] for item in vocabulary["species"]] == ["RAT", "MOUSE"]
    assert vocabulary["species"][0]["strains"] == ["WISTAR HAN", "SPRAGUE DAWLEY"]
    assert vocabulary["species"][1]["strains"] == ["CD-1"]
    for species in vocabulary["species"]:
        in_species = _members(con, "species", "string", "eq", species["value"])
        for strain in species["strains"]:
            assert _members(con, "strain", "string", "eq", strain) & in_species

    assert vocabulary["dose_frequencies"] == ["QD", "BID"]
    for frequency in vocabulary["dose_frequencies"]:
        assert _members(con, "dose_frequency", "string", "eq", frequency)

    assert vocabulary["routes"] == ["ORAL GAVAGE", "SUBCUTANEOUS"]
    for route in vocabulary["routes"]:
        assert _members(con, "route", "string", "eq", route)
    assert vocabulary["planned_dose_duration_days"] == [28, 14]
    assert _members(con, "planned_dose_duration_days", "number", "between", [14, 14]) == {"B-1", "B-2"}

    years = vocabulary["study_start_year"]
    assert (years["minimum"], years["maximum"]) == (2018, 2021)
    assert _members(con, "study_start_year", "number", "between", [years["minimum"], years["maximum"]]) == {
        "A-0", "A-1", "A-2", "B-1", "B-2", "C-1",
    }

    weight = vocabulary["baseline_body_weight_g"]
    assert weight["unit"] == "g"
    assert weight["minimum"] <= 200 and weight["maximum"] >= 270
    assert len(_members(con, "baseline_body_weight_g", "number", "between", [weight["minimum"], weight["maximum"]])) == 8
