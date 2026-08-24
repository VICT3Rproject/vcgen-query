"""Long-format SEND observation extraction for a selected cohort.

Given a :class:`~vcgen_query.cohort.CohortSelection` and an
:class:`~vcgen_query.contract.ObservationContract`, :func:`extract_observations`
returns one record per parsed SEND measurement that matches a configured
endpoint, scoped to the selected subjects. This is the *query* output: parsed,
normalized observations in long form.

What is deliberately absent (it lives in the modelling layer, not the query
contract):

* single-table shaping (wide feature columns, one-cell-per-sample/day
  deduplication, duplicate-cell errors);
* temporal/terminal slice selection and coverage (presence) filtering;
* baseline attachment and endpoint-role validation;
* study splitting and outer-evaluation subject selection.

Each endpoint is matched by domain, test code (and optionally test name, source
unit, specimen, subject sex, and an inclusive day window). SEND parsing
(:mod:`vcgen_query.send_semantics`) classifies every matched cell into a result
state (``numeric_observed``, ``censored_observed``, ``qualitative_observed``,
``explicitly_failed``, ``silently_absent``) so downstream code can decide what
is numerically usable without re-parsing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, fields
from typing import Any

from .cohort import CohortSelection
from .contract import EndpointSpec, ObservationContract
from .endpoint_rows import (
    endpoint_matches_row,
    endpoint_projection_columns,
    fetch_batches,
    row_pool_id,
)
from .duckdb import q, quote_ident, table_columns
from .send_columns import STUDY_ID, SUBJECT_ID
from .send_semantics import (
    RESULT_STATES,
    ResultState,
    baseline_flag_columns,
    day_columns,
    endpoint_key,
    first_numeric_day,
    is_explicit_baseline,
    measurement_context,
    measurement_identity,
    parse_result,
    qualifier_columns,
    source_order_key,
)



@dataclass(frozen=True)
class ObservationRecord:
    """One parsed SEND measurement matching a configured endpoint."""

    study_id: str
    subject_id: str
    sample_id: str
    sex: str | None
    pool_id: str | None
    endpoint_name: str
    domain: str
    test_code: str | None
    test_name: str | None
    study_day: int | None
    measurement_identity: str
    endpoint_key: str
    specimen: str | None
    unit: str | None
    method: str | None
    laterality: str | None
    portion_totality: str | None
    fasting_status: str | None
    laboratory_name: str | None
    result_state: ResultState
    numeric_value: float | None
    raw_value: str | None
    value_column: str | None
    unit_column: str | None
    excluded: bool
    exclusion_reason: str | None
    failure_reason: str | None
    source_order: tuple[Any, ...]
    explicit_baseline: bool

    def __post_init__(self) -> None:
        if not isinstance(self.result_state, str) or self.result_state not in RESULT_STATES:
            raise ValueError(f"unsupported SEND result state: {self.result_state!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "study_id": self.study_id,
            "subject_id": self.subject_id,
            "sample_id": self.sample_id,
            "sex": self.sex,
            "pool_id": self.pool_id,
            "endpoint_name": self.endpoint_name,
            "domain": self.domain,
            "test_code": self.test_code,
            "test_name": self.test_name,
            "study_day": self.study_day,
            "measurement_identity": self.measurement_identity,
            "endpoint_key": self.endpoint_key,
            "specimen": self.specimen,
            "unit": self.unit,
            "method": self.method,
            "laterality": self.laterality,
            "portion_totality": self.portion_totality,
            "fasting_status": self.fasting_status,
            "laboratory_name": self.laboratory_name,
            "result_state": self.result_state,
            "numeric_value": self.numeric_value,
            "raw_value": self.raw_value,
            "value_column": self.value_column,
            "unit_column": self.unit_column,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "failure_reason": self.failure_reason,
            "source_order": self.source_order,
            "explicit_baseline": self.explicit_baseline,
        }
        return payload


OBSERVATION_COLUMNS = tuple(f.name for f in fields(ObservationRecord))


@dataclass(frozen=True)
class ObservationResult:
    """Long-format observations for a cohort plus a receipt."""

    records: tuple[ObservationRecord, ...] = ()
    endpoint_names: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return len(self.records)

    def to_receipt(self) -> dict[str, Any]:
        by_endpoint: dict[str, int] = defaultdict(int)
        by_state: dict[str, int] = defaultdict(int)
        numeric = 0
        for record in self.records:
            by_endpoint[record.endpoint_name] += 1
            by_state[record.result_state] += 1
            if record.numeric_value is not None and not record.excluded:
                numeric += 1
        return {
            "record_count": self.record_count,
            "endpoint_names": list(self.endpoint_names),
            "records_by_endpoint": dict(sorted(by_endpoint.items())),
            "records_by_result_state": dict(sorted(by_state.items())),
            "numeric_usable_count": numeric,
            "warnings": list(self.warnings),
            "counts": dict(self.counts),
        }


def extract_observations(
    con: Any,
    selection: CohortSelection,
    observation_contract: ObservationContract,
) -> ObservationResult:
    """Extract long-format SEND observations for a selected cohort."""

    endpoints = observation_contract.endpoints
    if not endpoints or not selection.subjects:
        warnings: list[str] = []
        if not endpoints:
            warnings.append("observation_contract has no endpoints; no observations extracted.")
        elif not selection.subjects:
            warnings.append("cohort selection is empty; no observations extracted.")
        return ObservationResult(
            records=(),
            endpoint_names=tuple(e.name for e in endpoints),
            warnings=tuple(warnings),
            counts={"record_count": 0},
        )

    contexts_by_key = {(c.study_id, c.subject_id): c for c in selection.subjects}
    selected_studies = {c.study_id for c in selection.subjects}
    endpoints_by_domain: dict[str, list[EndpointSpec]] = defaultdict(list)
    for endpoint in endpoints:
        endpoints_by_domain[endpoint.domain].append(endpoint)

    records: list[ObservationRecord] = []
    warnings: list[str] = []
    skipped_not_selected = 0

    for domain, domain_endpoints in sorted(endpoints_by_domain.items()):
        columns = table_columns(con, domain.lower())
        if not columns:
            warnings.append(
                f"{domain}: configured endpoint domain is not available in the source data."
            )
            continue
        days = day_columns(domain, columns)
        qualifiers = qualifier_columns(domain, columns)
        baseline_flags = baseline_flag_columns(domain, columns)
        cursor = _endpoint_filtered_cursor(con, domain, columns, selected_studies)
        domain_columns = tuple(item[0] for item in cursor.description)
        source_index = 0
        domain_seen = False
        for batch in fetch_batches(cursor):
            for raw in batch:
                source_index += 1
                row = dict(zip(domain_columns, raw))
                study_id = str(row.get(STUDY_ID) or "")
                subject_id = str(row.get(SUBJECT_ID) or "").strip()
                subject_key = (study_id, subject_id)
                context = contexts_by_key.get(subject_key)
                if context is None:
                    skipped_not_selected += 1
                    continue
                study_day = first_numeric_day(row, days)
                matching = [
                    endpoint
                    for endpoint in domain_endpoints
                    if endpoint_matches_row(endpoint, row, columns)
                    and endpoint.applies_to_sex(context.sex)
                    and (endpoint.day_window is None or endpoint.day_window.contains(study_day))
                ]
                if not matching:
                    continue
                parsed = parse_result(domain, row, domain_columns)
                measurement = measurement_context(domain, row, domain_columns, parsed_result=parsed)
                order = source_order_key(domain, row, stable_index=source_index)
                for endpoint in matching:
                    identity = measurement_identity(
                        domain,
                        row,
                        domain_columns,
                        parsed_result=parsed,
                        canonical_test_code=endpoint.name,
                    )
                    domain_seen = True
                    records.append(
                        ObservationRecord(
                            study_id=study_id,
                            subject_id=subject_id,
                            sample_id=context.sample_id,
                            sex=context.sex,
                            pool_id=row_pool_id(domain, row),
                            endpoint_name=endpoint.name,
                            domain=domain,
                            test_code=measurement["test_code"],
                            test_name=measurement["test_name"],
                            study_day=study_day,
                            measurement_identity=identity,
                            endpoint_key=endpoint_key(domain, row, domain_columns, qualifiers),
                            specimen=measurement["specimen"],
                            unit=measurement["unit"],
                            method=measurement["method"],
                            laterality=measurement["laterality"],
                            portion_totality=measurement["portion_totality"],
                            fasting_status=measurement["fasting_status"],
                            laboratory_name=measurement["laboratory_name"],
                            result_state=parsed.state,
                            numeric_value=parsed.numeric_value,
                            raw_value=parsed.raw_value,
                            value_column=parsed.value_column,
                            unit_column=parsed.unit_column,
                            excluded=parsed.excluded,
                            exclusion_reason=parsed.exclusion_reason,
                            failure_reason=parsed.failure_reason,
                            explicit_baseline=is_explicit_baseline(row, baseline_flags),
                            source_order=order,
                        )
                    )
        if not domain_seen:
            warnings.append(f"{domain}: no configured observations were extracted for the cohort.")

    counts = {
        "record_count": len(records),
        "endpoint_count": len(endpoints),
        "domain_count": len(endpoints_by_domain),
        "skipped_not_selected": skipped_not_selected,
    }
    return ObservationResult(
        records=tuple(records),
        endpoint_names=tuple(endpoint.name for endpoint in endpoints),
        warnings=tuple(warnings),
        counts=counts,
    )


# --- endpoint matching helpers (ported from the host extraction layer) -----


def _endpoint_filtered_cursor(
    con: Any,
    domain: str,
    columns: tuple[str, ...],
    selected_studies: set[str],
) -> Any:
    """Stream a domain restricted to selected studies.

    Test-code matching is performed in Python by :func:`_endpoint_matches_row`
    using the canonical SEND normalizer, so it is intentionally NOT prefiltered
    in SQL. A SQL ``upper(trim(...))`` prefilter would diverge from NFKC
    normalization and silently drop matching rows (e.g. full-width codes).
    """

    selected_columns = endpoint_projection_columns(domain, columns)
    projection = ", ".join(q(column) for column in selected_columns)
    predicates: list[str] = []
    params: list[Any] = []
    study_ids = tuple(sorted(study_id for study_id in selected_studies if study_id))
    if study_ids:
        study_placeholders = ", ".join("?" for _ in study_ids)
        predicates.append(f"cast({q(STUDY_ID)} AS VARCHAR) IN ({study_placeholders})")
        params.extend(study_ids)
    where = f" WHERE {' AND '.join(f'({item})' for item in predicates)}" if predicates else ""
    ordering = ", ".join(q(column) for column in selected_columns)
    return con.execute(
        f"SELECT {projection} FROM {quote_ident(domain.lower())} {where} ORDER BY {ordering}",
        params,
    )


__all__ = [
    "OBSERVATION_COLUMNS",
    "ObservationRecord",
    "ObservationResult",
    "extract_observations",
]
