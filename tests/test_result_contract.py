"""QueryResult applies the public table schema to every table it holds.

Failure modes:
1. A table with missing, extra or reordered columns is accepted, so readers
   make up values for the missing columns.
2. A text column with float NaN is accepted, so readers can produce "nan".
3. A null in a required column is accepted.
4. A non-finite number is accepted, so readers must check every value again.
5. Duplicate subject keys are accepted.
6. A blank key, or a key with surrounding spaces, is accepted.
7. An observation whose subject is not in ``subjects`` is accepted.
8. An observation whose sample id or sex differs from its subject is accepted.
9. An observation of an endpoint outside ``endpoint_names`` is accepted.
10. Endpoint facts whose names differ from ``endpoint_names`` are accepted.
11. An observation of an endpoint that does not apply to its subject's sex is accepted.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from vcgen_query import EndpointFacts, QueryResult
from vcgen_query.table_schema import OBSERVATIONS_TABLE, SUBJECTS_TABLE


def _observation(**values: object) -> dict[str, object]:
    row: dict[str, object] = {column.name: None for column in OBSERVATIONS_TABLE.columns}
    row.update(
        study_id="S",
        subject_id="S-1",
        sample_id="S::S-1",
        sex="F",
        endpoint_name="HCT",
        domain="LB",
        measurement_identity="test_code=HCT",
        endpoint_key="HCT",
        result_state="numeric_observed",
        numeric_value=0.4,
        excluded=False,
        source_order=("", 1.0, "1", 0),
        explicit_baseline=False,
        unit_inferred=False,
    )
    row.update(values)
    return row


def _subject(**values: object) -> dict[str, object]:
    row: dict[str, object] = {
        column.name: [] if column.pandas_dtype == "object" else None
        for column in SUBJECTS_TABLE.columns
    }
    row.update(
        sample_id="S::S-1",
        study_id="S",
        subject_id="S-1",
        species="RAT",
        strain="WISTAR HAN",
        sex="F",
        dose_duration_evidence_status="corroborated",
    )
    row.update(values)
    return row


def _result(
    observations: pd.DataFrame,
    subjects: list[dict[str, object]] | None = None,
    *,
    endpoint_names: tuple[str, ...] = ("HCT",),
    facts: tuple[str, ...] = ("HCT",),
) -> QueryResult:
    return QueryResult(
        subjects=pd.DataFrame(
            [_subject()] if subjects is None else subjects,
            columns=[column.name for column in SUBJECTS_TABLE.columns],
        ),
        observations=observations,
        metadata={},
        receipt={},
        endpoint_names=endpoint_names,
        endpoint_facts=tuple(EndpointFacts(name, "LB") for name in facts),
    )


def test_tables_get_the_declared_dtypes() -> None:
    result = _result(pd.DataFrame([_observation(specimen=math.nan)]))

    for column in OBSERVATIONS_TABLE.columns:
        assert str(result.observations[column.name].dtype) == column.pandas_dtype
    assert result.observations["specimen"].isna().all()
    assert result.observations["specimen"].astype(object).iloc[0] is pd.NA


def test_a_table_without_a_declared_column_is_rejected() -> None:
    frame = pd.DataFrame([_observation()]).drop(columns=["unit_inferred"])

    with pytest.raises(ValueError, match="observations columns"):
        _result(frame)


@pytest.mark.parametrize("column", ["excluded", "source_order", "endpoint_name"])
def test_a_null_in_a_required_column_is_rejected(column: str) -> None:
    with pytest.raises(ValueError, match=f"observations.{column}"):
        _result(pd.DataFrame([_observation(**{column: None})]))


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_a_non_finite_number_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="observations.numeric_value"):
        _result(pd.DataFrame([_observation(numeric_value=value)]))


def test_duplicate_subject_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="subjects.*repeat"):
        _result(pd.DataFrame([_observation()]), [_subject(), _subject()])


@pytest.mark.parametrize("subject_id", ["", " S-1"])
def test_a_blank_or_untrimmed_key_is_rejected(subject_id: str) -> None:
    with pytest.raises(ValueError, match="subjects.subject_id"):
        _result(pd.DataFrame([_observation()]), [_subject(subject_id=subject_id)])


@pytest.mark.parametrize(
    "values",
    [{"subject_id": "S-2"}, {"sample_id": "S::other"}, {"sex": "M"}],
)
def test_an_observation_must_match_a_selected_subject(values: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="observations do not match"):
        _result(pd.DataFrame([_observation(**values)]))


def test_an_observation_of_an_undeclared_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="endpoint_names"):
        _result(pd.DataFrame([_observation(endpoint_name="ALT")]))


def test_endpoint_facts_must_name_exactly_the_endpoints() -> None:
    with pytest.raises(ValueError, match="endpoint_facts"):
        _result(pd.DataFrame([_observation()]), facts=("HCT", "ALT"))


def test_an_observation_outside_its_endpoint_sexes_is_rejected() -> None:
    result = _result(pd.DataFrame([_observation()]))

    with pytest.raises(ValueError, match="sex"):
        QueryResult(
            subjects=result.subjects,
            observations=result.observations,
            metadata={},
            receipt={},
            endpoint_names=("HCT",),
            endpoint_facts=(EndpointFacts("HCT", "LB", ("M",)),),
        )
