from __future__ import annotations

from copy import deepcopy
import json

import pytest

from vcgen_query import endpoint_catalogue_from_mapping
from vcgen_query.endpoint_catalogue import bundled_catalogue_bytes


def _payload() -> dict[str, object]:
    """The bundled endpoint catalogue, as a mapping to change."""

    return json.loads(bundled_catalogue_bytes())


def _compile(payload: dict[str, object]):
    return endpoint_catalogue_from_mapping(payload, md5="a" * 32)


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


def _chemistry(payload: dict[str, object]) -> dict[str, object]:
    return next(item for item in payload["endpoints"] if item["name"] == "LB_ALT_BLOOD")


def test_catalogue_specimen_rules_are_positive_and_panel_bound() -> None:
    # Failure modes: a negative rule ("not URINE") enters the catalogue; a class
    # rule has no panel, an unknown panel, or classes other than the panel's; a
    # panel offers a user choice with one class; a panel names an unknown class.
    payload = _payload()
    _chemistry(payload)["specimen_rule"] = {"match": "not", "values": ["URINE"]}
    with pytest.raises(RuntimeError, match="match is invalid"):
        _compile(payload)

    payload = _payload()
    del _chemistry(payload)["specimen_panel"]
    with pytest.raises(RuntimeError, match="specimen_panel"):
        _compile(payload)

    payload = _payload()
    _chemistry(payload)["specimen_panel"] = "no_such_panel"
    with pytest.raises(RuntimeError, match="unknown specimen panel"):
        _compile(payload)

    payload = _payload()
    _chemistry(payload)["specimen_rule"] = {"match": "class", "values": ["SERUM"]}
    with pytest.raises(RuntimeError, match="classes of its specimen panel"):
        _compile(payload)

    payload = _payload()
    panel = next(item for item in payload["specimen_panels"] if item["id"] == "haematology")
    panel["user_choice"] = True
    with pytest.raises(RuntimeError, match="user choice"):
        _compile(payload)

    payload = _payload()
    panel = next(item for item in payload["specimen_panels"] if item["id"] == "blood_chemistry")
    panel["classes"] = ["SERUM", "BLOOD"]
    with pytest.raises(RuntimeError, match="unknown specimen class"):
        _compile(payload)


def test_catalogue_rejects_unknown_derived_parents() -> None:
    payload = _payload()
    payload["endpoints"][0]["derivation"] = {
        "kind": "ratio",
        "parents": ["NOT_IN_THE_CATALOGUE", "BW_TERMBW"],
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


def test_catalogue_rejects_parent_cycles() -> None:
    payload = _payload()
    endpoint = next(item for item in payload["endpoints"] if item["name"] == "OM_OWBW_BRAIN")
    endpoint["derivation"]["parents"] = ["OM_OWBW_BRAIN", "BW_TERMBW"]
    with pytest.raises(RuntimeError, match="cycle"):
        _compile(payload)
