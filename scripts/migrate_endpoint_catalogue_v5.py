"""Migrate v4 query endpoint selectors to the canonical v5 endpoint catalogue.

This is an idempotent repository migration tool. It reads the checked-in v4
benchmark query only when the v5 catalogue does not yet exist. After the
migration, it validates the existing catalogue and rewrites all package query
and example files as v5 documents that contain endpoint names only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from vcgen_query import endpoint_catalogue_from_mapping


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "vcgen_query" / "data"
CATALOGUE = DATA / "canonical_endpoints.2026-09-17.v4.json"
CATALOGUE_ID = "grit42-canonical-endpoints-2026-09-17-v4"
COLUMN_CATALOGUE_ID = "grit42-columns-2026-08-22-v1"
QUERY_FILES = (
    DATA / "queries" / "gurjanov_28d_benchmark_full.yaml",
    DATA / "queries" / "gurjanov_28d_generation_core.yaml",
    DATA / "queries" / "hosted_debug_wistar_han_28d.yaml",
    DATA / "queries" / "wistar_han_28d_historical.yaml",
    DATA / "examples" / "all_subjects.yaml",
    DATA / "examples" / "wistar_han_28d_cohort.yaml",
    DATA / "examples" / "wistar_han_28d_with_observations.yaml",
)


def _label(name: str) -> str:
    known = {
        "BW": "Body weight",
        "BW_TERMINAL": "Terminal body weight",
        "LB_HCT": "Hematocrit",
    }
    return known.get(name, name.replace("_", " ").title())


def _derived_parents(name: str, names: set[str]) -> list[str]:
    if not name.startswith("OM_OWBW_"):
        return []
    organ = name.removeprefix("OM_OWBW_")
    parent = f"OM_WEIGHT_{organ}"
    return [parent, "BW_TERMINAL"] if parent in names else []


def _catalogue_from_v4() -> dict[str, Any]:
    source = yaml.safe_load(QUERY_FILES[0].read_text(encoding="utf-8"))
    legacy = source.get("observation_contract", {}).get("endpoints", [])
    if not legacy:
        raise RuntimeError("v4 benchmark query has no endpoint definitions to migrate")
    names = {str(item["name"]) for item in legacy}
    endpoints = []
    for item in legacy:
        name = str(item["name"])
        specimen_values = list(item.get("specimens", []))
        endpoints.append(
            {
                "name": name,
                "source_domain": str(item["domain"]),
                "test_code_aliases": list(item["test_codes"]),
                "test_name_aliases": list(item.get("test_names", [])),
                "specimen_rule": {
                    "match": (
                        str(item.get("specimen_match", "exact"))
                        if specimen_values
                        else "any"
                    ),
                    "values": specimen_values,
                },
                "source_units": list(item.get("source_units", [])),
                "target_unit": item.get("target_unit"),
                "method_rule": {"match": "any", "values": []},
                "sexes": list(item.get("sexes", [])),
                "display_label": _label(name),
                "derivation": (
                    {
                        "kind": "ratio",
                        "parents": _derived_parents(name, names),
                        "scale": 100.0,
                    }
                    if _derived_parents(name, names)
                    else {"kind": "primitive"}
                ),
                "provenance": {
                    "source": "vcgen v4 selector parity migration",
                    "source_query_id": str(source["query_id"]),
                    "grit42_column_catalogue_id": COLUMN_CATALOGUE_ID,
                    "captured_on": "2026-09-17",
                    "derivation_source": "vcgen protocol-v2 parity migration",
                },
            }
        )
    endpoints.append(
        {
            "name": "PRE_STUDY_BODY_TEMPERATURE_C",
            "source_domain": "VS",
            "test_code_aliases": ["TEMP"],
            "test_name_aliases": [],
            "specimen_rule": {"match": "any", "values": []},
            "source_units": ["C"],
            "target_unit": "Cel",
            "method_rule": {"match": "any", "values": []},
            "sexes": [],
            "display_label": "Pre-study body temperature",
            "derivation": {"kind": "primitive"},
            "provenance": {
                "source": "Grit42 form selector mapping",
                "source_query_id": "pre-study-body-temperature",
                "grit42_column_catalogue_id": COLUMN_CATALOGUE_ID,
                "captured_on": "2026-09-17",
                "derivation_source": "vcgen protocol-v2 parity migration",
            },
        }
    )
    return {
        "contract": "vcgen.canonical_endpoint_catalogue.v4",
        "catalogue_id": CATALOGUE_ID,
        "captured_on": "2026-09-17",
        "grit42_column_catalogue_id": COLUMN_CATALOGUE_ID,
        "source": {
            "kind": "validated_v4_parity_migration",
            "note": (
                "Selectors were migrated without changing accepted rows or canonical values. "
                "Future aliases must come from the controlled Grit42 extraction process."
            ),
        },
        "endpoints": endpoints,
    }


def _write_catalogue() -> None:
    if CATALOGUE.exists():
        payload = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        if payload.get("catalogue_id") != CATALOGUE_ID:
            raise RuntimeError("existing v5 endpoint catalogue identity is invalid")
        endpoint_catalogue_from_mapping(payload, sha256=hashlib.sha256(CATALOGUE.read_bytes()).hexdigest())
        return
    payload = _catalogue_from_v4()
    encoded = json.dumps(payload, indent=2) + "\n"
    endpoint_catalogue_from_mapping(payload, sha256=hashlib.sha256(encoded.encode()).hexdigest())
    CATALOGUE.write_text(encoded, encoding="utf-8")


def _catalogue_identity() -> dict[str, str]:
    digest = hashlib.sha256(CATALOGUE.read_bytes()).hexdigest()
    return {
        "contract": "vcgen.canonical_endpoint_catalogue.v4",
        "catalogue_id": CATALOGUE_ID,
        "sha256": digest,
    }


def _rewrite_query(path: Path, identity: dict[str, str]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    observations = document.pop("observation_contract", None)
    if observations is not None:
        requested = [str(item["name"]) for item in observations.get("endpoints", [])]
    else:
        requested = list(document.get("requested_endpoints", []))
    if not requested:
        raise RuntimeError(f"{path} must request at least one canonical endpoint")
    document["contract"] = "vcgen.cohort_query.v5"
    document["requested_endpoints"] = requested
    document["endpoint_catalogue"] = identity
    header = (
        "# yaml-language-server: $schema=../vcgen.cohort_query.v5.schema.json\n"
        "# Endpoint source selectors are resolved by the package catalogue.\n"
    )
    path.write_text(header + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def main() -> None:
    _write_catalogue()
    identity = _catalogue_identity()
    for path in QUERY_FILES:
        _rewrite_query(path, identity)


if __name__ == "__main__":
    main()
