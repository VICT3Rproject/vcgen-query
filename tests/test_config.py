"""Bundled v4 query documents load through the local contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from vcgen_query import COHORT_QUERY_CONTRACT, load_query, schema_dict


_DATA = Path(__file__).resolve().parents[1] / "src" / "vcgen_query" / "data"


def test_bundled_documents_are_v4_only_and_loadable() -> None:
    paths = [
        _DATA / "examples" / "all_subjects.yaml",
        _DATA / "examples" / "rat_28d_cohort.yaml",
        _DATA / "examples" / "rat_28d_with_observations.yaml",
        _DATA / "queries" / "wistar_han_28d_historical.yaml",
        _DATA / "queries" / "gurjanov_28d_benchmark_full.yaml",
        _DATA / "queries" / "gurjanov_28d_generation_core.yaml",
    ]

    for path in paths:
        query = load_query(path)
        assert query.contract == COHORT_QUERY_CONTRACT == "vcgen.cohort_query.v4"


def test_schema_lookup_requires_an_explicit_contract() -> None:
    with pytest.raises(TypeError):
        schema_dict()  # type: ignore[call-arg]


def test_gurjanov_profiles_separate_discovery_benchmark_and_generation() -> None:
    discovery = load_query(_DATA / "queries" / "wistar_han_28d_historical.yaml")
    benchmark = load_query(_DATA / "queries" / "gurjanov_28d_benchmark_full.yaml")
    core = load_query(_DATA / "queries" / "gurjanov_28d_generation_core.yaml")

    assert len(discovery.observation_contract.endpoints) == 65
    assert len(benchmark.observation_contract.endpoints) == 65
    assert len(core.observation_contract.endpoints) == 48
    assert benchmark.predicate == core.predicate
    assert discovery.predicate != core.predicate
    benchmark_names = {item.name for item in benchmark.observation_contract.endpoints}
    core_names = {item.name for item in core.observation_contract.endpoints}
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
        for endpoint in query.observation_contract.endpoints
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
