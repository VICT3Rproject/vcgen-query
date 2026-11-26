"""Public cohort-query execution and explain facade."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, TypedDict
from zipfile import is_zipfile

import pandas as pd

from .contract import (
    EndpointSpec,
    QueryError,
    ident,
    machine_ident,
    normalized_texts,
    rules,
)
from .contract_helpers import SourceDataError
from .fingerprint import fingerprint_observation_contract
from .endpoint_catalogue import canonical_endpoint, endpoint_catalogue_identity
from .duckdb import archive_sha256, connect, open_archive, open_directory
from .cohort import CohortSelection, SubjectContext
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_PREDICATE_CONTRACT, SUBJECT_QUERY_CONTRACT
from .observation import OBSERVATION_COLUMNS, ObservationResult, extract_observations
from .query import (
    SOURCE_STAGE,
    SUBJECT_CONTEXT_STAGE,
    CohortQuery,
    EndpointExclusion,
    EndpointObservations,
    StudyExclusion,
    StageInfo,
    StageRecord,
    StageDrop,
    SubjectQuery,
    TraceUnit,
)
from .send_semantics import normalize_human_category
from .set_execution import (
    PredicateEvaluation,
    evaluate_predicate,
    membership_hash,
    selection_for_keys,
    subject_universe,
)
from .source_validation import validate_active_source
from .unit_catalogue import _CompiledUnitRule, catalogue_identity, observation_unit_semantics
from .table_schema import OBSERVATIONS_TABLE, SUBJECTS_TABLE, normalize_result_table


class EndpointMetadata(TypedDict, total=False):
    endpoint_name: str
    domain: str
    sex_applicability: list[str]
    canonical_unit: str
    conversion_rules: list[dict[str, Any]]
    unit_catalogue: dict[str, str]
    derivation_status: str
    parent_variables: list[str]
    derivation_scale: float
    derivation_formula: str
    modeling_role: str


class QueryMetadata(TypedDict):
    contract: Literal["vcgen.cohort_query.v7"]
    subject_query_contract: Literal["vcgen.subject_query.v1"]
    predicate_contract: Literal["vcgen.subject_predicate.v1"]
    query_id: str
    predicate_fingerprint: str
    observation_fingerprint: str
    import_provenance: dict[str, str] | None
    source_identity: str | None
    membership_hash: str
    query: dict[str, Any]
    subject_count: int
    unit_catalogue: dict[str, str]
    unit_semantics: dict[str, Any]
    endpoint_catalogue: dict[str, str]


class QueryReceipt(TypedDict):
    query_id: str
    contract: Literal["vcgen.cohort_query.v7"]
    predicate_contract: Literal["vcgen.subject_predicate.v1"]
    predicate_fingerprint: str
    observation_fingerprint: str
    import_provenance: dict[str, str] | None
    source_identity: str | None
    assumptions: list[str]
    warnings: list[str]
    membership_hash: str
    subject_count: int
    study_count: int
    observations: dict[str, Any]
    unit_catalogue: dict[str, str]
    unit_semantics: dict[str, Any]
    endpoint_catalogue: dict[str, str]
    dose_duration_evidence: dict[str, Any]
    stages: list[dict[str, Any]]


_SUBJECT_COLUMNS = tuple(f.name for f in fields(SubjectContext) if f.repr)


@dataclass(frozen=True)
class EndpointFacts:
    endpoint_name: str
    domain: str
    sex_applicability: tuple[str, ...] = ()
    target_unit: str | None = None
    compiled_unit_rules: tuple[_CompiledUnitRule, ...] = ()
    derivation_kind: Literal["primitive", "ratio"] = "primitive"
    derived_parents: tuple[str, ...] = ()
    derivation_scale: float | None = None

    def __post_init__(self) -> None:
        endpoint_name = machine_ident(self.endpoint_name, "endpoint_facts.endpoint_name")
        if endpoint_name in {"STUDY_ID", "SUBJECT_ID", "STUDY_DAY"}:
            raise QueryError("endpoint_facts.endpoint_name cannot use a daily key column.")
        if self.target_unit is not None and not isinstance(self.target_unit, str):
            raise QueryError("endpoint_facts.target_unit must be a string when supplied.")
        if any(not isinstance(rule, _CompiledUnitRule) for rule in self.compiled_unit_rules):
            raise QueryError("endpoint_facts.compiled_unit_rules contains an invalid rule.")
        if self.derivation_kind not in {"primitive", "ratio"}:
            raise QueryError("endpoint_facts.derivation_kind is unsupported.")
        if self.derivation_kind == "primitive" and (
            self.derived_parents or self.derivation_scale is not None
        ):
            raise QueryError("primitive endpoint facts cannot define derivation fields.")
        if self.derivation_kind == "ratio" and (
            len(self.derived_parents) != 2
            or self.derivation_scale is None
            or self.derivation_scale <= 0
        ):
            raise QueryError("ratio endpoint facts require two parents and a positive scale.")
        object.__setattr__(self, "endpoint_name", endpoint_name)
        object.__setattr__(self, "domain", ident(self.domain, "endpoint_facts.domain"))
        object.__setattr__(
            self,
            "sex_applicability",
            normalized_texts(self.sex_applicability, "endpoint_facts.sex_applicability"),
        )

    def applies_to_sex(self, sex: str | None) -> bool:
        normalized = normalize_human_category(sex)
        return not self.sex_applicability or normalized in self.sex_applicability

    def metadata(self) -> EndpointMetadata:
        metadata: EndpointMetadata = {
            "endpoint_name": self.endpoint_name,
            "domain": self.domain,
            "sex_applicability": list(self.sex_applicability)
            if self.sex_applicability
            else ["ALL"],
        }
        if self.target_unit is not None:
            metadata["canonical_unit"] = self.target_unit
            metadata["conversion_rules"] = [
                rule.to_canonical_dict() for rule in self.compiled_unit_rules
            ]
            metadata["unit_catalogue"] = catalogue_identity()
        metadata["derivation_status"] = (
            "primitive_observed"
            if self.derivation_kind == "primitive"
            else "derived_ratio"
        )
        metadata["parent_variables"] = list(self.derived_parents)
        metadata["modeling_role"] = (
            "modeled" if self.derivation_kind == "primitive" else "derived_postprocess"
        )
        if self.derivation_kind == "ratio":
            metadata["derivation_scale"] = float(self.derivation_scale)
            metadata["derivation_formula"] = "scale * numerator / denominator"
        return metadata


@dataclass(frozen=True)
class QueryMembership:
    """Supported same-execution handoff for parity and source validation.

    The keys come from the exact predicate evaluation that produced the
    associated :class:`QueryResult`. Consumers must not treat them as a second
    query result or expose subject-level keys in aggregate reports.
    """

    subject_keys: tuple[tuple[str, str], ...]
    universe_keys: tuple[tuple[str, str], ...]
    membership_hash: str

    @classmethod
    def from_evaluation(cls, evaluation: PredicateEvaluation) -> QueryMembership:
        return cls(
            subject_keys=evaluation.subject_keys,
            universe_keys=evaluation.universe_keys,
            membership_hash=evaluation.membership_hash,
        )


@dataclass(frozen=True)
class QueryResult:
    """DataFrame-first query output with typed provenance and endpoint facts."""

    subjects: pd.DataFrame
    observations: pd.DataFrame
    metadata: QueryMetadata
    receipt: QueryReceipt
    endpoint_names: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    endpoint_facts: tuple[EndpointFacts, ...] = ()
    membership: QueryMembership | None = None
    trace: tuple[StageRecord, ...] = ()


@dataclass(frozen=True)
class _QueryState:
    """Products that one stage passes to the next, in stage order."""

    con: Any
    universe_keys: tuple[tuple[str, str], ...] | None = None
    evaluation: PredicateEvaluation | None = None
    retained: tuple[tuple[str, str], ...] | None = None
    selection: CohortSelection | None = None
    observations: ObservationResult | None = None

    @property
    def members(self) -> tuple[tuple[str, str], ...]:
        """Subjects still in the cohort: the matched set minus any study exclusion."""
        return self.evaluation.subject_keys if self.retained is None else self.retained

    def counts(self) -> dict[TraceUnit, int]:
        if self.universe_keys is None:
            return {}
        keys = self.universe_keys if self.evaluation is None else self.members
        if self.observations is not None:
            # After extraction a subject stays in the pool only when it has observations.
            keys = {(record.study_id, record.subject_id) for record in self.observations.records}
        counts: dict[TraceUnit, int] = {
            "subjects": len(keys),
            "studies": len({study for study, _ in keys}),
        }
        if self.observations is not None:
            counts["observations"] = self.observations.record_count
        return counts


# Each step returns (state, details, warnings, drops). A step must not change earlier products.
_Step = tuple[_QueryState, dict[str, Any], tuple[str, ...], tuple[StageDrop, ...]]


def _source_step(state: _QueryState, query: CohortQuery) -> _Step:
    validate_active_source(state.con, query.subject_query, query.resolved_endpoints)
    universe = subject_universe(state.con)
    return replace(state, universe_keys=universe), {}, (), ()


def _subject_query_step(state: _QueryState, query: SubjectQuery) -> _Step:
    evaluation = evaluate_predicate(state.con, query, validate_source=False)
    if evaluation.universe_keys != state.universe_keys:
        raise SourceDataError("predicate universe does not match the source subject universe")
    details = {
        "parity_checkpoint": "grit42",
        "membership_hash": evaluation.membership_hash,
        "predicate_fingerprint": query.fingerprint,
        "assumptions": list(evaluation.assumptions),
        "nodes": [node.to_dict(include_subject_keys=False) for node in evaluation.nodes],
    }
    matched = set(evaluation.subject_keys)
    unmatched = Counter(study for study, subject in state.universe_keys if (study, subject) not in matched)
    drops = (
        (StageDrop("predicate_not_satisfied", "subjects", sum(unmatched.values()), dict(unmatched)),)
        if unmatched
        else ()
    )
    return replace(state, evaluation=evaluation), details, evaluation.warnings, drops


def _study_exclusion_step(state: _QueryState, stage: StudyExclusion) -> _Step:
    excluded = {entry.study_id: entry for entry in stage.entries}
    removed = Counter(study for study, _ in state.members if study in excluded)
    retained = tuple(key for key in state.members if key[0] not in excluded)
    details = {
        "reviewed": len(stage.entries),
        "exclusions": [
            {**excluded[study].to_dict(), "subjects": count}
            for study, count in sorted(removed.items(), key=lambda item: excluded[item[0]].ref)
        ],
    }
    drops = (
        (
            StageDrop("study_excluded", "subjects", sum(removed.values()), dict(removed)),
            StageDrop("study_excluded", "studies", len(removed), {study: 1 for study in removed}),
        )
        if removed
        else ()
    )
    return replace(state, retained=retained), details, (), drops


def _subject_context_step(state: _QueryState) -> _Step:
    evaluation = state.evaluation
    selection = selection_for_keys(
        state.con, state.members, universe_keys=evaluation.universe_keys
    )
    details = {
        "subjects_by_study": dict(sorted(Counter(study for study, _ in state.members).items())),
        "dose_duration_evidence": _dose_duration_evidence(selection),
    }
    return replace(state, selection=selection), details, selection.warnings, ()


def _endpoint_exclusion_step(state: _QueryState, stage: EndpointExclusion) -> _Step:
    removed = stage.removed
    details = {
        "reviewed": len(stage.entries),
        "exclusions": [entry.to_dict() for entry in removed],
    }
    drops = (StageDrop("endpoint_excluded", "endpoints", len(removed)),) if removed else ()
    return state, details, (), drops


def _endpoint_observations_step(state: _QueryState, stage: EndpointObservations) -> _Step:
    observations = extract_observations(state.con, state.selection, stage.resolved_endpoints)
    details = {key: value for key, value in observations.to_receipt().items() if key != "warnings"}
    observed = {(record.study_id, record.subject_id) for record in observations.records}
    missing = Counter(study for study, subject in state.members if (study, subject) not in observed)
    drops = (
        (StageDrop("no_observations", "subjects", sum(missing.values()), dict(missing)),)
        if missing
        else ()
    )
    return replace(state, observations=observations), details, observations.warnings, drops


_STAGE_STEPS = {
    StudyExclusion: _study_exclusion_step,
    EndpointExclusion: _endpoint_exclusion_step,
    EndpointObservations: _endpoint_observations_step,
}


def _run_stages(con: Any, query: CohortQuery) -> tuple[_QueryState, tuple[StageRecord, ...]]:
    """Run the cohort query stage by stage and record what each stage did."""
    trace: list[StageRecord] = []

    def record(
        info: StageInfo, params: Mapping[str, Any], before: _QueryState, step: _Step
    ) -> _QueryState:
        after, details, warnings, drops = step
        trace.append(
            StageRecord(
                info, params, before.counts(), after.counts(), details, tuple(warnings), drops
            )
        )
        return after

    state = _QueryState(con)
    state = record(SOURCE_STAGE, {}, state, _source_step(state, query))
    subject_query = query.subject_query
    state = record(
        subject_query.info,
        subject_query.params(),
        state,
        _subject_query_step(state, subject_query),
    )
    # Study exclusions come before subject context, so context is built for kept subjects only.
    for stage in (item for item in query.stages if isinstance(item, StudyExclusion)):
        state = record(stage.info, stage.params(), state, _STAGE_STEPS[type(stage)](state, stage))
    state = record(SUBJECT_CONTEXT_STAGE, {}, state, _subject_context_step(state))
    for stage in (item for item in query.stages if not isinstance(item, StudyExclusion)):
        state = record(stage.info, stage.params(), state, _STAGE_STEPS[type(stage)](state, stage))
    return state, tuple(trace)


def execute_query(
    source: Any,
    query: CohortQuery,
    *,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
    source_data_identity: str | None = None,
) -> QueryResult:
    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query.subject_query, query.resolved_endpoints),
    )
    source_identity, source_warning = _source_identity(source, source_data_identity)
    try:
        state, trace = _run_stages(con, query)
        evaluation, selection, observations = state.evaluation, state.selection, state.observations
        subjects = normalize_result_table(
            pd.DataFrame.from_records(
                (item.to_dict() for item in selection.subjects),
                columns=_SUBJECT_COLUMNS,
            ),
            SUBJECTS_TABLE,
        )
        _validate_subject_frame(subjects, state)
    finally:
        if opened and close_connection:
            con.close()
    warnings = tuple(
        warning for item in trace for warning in item.warnings
    ) + ((source_warning,) if source_warning else ())
    receipt = _receipt(
        query,
        evaluation,
        state.members,
        selection,
        source_identity,
        warnings,
        observations.to_receipt(),
        trace,
    )
    observation_rows = (item.to_dict() for item in observations.records)
    observation_frame = normalize_result_table(
        pd.DataFrame.from_records(
            observation_rows,
            columns=OBSERVATION_COLUMNS,
        ),
        OBSERVATIONS_TABLE,
    )
    return QueryResult(
        subjects=subjects,
        observations=observation_frame,
        metadata={
            "contract": COHORT_QUERY_CONTRACT,
            "subject_query_contract": SUBJECT_QUERY_CONTRACT,
            "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
            "query_id": query.query_id,
            "predicate_fingerprint": query.subject_query.fingerprint,
            "observation_fingerprint": fingerprint_observation_contract(query.endpoint_stage),
            "import_provenance": _import_provenance(query.subject_query),
            "source_identity": source_identity,
            "membership_hash": evaluation.membership_hash,
            "query": query.to_dict(),
            "subject_count": len(state.members),
            "unit_catalogue": catalogue_identity(),
            "unit_semantics": observation_unit_semantics(query.resolved_endpoints),
            "endpoint_catalogue": endpoint_catalogue_identity(),
        },
        receipt=receipt,
        endpoint_names=observations.endpoint_names,
        endpoint_facts=tuple(
            EndpointFacts(
                endpoint_name=item.name,
                domain=item.domain,
                sex_applicability=item.sexes,
                target_unit=item.target_unit,
                compiled_unit_rules=item.compiled_unit_rules,
                derivation_kind=canonical.derivation.kind,
                derived_parents=canonical.derivation.parents,
                derivation_scale=canonical.derivation.scale,
            )
            for item in query.resolved_endpoints
            for canonical in (canonical_endpoint(item.name),)
        ),
        warnings=warnings,
        membership=QueryMembership.from_evaluation(evaluation),
        trace=trace,
    )


def explain_query(
    source: Any,
    query: SubjectQuery,
    *,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
    include_subject_keys: bool = False,
    source_data_identity: str | None = None,
) -> dict[str, Any]:
    """Return the subject-query evaluation: the Grit42 parity checkpoint."""
    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query),
    )
    source_identity, source_warning = _source_identity(source, source_data_identity)
    try:
        evaluation = evaluate_predicate(con, query)
    finally:
        if opened and close_connection:
            con.close()
    warnings = (
        evaluation.warnings + ((source_warning,) if source_warning else ())
    )
    return {
        "contract": SUBJECT_QUERY_CONTRACT,
        "stage": query.info.id,
        "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
        "query_id": query.query_id,
        "predicate_fingerprint": query.fingerprint,
        "import_provenance": _import_provenance(query),
        "source_identity": source_identity,
        "assumptions": list(evaluation.assumptions),
        "warnings": list(warnings),
        "membership_hash": evaluation.membership_hash,
        "subject_count": len(evaluation.subject_keys),
        "matched_subject_count": len(evaluation.subject_keys),
        "candidate_subject_count": len(evaluation.universe_keys),
        "nodes": [
            node.to_dict(include_subject_keys=include_subject_keys) for node in evaluation.nodes
        ],
    }


def evaluate_query_membership(
    source: Any,
    query: SubjectQuery,
    *,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
) -> dict[str, Any]:
    """Evaluate the subject query alone, at the Grit42 parity checkpoint.

    Normal consumers should use ``execute_query(...).membership`` so that
    membership and observations are guaranteed to come from one execution.
    """

    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query),
    )
    try:
        evaluation = evaluate_predicate(con, query)
    finally:
        if opened and close_connection:
            con.close()
    membership = QueryMembership.from_evaluation(evaluation)
    return {
        "stage": query.info.id,
        "predicate_fingerprint": query.fingerprint,
        "subject_keys": membership.subject_keys,
        "universe_keys": membership.universe_keys,
        "membership_hash": membership.membership_hash,
    }


def _dose_duration_evidence(selection: CohortSelection) -> dict[str, Any]:
    return {
        "status_counts": dict(selection.dose_duration_evidence_status_counts),
        "exposure_interval_status_counts": dict(selection.exposure_interval_status_counts),
        "incomplete_declaration_subject_count": (
            selection.dose_duration_declaration_incomplete_subject_count
        ),
    }


def _receipt(
    query: CohortQuery,
    evaluation: PredicateEvaluation,
    members: tuple[tuple[str, str], ...],
    selection: CohortSelection,
    source_identity: str | None,
    warnings: tuple[str, ...],
    observation: Mapping[str, Any],
    trace: tuple[StageRecord, ...],
) -> QueryReceipt:
    return {
        "query_id": query.query_id,
        "contract": COHORT_QUERY_CONTRACT,
        "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
        "predicate_fingerprint": query.subject_query.fingerprint,
        "observation_fingerprint": fingerprint_observation_contract(query.endpoint_stage),
        "import_provenance": _import_provenance(query.subject_query),
        "source_identity": source_identity,
        "assumptions": list(evaluation.assumptions),
        "warnings": list(warnings),
        "membership_hash": evaluation.membership_hash,
        "subject_count": len(members),
        "study_count": len({study for study, _ in members}),
        "observations": dict(observation),
        "unit_catalogue": catalogue_identity(),
        "unit_semantics": observation_unit_semantics(query.resolved_endpoints),
        "endpoint_catalogue": endpoint_catalogue_identity(),
        "dose_duration_evidence": _dose_duration_evidence(selection),
        "stages": [item.to_dict() for item in trace],
    }


def _import_provenance(query: SubjectQuery) -> dict[str, str] | None:
    return None if query.import_provenance is None else query.import_provenance.to_canonical_dict()


def _source_identity(source: Any, override: str | None) -> tuple[str | None, str | None]:
    if override is not None:
        if not str(override).strip():
            raise ValueError("source_data_identity must be non-empty when supplied")
        return str(override), None
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file() and is_zipfile(path):
            return f"archive-sha256:{archive_sha256(path)}", None
    return (
        None,
        "source identity is unknown; supply source_data_identity for connection-backed execution",
    )


def _open_source(
    source: Any,
    *,
    extract_dir: str | Path | None,
    database: str | Path | None,
    domains: set[str],
) -> tuple[Any, bool]:
    if _is_connection(source):
        return source, False
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            return open_directory(path, domains=domains, database=database), True
        if path.is_file() and path.suffix.lower() in {".duckdb", ".db"}:
            return connect(path, read_only=True), True
        if is_zipfile(path):
            return open_archive(
                path,
                Path(extract_dir) if extract_dir else path.parent / ".vcgen_query_cache",
                database=database,
            ), True
        if path.exists():
            raise ValueError(f"source {path} is neither a directory nor a zip archive")
        raise FileNotFoundError(f"source not found: {path}")
    raise TypeError("source must be an open DuckDB connection, CSV directory, or SEND zip archive")


def _query_domains(query: SubjectQuery, endpoints: Sequence[EndpointSpec] = ()) -> set[str]:
    """Return only raw domains needed by the subject query and the endpoint stage."""
    domains = {endpoint.domain for endpoint in endpoints}
    for rule in rules(query.predicate):
        domain = getattr(rule.field, "domain", None)
        if domain is not None:
            domains.add(str(domain).upper())
        endpoint = getattr(rule.field, "endpoint", None)
        if endpoint is not None:
            from .endpoint_catalogue import canonical_endpoint

            domains.add(canonical_endpoint(str(endpoint)).spec.domain)
        if getattr(rule.field, "name", None) == "baseline_body_weight_g":
            domains.add("BW")
        if getattr(rule.field, "name", None) in {
            "disposition_categories", "terminal_sacrifice_offset_days",
        }:
            domains.add("DS")
    return domains


def _validate_subject_frame(subjects: pd.DataFrame, state: _QueryState) -> None:
    evaluation = state.evaluation
    keys = tuple(
        (str(study_id), str(subject_id))
        for study_id, subject_id in zip(
            subjects["study_id"], subjects["subject_id"], strict=True
        )
    )
    if len(keys) != len(set(keys)):
        raise SourceDataError("query result contains duplicate subject keys")
    if len(keys) != len(state.members):
        raise SourceDataError("query result subject count does not match the retained membership")
    if keys != state.members:
        raise SourceDataError("query result subject keys do not match the retained membership")
    # The membership hash is the Grit42 parity checkpoint; a study exclusion changes the
    # cohort after it, so the hash then identifies the matched set, not the result rows.
    if state.retained is None and membership_hash(keys) != evaluation.membership_hash:
        raise SourceDataError("query result membership hash does not match predicate membership")


def _is_connection(value: Any) -> bool:
    return hasattr(value, "execute") and callable(value.execute)


__all__ = [
    "EndpointMetadata",
    "QueryMetadata",
    "QueryReceipt",
    "QueryMembership",
    "EndpointFacts",
    "QueryResult",
    "execute_query",
    "evaluate_query_membership",
    "explain_query",
]
