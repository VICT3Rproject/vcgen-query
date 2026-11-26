"""Daily query-result projection tests."""

from __future__ import annotations

import pandas as pd
import pytest

import vcgen_query as vq


_KEYS = {"study_id": "S", "subject_id": "S-1", "study_day": 1}


def _result(
    records: list[dict[str, object]],
    facts: tuple[vq.EndpointFacts, ...] = (),
    *,
    endpoints: tuple[str, ...] = ("HCT",),
) -> vq.QueryResult:
    return vq.QueryResult(
        subjects=pd.DataFrame(),
        observations=pd.DataFrame(records),
        metadata={"query_id": "daily-test", "source_identity": "synthetic"},
        receipt={},
        endpoint_names=endpoints,
        endpoint_facts=facts,
    )


def _observation(
    value: float | None,
    unit: str | None,
    *,
    endpoint_name: str = "HCT",
    **overrides: object,
) -> dict[str, object]:
    return {
        **_KEYS,
        "endpoint_name": endpoint_name,
        "numeric_value": value,
        "unit": unit,
        "result_state": "numeric_observed",
        "excluded": False,
        "measurement_identity": endpoint_name,
        "source_order": (),
        **overrides,
    }


def _facts(
    endpoint_name: str, source_units: tuple[str, ...], target: str
) -> tuple[vq.EndpointFacts, ...]:
    endpoint = vq.EndpointSpec(
        endpoint_name,
        "LB",
        (endpoint_name,),
        source_units=source_units,
        target_unit=target,
    )
    return (
        vq.EndpointFacts(
            endpoint_name,
            "LB",
            target_unit=endpoint.target_unit,
            compiled_unit_rules=endpoint.compiled_unit_rules,
        ),
    )


def test_daily_frame_converts_and_collapses_decimal_equivalents() -> None:
    result = _result(
        [
            _observation(3, "g/L", endpoint_name="HGB"),
            _observation(0.3, "g/dL", endpoint_name="HGB"),
        ],
        _facts("HGB", ("g/L", "g/dL"), "g/dL"),
        endpoints=("HGB",),
    )

    daily = vq.to_daily_frame(result)

    assert daily.values.loc[0, "HGB"] == pytest.approx(0.3)
    assert daily.cell_audit.loc[0, "resolution"] == "equivalent_duplicate_collapsed"
    assert daily.receipt["conversion_count"] == 1
    assert daily.receipt["daily_projection_fingerprint"]
    rule = daily.cell_audit.loc[0, "conversion_rule"][0]
    assert rule["source_ucum"] == "g/L"
    assert rule["target_unit"] == "g/dL"
    assert rule["catalogue_id"] == "vcgen.unit_catalogue.v2"


def test_equivalent_alias_is_an_audited_identity_conversion() -> None:
    daily = vq.to_daily_frame(
        _result(
            [_observation(3, "G", endpoint_name="MASS")],
            _facts("MASS", ("G",), "g"),
            endpoints=("MASS",),
        )
    )

    assert daily.values.loc[0, "MASS"] == 3
    assert daily.cell_audit.loc[0, "conversion_status"] == ["identity"]
    rule = daily.cell_audit.loc[0, "conversion_rule"][0]
    assert rule["source_unit"] == "G"
    assert rule["source_ucum"] == "g"
    assert rule["scale"] == 1
    assert rule["offset"] == 0


def test_non_finite_converted_output_raises_structured_unit_error() -> None:
    with pytest.raises(vq.DailyProjectionUnitError) as error:
        vq.to_daily_frame(
            _result(
                [_observation(1e308, "kg", endpoint_name="MASS")],
                _facts("MASS", ("kg",), "g"),
                endpoints=("MASS",),
            )
        )

    assert error.value.failures[0]["endpoint_name"] == "MASS"
    assert error.value.failures[0]["target_unit"] == "g"




def test_values_select_numbers_while_states_describe_matching_source_records() -> None:
    daily = vq.to_daily_frame(
        _result(
            [
                _observation(1, "L/L", study_day=1),
                _observation(
                    None,
                    None,
                    study_day=2,
                    result_state="qualitative_observed",
                ),
            ]
        )
    )

    assert daily.values["study_day"].tolist() == [1]
    assert daily.values["HCT"].tolist() == [1.0]
    assert daily.values[["HCT"]].notna().any(axis="columns").all()
    assert daily.states.set_index("study_day")["HCT"].to_dict() == {
        1: "numeric_observed",
        2: "qualitative_observed",
    }
    audit = daily.cell_audit.set_index("study_day")
    assert audit.loc[1, "resolution"] == "selected"
    assert audit.loc[2, "resolution"] == "no_numeric_value"


def test_daily_frame_keeps_laboratory_values_on_terminal_study_days() -> None:
    daily = vq.to_daily_frame(
        _result(
            [
                _observation(
                    250,
                    "G",
                    endpoint_name="BW",
                    study_day=1,
                    measurement_identity="BW",
                ),
                _observation(
                    0.48,
                    "%",
                    endpoint_name="LB_HCT",
                    study_day=28,
                    measurement_identity="LB_HCT",
                ),
            ],
            endpoints=("BW", "LB_HCT"),
        )
    )

    values_by_day = daily.values.set_index("study_day")
    assert values_by_day.loc[1, "BW"] == pytest.approx(250)
    assert values_by_day.loc[28, "LB_HCT"] == pytest.approx(0.48)
    assert daily.states.set_index("study_day").loc[28, "LB_HCT"] == "numeric_observed"


def test_daily_frame_raises_structured_conflict() -> None:
    result = _result([_observation(1, "L/L"), _observation(2, "L/L")])

    with pytest.raises(vq.DailyProjectionConflictError) as error:
        vq.to_daily_frame(result)

    assert error.value.conflicts[0]["endpoint_name"] == "HCT"


def test_daily_frame_reports_conflicting_cells_without_value() -> None:
    result = _result([_observation(1, "L/L"), _observation(2, "L/L")])

    daily = vq.to_daily_frame(result, on_conflict="report")

    assert daily.values.empty
    assert daily.receipt["conflict_count"] == 1
    assert daily.cell_audit.loc[0, "resolution"] == "conflict_reported"


def test_daily_frame_does_not_equate_units_without_conversion_metadata() -> None:
    result = _result([_observation(1, "MG/DL"), _observation(1, "G/L")])

    with pytest.raises(vq.DailyProjectionConflictError):
        vq.to_daily_frame(result)


def test_daily_frame_rejects_endpoint_key_collision() -> None:
    result = _result([], endpoints=("study_id",))

    with pytest.raises(ValueError, match="daily key columns"):
        vq.to_daily_frame(result)
