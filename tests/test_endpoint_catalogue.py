from __future__ import annotations

from copy import deepcopy
from importlib import resources
from pathlib import Path
import json

import pytest

from vcgen_query import (
    ENDPOINT_CATALOGUE_FILENAME,
    canonical_endpoint,
    endpoint_catalogue_from_mapping,
    endpoint_catalogue_identity,
    load_endpoint_catalogue,
)
def _payload() -> dict[str, object]:
    resource = resources.files("vcgen_query").joinpath(
        "data", ENDPOINT_CATALOGUE_FILENAME
    )
    return json.loads(resource.read_text(encoding="utf-8"))


def _compile(payload: dict[str, object]):
    return endpoint_catalogue_from_mapping(payload, sha256="a" * 64)


def test_catalogue_has_stable_identity_and_endpoint_provenance() -> None:
    catalogue = load_endpoint_catalogue()
    assert endpoint_catalogue_identity() == catalogue.identity.to_canonical_dict()
    assert len(catalogue.endpoints) >= 3
    assert canonical_endpoint("LB_HCT").provenance["grit42_column_catalogue_id"]
    assert canonical_endpoint("LB_HCT").derivation.kind == "primitive"
    ratio = canonical_endpoint("OM_OWBW_BRAIN").derivation
    assert ratio.kind == "ratio"
    assert ratio.parents == ("OM_WEIGHT_BRAIN", "BW_TERMINAL")
    assert ratio.scale == 100.0


def test_catalogue_rejects_ambiguous_aliases() -> None:
    payload = _payload()
    duplicate = deepcopy(payload["endpoints"][0])
    duplicate["name"] = "AMBIGUOUS_COPY"
    payload["endpoints"].append(duplicate)
    with pytest.raises(RuntimeError, match="ambiguous aliases"):
        _compile(payload)


def test_catalogue_rejects_incomplete_qualifiers_and_invalid_units() -> None:
    payload = _payload()
    payload["endpoints"][0]["specimen_rule"] = {"match": "exact"}
    with pytest.raises(RuntimeError, match="incomplete qualifier"):
        _compile(payload)

    payload = _payload()
    payload["endpoints"][0]["source_units"] = ["not-a-UCUM-unit"]
    with pytest.raises(ValueError, match="unit"):
        _compile(payload)


def test_catalogue_rejects_unknown_derived_parents() -> None:
    payload = _payload()
    payload["endpoints"][0]["derivation"] = {
        "kind": "ratio",
        "parents": ["NOT_IN_THE_CATALOGUE", "BW_TERMINAL"],
        "scale": 100,
    }
    with pytest.raises(RuntimeError, match="unknown derived parent"):
        _compile(payload)


def test_catalogue_rejects_representation_timing() -> None:
    payload = _payload()
    payload["endpoint_behaviors"] = {}
    with pytest.raises(RuntimeError, match="unsupported fields"):
        _compile(payload)
    payload = _payload()
    payload["endpoints"][0]["day_window"] = {"start": 1, "end": 28}
    with pytest.raises(RuntimeError, match="unsupported fields"):
        _compile(payload)


def test_catalogue_cutover_preserves_every_source_selector_and_conversion() -> None:
    golden = json.loads((Path(__file__).parent / "fixtures/catalogue_selector_parity.json").read_text())
    assert {name: endpoint.spec.to_canonical_dict() for name, endpoint in load_endpoint_catalogue().endpoints.items()} == golden


def test_catalogue_rejects_parent_cycles() -> None:
    payload = _payload()
    endpoint = next(item for item in payload["endpoints"] if item["name"] == "OM_OWBW_BRAIN")
    endpoint["derivation"]["parents"] = ["OM_OWBW_BRAIN", "BW_TERMINAL"]
    with pytest.raises(RuntimeError, match="cycle"):
        _compile(payload)
