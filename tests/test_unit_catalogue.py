"""Independent UCUM checks for the package-owned affine unit catalogue."""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path

import pytest
from ucumvert import PintUcumRegistry

from vcgen_query.unit_catalogue import _CATALOGUE, compile_unit_rules


VALUES = (0.0, 1.0, 2.0, 273.15)


@pytest.fixture(scope="module")
def ucum() -> PintUcumRegistry:
    return PintUcumRegistry()


def _internal(source: str, target: str, value: float) -> float:
    rule = compile_unit_rules((source,), target)[0]
    return value * rule.scale + rule.offset


def test_internal_catalogue_matches_ucum_2_2_for_all_compatible_canonical_pairs(
    ucum: PintUcumRegistry,
) -> None:
    definitions = tuple(_CATALOGUE.units.values())
    for source in definitions:
        for target in definitions:
            if source.dimension != target.dimension:
                continue
            for value in VALUES:
                expected = ucum.convert(
                    value,
                    ucum.from_ucum(source.code),
                    ucum.from_ucum(target.code),
                )
                assert _internal(source.code, target.code, value) == pytest.approx(
                    expected,
                    rel=1e-12,
                    abs=1e-12,
                ), (source.code, target.code, value)


@pytest.mark.parametrize(
    ("source", "target", "resolved"),
    [
        ("G", "g", "g"),
        ("MG", "g", "mg"),
        ("µkat/L", "U/L", "ukat/L"),
        ("μkat/L", "ukat/L", "ukat/L"),
        ("°C", "K", "Cel"),
        ("C", "Cel", "Cel"),
    ],
)
def test_exact_source_aliases_resolve_only_through_the_catalogue(
    source: str, target: str, resolved: str
) -> None:
    rule = compile_unit_rules((source,), target)[0]
    assert rule.source_unit == source
    assert rule.source_ucum == resolved


def test_internal_catalogue_matches_nlm_ucum_4_1_9_golden_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures/nlm_ucum_4.1.9_golden.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for conversion in payload["conversions"]:
        actual = [
            _internal(conversion["source"], conversion["target"], value)
            for value in payload["values"]
        ]
        assert actual == pytest.approx(conversion["expected"])


def test_catalogue_resource_is_packaged() -> None:
    resource = resources.files("vcgen_query").joinpath("data/unit_catalogue.v2.json")
    assert resource.is_file()
