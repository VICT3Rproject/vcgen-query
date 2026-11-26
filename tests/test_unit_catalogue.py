"""Independent UCUM checks for the package-owned affine unit catalogue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ucumvert import PintUcumRegistry

from vcgen_query.contract_helpers import QueryError
from vcgen_query.unit_catalogue import _CATALOGUE, compile_unit_rules


VALUES = (0.0, 1.0, 2.0, 273.15)


@pytest.fixture(scope="module")
def ucum() -> PintUcumRegistry:
    return PintUcumRegistry()


def _internal(source: str, target: str, value: float) -> float:
    rule = compile_unit_rules((source,), target)[0]
    return value * rule.scale + rule.offset


# Annotated codes that name one analyte or one scaled quantity. UCUM ignores
# an annotation, so it reads `g/L{Hb}` as a mass concentration. The oracle is
# the reference code in the catalogue dimension and the factor from the code
# to it, from the molar mass (g/mol) or the scale of the quantity. Failure
# modes: a molar mass with a wrong power of ten, or the reciprocal factor.
ANNOTATED = {
    "%{SG}": ("1{SG}", 0.01),
    "g/L{SG}": ("1{SG}", 1e-3),
    "g/L{Hb}": ("mol/L", 1 / 16114.5),
    "mmol/L{Hb}": ("g/L", 16.1145),
    "fmol{Hb}": ("pg", 16.1145),
    "g/L{albumin}": ("mol/L", 1 / 66500),
    "ug/L{PTH}": ("pmol/L", 1000 / 9.425),
    "umol{creatinine}": ("mg", 0.11312),
    "mmol{glucose}": ("mg", 180.16),
}


def _reference(code: str) -> tuple[str, float]:
    return ANNOTATED.get(code, (code, 1.0))


def test_internal_catalogue_matches_ucum_2_2_for_all_compatible_canonical_pairs(
    ucum: PintUcumRegistry,
) -> None:
    assert {code for code in _CATALOGUE.units if "{" in code} - set(ANNOTATED) == {"1{pH}", "1{SG}", "1{index}"}
    definitions = tuple(_CATALOGUE.units.values())
    for source in definitions:
        for target in definitions:
            if source.dimension != target.dimension:
                continue
            (source_reference, source_factor) = _reference(source.code)
            (target_reference, target_factor) = _reference(target.code)
            for value in VALUES:
                expected = ucum.convert(
                    value * source_factor,
                    ucum.from_ucum(source_reference),
                    ucum.from_ucum(target_reference),
                ) / target_factor
                assert _internal(source.code, target.code, value) == pytest.approx(
                    expected,
                    rel=1e-12,
                    abs=1e-12,
                ), (source.code, target.code, value)


def test_internal_catalogue_matches_nlm_ucum_4_1_9_golden_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures/nlm_ucum_4.1.9_golden.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for conversion in payload["conversions"]:
        actual = [
            _internal(conversion["source"], conversion["target"], value)
            for value in payload["values"]
        ]
        assert actual == pytest.approx(conversion["expected"])


# Failure modes for the v3 additions: a scaled count label drifts in float
# arithmetic instead of mapping to its exact base unit; a micro label resolves to
# the wrong scale; a dimensionless measurement unit (pH, specific gravity) mixes
# with fractions; Unicode micro and Greek mu spellings resolve differently.
@pytest.mark.parametrize(
    ("label", "target"),
    [
        ("10^3/µL", "10^9/L"),
        ("10^3/uL", "10^9/L"),
        ("10^6/µL", "10^12/L"),
        ("/nL", "10^9/L"),
        ("µg/L", "ug/L"),
        ("μg/L", "ug/L"),
        ("µmol/L", "umol/L"),
        ("μmol/L", "umol/L"),
        ("RATIO", "1"),
        ("mOsm/kg", "mosm/kg"),
        # Percent of a reference population: the endpoint names the population.
        ("% WBC", "%"),
        ("/100 WBC", "%"),
        ("% RBC", "%"),
        ("/100 RBC", "%"),
        # Organ weight ratios: the endpoint names the denominator (body or brain).
        ("% body weight", "%"),
        ("% body", "%"),
        ("% brain weight", "%"),
        ("% brain", "%"),
        ("g/100g", "%"),
        # Creatinine-normalized ratios and other exact relabelings.
        ("µg/mmol", "ug/mmol"),
        ("µg/mmol Creat U", "ug/mmol"),
        ("µkat/mmol", "ukat/mmol"),
        ("U/mmol Creat U", "U/mmol"),
        ("mmol/mmol", "mol/mol"),
        ("osmol/mol", "osm/mol"),
        ("INR", "1"),
        ("nmol BCE/L", "nmol/L"),
    ],
)
def test_alias_labels_resolve_to_their_base_unit_without_scaling(label: str, target: str) -> None:
    rule = compile_unit_rules((label,), target)[0]
    assert (rule.scale, rule.offset) == (1.0, 0.0)


@pytest.mark.parametrize(
    ("label", "target", "scale"),
    [
        ("µg/L", "mg/L", 1e-3),
        ("ng/mL", "ug/L", 1.0),
        ("pg/mL", "ng/L", 1.0),
        ("µmol/L", "mmol/L", 1e-3),
        ("nmol/L", "umol/L", 1e-3),
        ("pmol/L", "nmol/L", 1e-3),
        ("pg", "fmol", None),
        ("/µL", "10^9/L", 1e-3),
        ("10^6/L", "10^9/L", 1e-3),
        ("g/mmol", "mg/mmol", 1e3),
        ("ng/mmol", "ug/mmol", 1e-3),
        ("g/mol", "mg/mmol", 1.0),
        ("U/mmol", "ukat/mmol", 1 / 60),
        ("U/mg", "U/g", 1e3),
        ("mol/mol", "%", 100.0),
        ("g/kg", "%", 0.1),
        ("ng/mg", "g/kg", 1e-3),
        ("ug/mmol", "g/L", None),
    ],
)
def test_prefixed_units_convert_by_exact_powers_of_ten(label: str, target: str, scale: float | None) -> None:
    if scale is None:
        with pytest.raises(QueryError, match="incompatible"):
            compile_unit_rules((label,), target)
        return
    rule = compile_unit_rules((label,), target)[0]
    assert rule.scale == pytest.approx(scale, rel=1e-15)
    assert rule.offset == 0.0


@pytest.mark.parametrize(
    ("source", "target"),
    [("1{pH}", "1"), ("1{SG}", "1"), ("1{pH}", "1{SG}"), ("1{index}", "1"), ("1{index}", "1{pH}")],
)
def test_dimensionless_measurement_units_do_not_convert_to_fractions(source: str, target: str) -> None:
    with pytest.raises(QueryError, match="incompatible"):
        compile_unit_rules((source,), target)


# `% RETI` labels a reticulocyte subpopulation (median about 52%) under the test
# name RETICULOCYTES, beside reticulocytes as % of red cells (median about 2.4%).
# The label is the only signal that separates the two, so it must stay unresolved.
@pytest.mark.parametrize("label", ["% RETI", "% Lymphocytes"])
def test_population_labels_that_change_the_measurand_stay_unresolved(label: str) -> None:
    with pytest.raises(QueryError, match="unknown unit"):
        compile_unit_rules((label,), "%")
