"""Bundled query examples use the correct source and local contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vcgen_query import (
    COHORT_QUERY_CONTRACT,
    import_grit42_subject_query,
    load_query,
    schema_dict,
)


_DATA = Path(__file__).resolve().parents[1] / "src" / "vcgen_query" / "data"


def test_bundled_documents_are_v5_only_and_loadable() -> None:
    paths = sorted(
        path
        for directory in (_DATA / "examples", _DATA / "queries")
        for path in directory.iterdir()
        if path.suffix in {".json", ".yaml", ".yml"}
    )

    assert paths
    for path in paths:
        query = load_query(path)
        assert query.contract == COHORT_QUERY_CONTRACT == "vcgen.cohort_query.v5"
        assert query.requested_endpoints


def test_saved_grit42_example_compiles_to_its_v5_counterpart() -> None:
    source_path = (
        _DATA / "saved_grit42_queries" / "hosted_debug_wistar_han_28d.json"
    )
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    imported = import_grit42_subject_query(raw, ["BW", "LB_HCT"])
    expected = load_query(_DATA / "queries" / "hosted_debug_wistar_han_28d.yaml")

    assert imported.to_canonical_dict() == expected.to_canonical_dict()


def test_wistar_examples_use_supported_broad_filters_and_hct_categories() -> None:
    cohort = load_query(_DATA / "examples" / "wistar_han_28d_cohort.yaml")
    observed = load_query(
        _DATA / "examples" / "wistar_han_28d_with_observations.yaml"
    )

    for query in (cohort, observed):
        predicate = query.predicate.to_canonical_dict()
        derived_names = {
            child["field"]["name"]
            for child in predicate["children"]
            if child["field"]["kind"] == "derived"
        }
        assert "treatment_vehicle" not in derived_names

    hct = next(
        endpoint
        for endpoint in observed.resolved_endpoints
        if endpoint.name == "LB_HCT"
    )
    assert hct.specimens == ("URINE",)
    assert hct.specimen_match == "not"
    assert hct.source_units == ("%",)
    assert hct.target_unit == "%"


def test_schema_lookup_requires_an_explicit_contract() -> None:
    with pytest.raises(TypeError):
        schema_dict()  # type: ignore[call-arg]


def test_gurjanov_profiles_separate_discovery_benchmark_and_generation() -> None:
    discovery = load_query(_DATA / "queries" / "wistar_han_28d_historical.yaml")
    benchmark = load_query(_DATA / "queries" / "gurjanov_28d_benchmark_full.yaml")
    core = load_query(_DATA / "queries" / "gurjanov_28d_generation_core.yaml")

    assert len(discovery.requested_endpoints) == 65
    assert len(benchmark.requested_endpoints) == 65
    assert len(core.requested_endpoints) == 48
    assert benchmark.predicate == core.predicate
    assert discovery.predicate != core.predicate
    benchmark_names = set(benchmark.requested_endpoints)
    core_names = set(core.requested_endpoints)
    assert benchmark_names - core_names == {
        "LB_APTT",
        "LB_BILI",
        "LB_CK",
        "LB_FIBRINO",
        "LB_GGT",
        "LB_GLDH",
        "LB_PROT_URINE",
        "LB_PT",
        "LB_TRIG",
        "LB_UREA",
        "LB_WBC",
        "OM_OWBW_ADRENAL",
        "OM_OWBW_EPIDIDYMIS",
        "OM_OWBW_SEMINAL_VESICLE",
        "OM_WEIGHT_EPIDIDYMIS",
        "OM_WEIGHT_SEMINAL_VESICLE",
        "OM_WEIGHT_UTERUS",
    }
    assert all(
        endpoint.day_window is None
        for query in (discovery, benchmark, core)
        for endpoint in query.resolved_endpoints
    )

    predicate = core.predicate.to_canonical_dict()
    rules = [item for item in predicate["children"] if "field" in item]
    assert any(
        item["field"] == {
            "kind": "send_column",
            "domain": "EX",
            "column": "EXDOSE",
            "value_type": "number",
        }
        and item["operator"] == "eq"
        and item["value"] == 0
        for item in rules
    )
    derived = {
        (item["field"].get("name"), item["operator"]): item.get("value")
        for item in rules
        if item["field"]["kind"] == "derived"
    }
    assert derived[("study_start_year", "between")] == [2017.0, 2022.0]
    assert derived[("study_type", "eq")] == "REPEAT DOSE TOXICITY"
    assert derived[("dose_frequency", "eq")] == "QD"
