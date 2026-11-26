"""Public cohort-query execution and explain facade."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
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
    sex_in_scope,
)
from .contract_helpers import SourceDataError
from .data_decisions import DataDecisions, load_data_decisions
from .fingerprint import fingerprint_observation_contract
from .endpoint_catalogue import canonical_endpoint, endpoint_catalogue_identity
from .duckdb import archive_source_identity, connect, open_archive, open_directory
from .cohort import CohortSelection, SubjectContext
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_PREDICATE_CONTRACT, SUBJECT_QUERY_CONTRACT
from .observation import ObservationResult, extract_observations
from .query import (
    SOURCE_STAGE,
    SUBJECT_CONTEXT_STAGE,
    SUPPORT_SELECTION_STAGE,
    CohortQuery,
    EndpointExclusion,
    EndpointObservations,
    StudyExclusion,
    StageInfo,
    StageRecord,
    StageWarning,
    StageDrop,
    SubjectQuery,
    TraceUnit,
    thaw,
    trace_items as sorted_trace_items,
)
from .set_execution import (
    PredicateEvaluation,
    evaluate_predicate,
    selection_for_keys,
    subject_universe,
)
from .source_validation import validate_active_source
from .unit_catalogue import _CompiledUnitRule, catalogue_identity, observation_unit_semantics
from .table_schema import OBSERVATIONS_TABLE, SUBJECTS_TABLE, normalize_result_table


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
    data_decisions: dict[str, Any]


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
    data_decisions: dict[str, Any]
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
        return sex_in_scope(self.sex_applicability, sex)


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


SUPPORT_DROP_REASONS: tuple[str, ...] = ("too_few_subjects", "missing_queried_sex")


@dataclass(frozen=True)
class StudySupport:
    """Which member studies the support lookup keeps, and why it excludes the others.

    ``dropped`` maps every excluded member study to one of ``SUPPORT_DROP_REASONS``.
    ``details`` holds public-safe facts about the rule (policy thresholds, basis).
    """

    eligible_studies: frozenset[str]
    dropped: Mapping[str, str]
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "eligible_studies", frozenset(self.eligible_studies))
        object.__setattr__(self, "dropped", dict(self.dropped))
        object.__setattr__(self, "details", dict(self.details))
        unknown = sorted({reason for reason in self.dropped.values() if reason not in SUPPORT_DROP_REASONS})
        if unknown:
            raise ValueError(f"unknown study support drop reasons: {unknown}")
        if self.eligible_studies & self.dropped.keys():
            raise ValueError("a study cannot be both eligible and dropped")

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

    def __post_init__(self) -> None:
        # Every reader can rely on the public table schema, whoever built the result.
        object.__setattr__(self, "subjects", normalize_result_table(self.subjects, SUBJECTS_TABLE))
        object.__setattr__(
            self, "observations", normalize_result_table(self.observations, OBSERVATIONS_TABLE)
        )
        _validate_result_tables(self)


@dataclass(frozen=True)
class _QueryState:
    """Products that one stage passes to the next, in stage order."""

    con: Any
    data_decisions: DataDecisions
    source_identity: str | None = None
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
            keys = self.observations.observed_subjects
        counts: dict[TraceUnit, int] = {
            "subjects": len(keys),
            "studies": len({study for study, _ in keys}),
        }
        if self.observations is not None:
            counts["observations"] = self.observations.record_count
        return counts


# Each step returns (state, details, warnings, drops) and, for a transform, also
# (n_changed, changes). A step must not change earlier products.
_Step = tuple[_QueryState, dict[str, Any], tuple[str, ...], tuple[StageDrop, ...]]


def _source_step(state: _QueryState, query: CohortQuery) -> _Step:
    validate_active_source(state.con, query.subject_query, query.resolved_endpoints)
    universe = subject_universe(state.con)
    return replace(state, universe_keys=universe), {}, (), ()


def _subject_query_step(
    state: _QueryState, query: SubjectQuery, membership: PredicateEvaluation, trace_items: bool
) -> _Step:
    # The membership comes from the support lookup's evaluation of the same predicate; the
    # execution never evaluates it again, so it must describe this source's universe.
    if membership.universe_keys != state.universe_keys:
        raise SourceDataError("the membership was evaluated on a different subject universe than the source")
    matched = set(membership.subject_keys)
    if len(matched) != len(membership.subject_keys):
        raise SourceDataError("the membership holds duplicate subject keys")
    if not matched.issubset(state.universe_keys):
        raise SourceDataError("the membership holds a subject outside the source's subject universe")
    details = {
        "parity_checkpoint": "grit42",
        "membership_hash": membership.membership_hash,
        "predicate_fingerprint": query.fingerprint,
        "assumptions": list(membership.assumptions),
        "nodes": [node.to_dict() for node in membership.nodes],
    }
    outside = [(study, subject) for study, subject in state.universe_keys if (study, subject) not in matched]
    unmatched = Counter(study for study, _ in outside)
    drops = (
        (StageDrop(
            "predicate_not_satisfied", "subjects", sum(unmatched.values()), dict(unmatched),
            sorted_trace_items({"study": study, "subject": subject} for study, subject in outside)
            if trace_items else None,
        ),)
        if unmatched
        else ()
    )
    return replace(state, evaluation=membership), details, membership.warnings, drops


def _study_exclusion_step(state: _QueryState, stage: StudyExclusion, trace_items: bool) -> _Step:
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
            StageDrop(
                "study_excluded", "subjects", sum(removed.values()), dict(removed),
                sorted_trace_items(
                    {"study": study, "subject": subject} for study, subject in state.members if study in excluded
                ) if trace_items else None,
            ),
            StageDrop(
                "study_excluded", "studies", len(removed), {study: 1 for study in removed},
                sorted_trace_items({"study": study} for study in removed) if trace_items else None,
            ),
        )
        if removed
        else ()
    )
    return replace(state, retained=retained), details, (), drops


def _support_selection_step(state: _QueryState, support: StudySupport, trace_items: bool) -> _Step:
    subjects_by_study = Counter(study for study, _ in state.members)
    excluded = {study for study in subjects_by_study if study not in support.eligible_studies}
    if set(support.dropped) != excluded:
        raise SourceDataError(
            "the study support must name exactly the retained member studies it excludes: "
            f"it names {len(support.dropped)} and the retained members hold {len(excluded)} ineligible"
        )
    retained = tuple(key for key in state.members if key[0] not in excluded)
    by_reason: dict[str, list[str]] = {}
    for study in sorted(excluded):
        by_reason.setdefault(support.dropped[study], []).append(study)
    details = {
        "studies_reviewed": len(subjects_by_study),
        "studies_excluded": len(excluded),
        "studies_by_reason": {reason: len(studies) for reason, studies in sorted(by_reason.items())},
        "support": thaw(support.details),
    }
    drops: list[StageDrop] = []
    for reason, studies in sorted(by_reason.items()):
        chosen = set(studies)
        drops.append(
            StageDrop(
                reason, "subjects", sum(subjects_by_study[study] for study in studies),
                {study: subjects_by_study[study] for study in studies},
                sorted_trace_items(
                    {"study": study, "subject": subject} for study, subject in state.members if study in chosen
                ) if trace_items else None,
            )
        )
        drops.append(
            StageDrop(
                reason, "studies", len(studies), {study: 1 for study in studies},
                sorted_trace_items({"study": study} for study in studies) if trace_items else None,
            )
        )
    return replace(state, retained=retained), details, (), tuple(drops)


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


def _endpoint_exclusion_step(state: _QueryState, stage: EndpointExclusion, trace_items: bool) -> _Step:
    removed = stage.removed
    details = {
        "reviewed": len(stage.entries),
        "exclusions": [entry.to_dict() for entry in removed],
    }
    drops = (
        (StageDrop(
            "endpoint_excluded", "endpoints", len(removed), {},
            sorted_trace_items({"endpoint": entry.endpoint} for entry in removed) if trace_items else None,
        ),)
        if removed
        else ()
    )
    return state, details, (), drops


def _endpoint_observations_step(state: _QueryState, stage: EndpointObservations, trace_items: bool) -> _Step:
    observations = extract_observations(
        state.con, state.selection, stage.resolved_endpoints, data_decisions=state.data_decisions,
        trace_items=trace_items,
    )
    details = {key: value for key, value in observations.to_receipt().items() if key != "warnings"}
    observed = observations.observed_subjects
    unobserved = [(study, subject) for study, subject in state.members
                  if (study, subject) not in observed]
    missing = Counter(study for study, _ in unobserved)
    drops = (
        (StageDrop(
            "no_observations", "subjects", sum(missing.values()), dict(missing),
            sorted_trace_items({"study": study, "subject": subject} for study, subject in unobserved)
            if trace_items else None,
        ),)
        if missing
        else ()
    ) + tuple(
        StageDrop(
            f"data_decision:{finding}", "observations", sum(by_study.values()), by_study,
            observations.decision_removal_items.get(finding, ()) if trace_items else None,
        )
        for finding, by_study in observations.decision_removals.items()
    )
    warnings = observations.warnings + (
        state.data_decisions.source_warnings(state.source_identity) if state.data_decisions.topics else ()
    )
    return replace(state, observations=observations), details, warnings, drops


_STAGE_STEPS = {
    StudyExclusion: _study_exclusion_step,
    EndpointExclusion: _endpoint_exclusion_step,
    EndpointObservations: _endpoint_observations_step,
}


def _run_stages(
    con: Any,
    query: CohortQuery,
    *,
    membership: PredicateEvaluation,
    support: StudySupport | None,
    data_decisions: DataDecisions,
    source_identity: str | None,
    trace_items: bool = False,
) -> tuple[_QueryState, tuple[StageRecord, ...]]:
    """Run the cohort query stage by stage and record what each stage did."""
    trace: list[StageRecord] = []

    def record(
        info: StageInfo, params: Mapping[str, Any], before: _QueryState, step: tuple[Any, ...]
    ) -> _QueryState:
        after, details, warnings, drops, *changed = step
        n_changed, changes = changed or ({}, ())
        trace.append(
            StageRecord(
                info, params, before.counts(), after.counts(), details, tuple(warnings), drops,
                n_changed=n_changed, changes=changes,
            )
        )
        return after

    state = _QueryState(con, data_decisions, source_identity)
    state = record(SOURCE_STAGE, _source_params(source_identity), state, _source_step(state, query))
    subject_query = query.subject_query
    state = record(
        subject_query.info,
        subject_query.params(),
        state,
        _subject_query_step(state, subject_query, membership, trace_items),
    )
    # Study exclusions come before subject context, so context is built for kept subjects only.
    for stage in (item for item in query.stages if isinstance(item, StudyExclusion)):
        state = record(stage.info, stage.params(), state, _STAGE_STEPS[type(stage)](state, stage, trace_items))
    if support is not None:
        state = record(
            SUPPORT_SELECTION_STAGE, {}, state, _support_selection_step(state, support, trace_items)
        )
    state = record(SUBJECT_CONTEXT_STAGE, {}, state, _subject_context_step(state))
    for stage in (item for item in query.stages if not isinstance(item, StudyExclusion)):
        step = _STAGE_STEPS[type(stage)](state, stage, trace_items)
        params = stage.params()
        if isinstance(stage, EndpointObservations):
            # The decisions apply while the rows are read, so their ids are known only after the step.
            # A step that reads no row (no endpoint or no subject) applies none.
            applied = step[0].observations.data_decisions.get("applied", {})
            params["data_decisions_applied"] = ", ".join(sorted(applied)) or "none"
        state = record(stage.info, params, state, step)
    return state, tuple(trace)


def _source_params(source_identity: str | None) -> dict[str, str]:
    """The source snapshot (when known) and the endpoint catalogue that every later stage reads."""

    catalogue = endpoint_catalogue_identity()
    params = {} if source_identity is None else {"source_snapshot": source_identity}
    return {**params, "endpoint_catalogue": catalogue["catalogue_id"], "endpoint_catalogue_md5": catalogue["md5"]}


def execute_query(
    source: Any,
    query: CohortQuery,
    *,
    membership: PredicateEvaluation,
    support: StudySupport | None = None,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
    source_data_identity: str | None = None,
    data_decisions: DataDecisions | None = None,
    trace_items: bool = False,
) -> QueryResult:
    """Run the stages after the subject query on a membership the caller already resolved.

    ``membership`` is the evaluation of ``query.subject_query`` on this source
    (:func:`evaluate_membership`); it must hold the source's subject
    universe. ``support`` is the support lookup's verdict on the member studies; when it is given
    the ``support_selection`` stage removes the studies it excludes before any row-level stage.
    """
    decisions = load_data_decisions() if data_decisions is None else data_decisions
    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query.subject_query, query.resolved_endpoints),
    )
    source_identity, source_warning = _source_identity(source, source_data_identity)
    try:
        state, trace = _run_stages(
            con, query, membership=membership, support=support, data_decisions=decisions,
            source_identity=source_identity, trace_items=trace_items,
        )
        evaluation, selection, observations = state.evaluation, state.selection, state.observations
        subjects = pd.DataFrame.from_records(
            (item.to_dict() for item in selection.subjects), columns=_SUBJECT_COLUMNS
        )
        _validate_subject_frame(subjects, state)
    finally:
        if opened and close_connection:
            con.close()
    warnings = tuple(
        warning.message for item in trace for warning in item.warnings
    ) + ((source_warning.message,) if source_warning else ())
    unit_semantics = observation_unit_semantics(query.resolved_endpoints)
    identity = {
        "observation_fingerprint": fingerprint_observation_contract(query.endpoint_stage, unit_semantics),
        "import_provenance": _import_provenance(query.subject_query),
        "unit_catalogue": catalogue_identity(),
        "unit_semantics": unit_semantics,
        "endpoint_catalogue": endpoint_catalogue_identity(),
        "data_decisions": decisions.identity(),
    }
    receipt = _receipt(
        query,
        evaluation,
        state.members,
        selection,
        source_identity,
        warnings,
        observations.to_receipt(),
        trace,
        deepcopy(identity),
    )
    observation_frame = observations.frame
    return QueryResult(
        subjects=subjects,
        observations=observation_frame,
        metadata={
            "contract": COHORT_QUERY_CONTRACT,
            "subject_query_contract": SUBJECT_QUERY_CONTRACT,
            "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
            "query_id": query.query_id,
            "predicate_fingerprint": query.subject_query.fingerprint,
            "observation_fingerprint": identity["observation_fingerprint"],
            "import_provenance": identity["import_provenance"],
            "source_identity": source_identity,
            "membership_hash": evaluation.membership_hash,
            "query": query.to_dict(),
            "subject_count": len(state.members),
            "unit_catalogue": identity["unit_catalogue"],
            "unit_semantics": identity["unit_semantics"],
            "endpoint_catalogue": identity["endpoint_catalogue"],
            "data_decisions": identity["data_decisions"],
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


def evaluate_membership(
    source: Any,
    query: SubjectQuery,
    *,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
) -> PredicateEvaluation:
    """Evaluate the subject query alone, at the Grit42 parity checkpoint.

    The result is the ``membership`` that :func:`execute_query` consumes.
    """
    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query),
    )
    try:
        return evaluate_predicate(con, query)
    finally:
        if opened and close_connection:
            con.close()


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
    identity: Mapping[str, Any],
) -> QueryReceipt:
    return {
        "query_id": query.query_id,
        "contract": COHORT_QUERY_CONTRACT,
        "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
        "predicate_fingerprint": query.subject_query.fingerprint,
        "observation_fingerprint": identity["observation_fingerprint"],
        "import_provenance": identity["import_provenance"],
        "source_identity": source_identity,
        "assumptions": list(evaluation.assumptions),
        "warnings": list(warnings),
        "membership_hash": evaluation.membership_hash,
        "subject_count": len(members),
        "study_count": len({study for study, _ in members}),
        "observations": dict(observation),
        "unit_catalogue": identity["unit_catalogue"],
        "unit_semantics": identity["unit_semantics"],
        "endpoint_catalogue": identity["endpoint_catalogue"],
        "data_decisions": identity["data_decisions"],
        "dose_duration_evidence": _dose_duration_evidence(selection),
        "stages": [item.to_dict() for item in trace],
    }


def _import_provenance(query: SubjectQuery) -> dict[str, str] | None:
    return None if query.import_provenance is None else query.import_provenance.to_canonical_dict()


def _source_identity(source: Any, override: str | None) -> tuple[str | None, StageWarning | None]:
    if override is not None:
        if not str(override).strip():
            raise ValueError("source_data_identity must be non-empty when supplied")
        return str(override), None
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file() and is_zipfile(path):
            return archive_source_identity(path), None
    return (
        None,
        StageWarning(
            "source_identity_unknown",
            "The source identity is unknown. Supply source_data_identity for a connection-backed execution.",
        ),
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
            domains.add(canonical_endpoint(str(endpoint)).spec.domain)
        if getattr(rule.field, "name", None) == "baseline_body_weight_g":
            domains.add("BW")
        if getattr(rule.field, "name", None) in {
            "disposition_categories", "terminal_sacrifice_offset_days",
        }:
            domains.add("DS")
    return domains


# Observation columns that copy the selected subject's values.
_SUBJECT_REFERENCE = ["study_id", "subject_id", "sample_id", "sex"]


def _validate_result_tables(result: QueryResult) -> None:
    """Check the rules that join the tables and the endpoint lists.

    The subject key is unique, non-blank and trimmed; each observation belongs
    to a subject, copies its sample id and sex, and has an endpoint that applies
    to that sex; observations and endpoint facts name exactly the declared endpoints.
    """

    subjects, observations = result.subjects, result.observations
    keys = subjects[list(SUBJECTS_TABLE.keys)]
    for name in keys:
        values = keys[name]
        if values.eq("").any() or values.ne(values.str.strip()).any():
            raise ValueError(f"subjects.{name} has a blank or untrimmed value")
    if keys.duplicated().any():
        raise ValueError(f"subjects repeat a {SUBJECTS_TABLE.keys} key")
    matched = observations[_SUBJECT_REFERENCE].merge(
        subjects[_SUBJECT_REFERENCE], how="left", indicator=True
    )
    if matched["_merge"].ne("both").any():
        raise ValueError(f"observations do not match a selected subject on {_SUBJECT_REFERENCE}")
    if not set(observations["endpoint_name"]) <= set(result.endpoint_names):
        raise ValueError("observations name an endpoint outside endpoint_names")
    facts = {item.endpoint_name: item for item in result.endpoint_facts}
    if len(facts) != len(result.endpoint_facts) or set(facts) != set(result.endpoint_names):
        raise ValueError("endpoint_facts must name each of endpoint_names exactly once")
    pairs = observations[["endpoint_name", "sex"]].drop_duplicates().itertuples(index=False)
    if not all(facts[name].applies_to_sex(None if pd.isna(sex) else sex) for name, sex in pairs):
        raise ValueError("observations hold an endpoint that does not apply to the subject's sex")


def _validate_subject_frame(subjects: pd.DataFrame, state: _QueryState) -> None:
    keys = tuple(
        (str(study_id), str(subject_id))
        for study_id, subject_id in zip(
            subjects["study_id"], subjects["subject_id"], strict=True
        )
    )
    # Equal keys have the same count and membership hash. Without a study exclusion
    # the retained membership is the predicate membership, the Grit42 parity checkpoint.
    if keys != state.members:
        raise SourceDataError("query result subject keys do not match the retained membership")


def _is_connection(value: Any) -> bool:
    return hasattr(value, "execute") and callable(value.execute)


__all__ = [
    "QueryMetadata",
    "QueryReceipt",
    "QueryMembership",
    "EndpointFacts",
    "QueryResult",
    "StudySupport",
    "execute_query",
    "evaluate_membership",
]
