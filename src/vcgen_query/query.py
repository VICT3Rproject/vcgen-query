"""Staged cohort query: the Grit42 subject query, then the VCGen stages after it.

A cohort query is an ordered list of stage blocks. The first block is always
the :class:`SubjectQuery`. It is the Grit42-compatible layer: Grit42 sends it
over the API, we keep it exactly as written, and its output membership is the
Grit42 parity checkpoint. The blocks after it are VCGen stages, for example
:class:`EndpointObservations`. To add a query stage, define one block class
with ``info``, ``params()``, ``to_dict()`` and ``from_mapping()``, register it
in ``_STAGE_TYPES``, and add its runner in :mod:`vcgen_query.result`.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, ClassVar, Literal, TypeAlias

from .contract import (
    EndpointSpec,
    Predicate,
    PredicateGroup,
    PredicateRule,
    QueryError,
    QueryImportProvenance,
    keys,
    machine_ident,
    text,
    mapping,
    parse_predicate,
    rules,
    seq,
)
from .endpoint_catalogue import endpoint_catalogue_identity, resolve_endpoint_specs
from .fingerprint import fingerprint_predicate
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_QUERY_CONTRACT

StageKind: TypeAlias = Literal[
    "source", "query", "extract", "select", "filter", "transform", "model"
]
StageOrigin: TypeAlias = Literal["grit42", "vcgen"]
TraceUnit: TypeAlias = Literal["subjects", "studies", "observations", "cells", "rows", "endpoints"]


@dataclass(frozen=True)
class StageInfo:
    """Display and audit metadata that every stage block carries."""

    id: str
    name: str
    kind: StageKind
    origin: StageOrigin
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "origin": self.origin,
            "description": self.description,
        }


@dataclass(frozen=True)
class StageDrop:
    """Items that one stage removed for one reason, with per-study counts."""

    reason: str
    unit: TraceUnit
    count: int
    by_study: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "unit": self.unit,
            "count": self.count,
            "by_study": dict(sorted(self.by_study.items())),
        }


@dataclass(frozen=True)
class StageChange:
    """Items that one stage modified, but kept, for one effect, with per-study counts."""

    effect: str
    unit: TraceUnit
    count: int
    by_study: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "unit": self.unit,
            "count": self.count,
            "by_study": dict(sorted(self.by_study.items())),
        }


# Why a stage refused a request. The codes are a closed set, so a refusal can be
# counted by category; the description is for the user (a toxicologist or study
# director) and states the numbers behind the refusal.
REFUSAL_CODES: tuple[str, ...] = (
    "no_matching_subjects",
    "no_requested_design",
    "missing_required_sex",
    "too_few_donor_studies",
    "too_few_eligible_studies",
    "too_few_completed_folds",
)


@dataclass(frozen=True)
class StageRefusal:
    """A coded, user-readable reason why a stage cannot continue."""

    code: str
    description: str

    def __post_init__(self) -> None:
        if self.code not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {self.code!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("a refusal needs a description")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "description": self.description}


@dataclass(frozen=True)
class StageRecord:
    """What one stage did in one execution: counts in and out, drops, changes, and details.

    ``n_changed`` counts the distinct kept items that the stage modified, per unit
    (a subject counts once however many of its cells changed). ``changes`` splits
    the modified items by effect.
    """

    info: StageInfo
    params: Mapping[str, Any]
    n_in: Mapping[TraceUnit, int]
    n_out: Mapping[TraceUnit, int]
    details: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    drops: tuple[StageDrop, ...] = ()
    refusal: StageRefusal | None = None
    n_changed: Mapping[TraceUnit, int] = field(default_factory=dict)
    changes: tuple[StageChange, ...] = ()

    @property
    def outcome(self) -> Literal["passed", "refused"]:
        return "passed" if self.refusal is None else "refused"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.info.to_dict(),
            "params": dict(self.params),
            "n_in": dict(self.n_in),
            "n_out": dict(self.n_out),
            "drops": [drop.to_dict() for drop in self.drops],
            "n_changed": dict(self.n_changed),
            "changes": [change.to_dict() for change in self.changes],
            "details": dict(self.details),
            "warnings": list(self.warnings),
            "outcome": self.outcome,
            "refusal": None if self.refusal is None else self.refusal.to_dict(),
        }


SOURCE_STAGE = StageInfo(
    "source",
    "Source",
    "source",
    "vcgen",
    "Open the SEND source, validate subject identities, and form the DM subject universe.",
)
SUBJECT_CONTEXT_STAGE = StageInfo(
    "subject_context",
    "Subject context",
    "extract",
    "vcgen",
    "Attach species, strain, sex, study design, exposure, facility and baseline "
    "context to each matched subject.",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class SubjectQuery:
    """The Grit42-compatible subject query: who is in the cohort, nothing else.

    ``to_dict()`` returns the submitted document unchanged. Execution uses the
    parsed predicate; fingerprints use its order-free canonical form.
    """

    info: ClassVar[StageInfo] = StageInfo(
        "subject_query",
        "Subject query",
        "query",
        "grit42",
        "Evaluate the Grit42-compatible predicate against the DM universe. "
        "The output membership is the Grit42 parity checkpoint.",
    )
    contract: ClassVar[str] = SUBJECT_QUERY_CONTRACT

    query_id: str
    predicate: Predicate | None = None
    import_provenance: QueryImportProvenance | None = None
    document: Mapping[str, Any] = field(default=None, compare=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or not self.query_id.strip():
            raise QueryError("query_id must be a non-empty string.")
        if self.predicate is not None and not isinstance(
            self.predicate, (PredicateRule, PredicateGroup)
        ):
            raise QueryError("predicate must be a typed predicate when supplied.")
        if self.import_provenance is not None and not isinstance(
            self.import_provenance, QueryImportProvenance
        ):
            raise QueryError("import_provenance must be QueryImportProvenance when supplied.")
        document = self.document
        if document is None:
            document = {"contract": self.contract, "query_id": self.query_id}
            if self.predicate is not None:
                document["predicate"] = self.predicate.to_dict()
            if self.import_provenance is not None:
                document["import_provenance"] = self.import_provenance.to_canonical_dict()
        object.__setattr__(self, "document", _freeze(document))

    @classmethod
    def from_mapping(cls, value: Any) -> SubjectQuery:
        raw = mapping(value, "subject_query")
        keys(raw, {"contract", "query_id", "predicate", "import_provenance"}, "subject_query")
        if raw.get("contract") != SUBJECT_QUERY_CONTRACT:
            raise QueryError(
                f"unsupported contract {raw.get('contract')!r}; expected {SUBJECT_QUERY_CONTRACT!r}."
            )
        if "query_id" not in raw:
            raise QueryError("subject_query is missing required key 'query_id'.")
        return cls(
            raw["query_id"],
            None if raw.get("predicate") is None else parse_predicate(raw["predicate"]),
            None
            if raw.get("import_provenance") is None
            else QueryImportProvenance.from_mapping(raw["import_provenance"]),
            copy.deepcopy(dict(raw)),
        )

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.document)

    @property
    def fingerprint(self) -> str:
        return fingerprint_predicate(self.predicate)

    def params(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "predicate_fingerprint": self.fingerprint,
            "rule_count": len(rules(self.predicate)),
            "import_source": None
            if self.import_provenance is None
            else self.import_provenance.source_contract,
        }


@dataclass(frozen=True)
class EndpointObservations:
    """Select endpoints and extract their SEND observations for the matched subjects."""

    info: ClassVar[StageInfo] = StageInfo(
        "endpoint_observations",
        "Endpoint observations",
        "extract",
        "vcgen",
        "Extract SEND observations for the selected endpoints. Match test codes, "
        "specimens and methods, apply sex applicability, and convert units.",
    )

    requested_endpoints: tuple[str, ...]

    def __post_init__(self) -> None:
        if not seq(self.requested_endpoints) or not self.requested_endpoints:
            raise QueryError("requested_endpoints must be a non-empty array.")
        names = tuple(
            machine_ident(value, f"requested_endpoints[{index}]")
            for index, value in enumerate(self.requested_endpoints)
        )
        if len(names) != len(set(names)):
            raise QueryError("requested_endpoints must contain unique names.")
        resolve_endpoint_specs(names)
        object.__setattr__(self, "requested_endpoints", names)

    @classmethod
    def from_mapping(cls, value: Any) -> EndpointObservations:
        raw = mapping(value, "endpoint_observations")
        keys(raw, {"stage", "requested_endpoints", "endpoint_catalogue"}, "endpoint_observations")
        if dict(mapping(raw.get("endpoint_catalogue"), "endpoint_catalogue")) != (
            endpoint_catalogue_identity()
        ):
            raise QueryError(
                "endpoint_catalogue identity does not match the installed package catalogue."
            )
        requested = raw.get("requested_endpoints")
        return cls(tuple(requested) if seq(requested) else requested)

    @property
    def resolved_endpoints(self) -> tuple[EndpointSpec, ...]:
        return resolve_endpoint_specs(self.requested_endpoints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.info.id,
            "requested_endpoints": list(self.requested_endpoints),
            "endpoint_catalogue": endpoint_catalogue_identity(),
        }

    def params(self) -> dict[str, Any]:
        return {
            "requested_endpoints": list(self.requested_endpoints),
            "endpoint_catalogue": endpoint_catalogue_identity()["catalogue_id"],
        }


_EXCLUSION_REF = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _note(value: Any, location: str, *, hidden: tuple[str, ...] = ()) -> str:
    note = text(value, location, nonempty=True).strip()
    if not note:
        raise QueryError(f"{location} must be a non-empty string.")
    lowered = note.lower()
    if any(item and item.lower() in lowered for item in hidden):
        raise QueryError(f"{location} must not repeat the identifier it explains.")
    return note


@dataclass(frozen=True)
class StudyExclusionEntry:
    """One excluded source study: its identifier, a public opaque reference, and why.

    ``study_id`` is the raw source StudyID and stays in the full trace and
    manifests. ``ref`` and ``note`` are the only parts a public view may show.
    """

    study_id: str
    ref: str
    note: str

    def __post_init__(self) -> None:
        study_id = text(self.study_id, "study_exclusion.study_id", nonempty=True)
        if study_id != study_id.strip():
            raise QueryError("study_exclusion.study_id must not have surrounding whitespace.")
        if not isinstance(self.ref, str) or not _EXCLUSION_REF.fullmatch(self.ref):
            raise QueryError("study_exclusion.ref must be a lowercase label such as 'study-a'.")
        if study_id.lower() in self.ref:
            raise QueryError("study_exclusion.ref must not contain the study identifier.")
        object.__setattr__(
            self, "note", _note(self.note, "study_exclusion.note", hidden=(study_id,))
        )

    @classmethod
    def from_mapping(cls, value: Any) -> StudyExclusionEntry:
        raw = mapping(value, "study_exclusion entry")
        keys(raw, {"study_id", "ref", "note"}, "study_exclusion entry")
        return cls(raw.get("study_id"), raw.get("ref"), raw.get("note"))

    def to_dict(self) -> dict[str, str]:
        return {"study_id": self.study_id, "ref": self.ref, "note": self.note}


@dataclass(frozen=True)
class EndpointExclusionEntry:
    """One excluded canonical endpoint and why. Endpoint names are not identifying."""

    endpoint: str
    note: str

    def __post_init__(self) -> None:
        name = machine_ident(self.endpoint, "endpoint_exclusion.endpoint")
        resolve_endpoint_specs((name,))
        object.__setattr__(self, "note", _note(self.note, "endpoint_exclusion.note"))

    @classmethod
    def from_mapping(cls, value: Any) -> EndpointExclusionEntry:
        raw = mapping(value, "endpoint_exclusion entry")
        keys(raw, {"endpoint", "note"}, "endpoint_exclusion entry")
        return cls(raw.get("endpoint"), raw.get("note"))

    def to_dict(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "note": self.note}


def _entry_tuple(value: Any, entry_type: type, location: str) -> tuple[Any, ...]:
    if not seq(value):
        raise QueryError(f"{location} must be an array.")
    return tuple(item if isinstance(item, entry_type) else entry_type.from_mapping(item) for item in value)


@dataclass(frozen=True)
class StudyExclusion:
    """Remove the subjects of curated excluded studies from the matched cohort.

    It runs after the subject query, which stays the Grit42 parity checkpoint,
    and before subject context is built. An empty entry list is a recorded
    review with nothing excluded.
    """

    info: ClassVar[StageInfo] = StageInfo(
        "study_exclusion",
        "Study exclusions",
        "filter",
        "vcgen",
        "Remove subjects of studies excluded by curation. The Grit42 membership "
        "is unchanged; each exclusion carries the reason it was made.",
    )

    entries: tuple[StudyExclusionEntry, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(sorted(_entry_tuple(self.entries, StudyExclusionEntry, "study_exclusion.entries"),
                               key=lambda item: item.ref))
        for label, values in (
            ("study identifiers", [item.study_id for item in entries]),
            ("references", [item.ref for item in entries]),
        ):
            if len(values) != len(set(values)):
                raise QueryError(f"study_exclusion.entries must have unique {label}.")
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_mapping(cls, value: Any) -> StudyExclusion:
        raw = mapping(value, "study_exclusion")
        keys(raw, {"stage", "entries"}, "study_exclusion")
        return cls(raw.get("entries"))

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.info.id, "entries": [item.to_dict() for item in self.entries]}

    def params(self) -> dict[str, Any]:
        return {"entries_reviewed": len(self.entries)}


@dataclass(frozen=True)
class EndpointExclusion:
    """Remove curated excluded endpoints from the submitted endpoint selection.

    ``requested_endpoints`` is the selection as submitted. The following
    :class:`EndpointObservations` stage holds what is left, which this stage
    checks, so a document cannot disagree with its own record of the removal.
    """

    info: ClassVar[StageInfo] = StageInfo(
        "endpoint_exclusion",
        "Endpoint exclusions",
        "select",
        "vcgen",
        "Remove requested endpoints excluded by curation before extraction. "
        "Each exclusion carries the reason it was made.",
    )

    requested_endpoints: tuple[str, ...]
    entries: tuple[EndpointExclusionEntry, ...] = ()

    def __post_init__(self) -> None:
        if not seq(self.requested_endpoints) or not self.requested_endpoints:
            raise QueryError("endpoint_exclusion.requested_endpoints must be a non-empty array.")
        names = tuple(
            machine_ident(value, f"endpoint_exclusion.requested_endpoints[{index}]")
            for index, value in enumerate(self.requested_endpoints)
        )
        if len(names) != len(set(names)):
            raise QueryError("endpoint_exclusion.requested_endpoints must contain unique names.")
        entries = tuple(sorted(_entry_tuple(self.entries, EndpointExclusionEntry, "endpoint_exclusion.entries"),
                               key=lambda item: item.endpoint))
        if len({item.endpoint for item in entries}) != len(entries):
            raise QueryError("endpoint_exclusion.entries must have unique endpoints.")
        object.__setattr__(self, "requested_endpoints", names)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_mapping(cls, value: Any) -> EndpointExclusion:
        raw = mapping(value, "endpoint_exclusion")
        keys(raw, {"stage", "requested_endpoints", "entries"}, "endpoint_exclusion")
        requested = raw.get("requested_endpoints")
        return cls(tuple(requested) if seq(requested) else requested, raw.get("entries"))

    @property
    def removed(self) -> tuple[EndpointExclusionEntry, ...]:
        """Entries that matched a submitted endpoint, in submitted order."""
        by_name = {item.endpoint: item for item in self.entries}
        return tuple(by_name[name] for name in self.requested_endpoints if name in by_name)

    @property
    def retained_endpoints(self) -> tuple[str, ...]:
        excluded = {item.endpoint for item in self.entries}
        return tuple(name for name in self.requested_endpoints if name not in excluded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.info.id,
            "requested_endpoints": list(self.requested_endpoints),
            "entries": [item.to_dict() for item in self.entries],
        }

    def params(self) -> dict[str, Any]:
        return {
            "entries_reviewed": len(self.entries),
            "requested_endpoints": list(self.requested_endpoints),
        }


QueryStage: TypeAlias = StudyExclusion | EndpointExclusion | EndpointObservations
_STAGE_TYPES: dict[str, type[QueryStage]] = {
    StudyExclusion.info.id: StudyExclusion,
    EndpointExclusion.info.id: EndpointExclusion,
    EndpointObservations.info.id: EndpointObservations,
}
_STAGE_ORDER = {StudyExclusion: 0, EndpointExclusion: 1, EndpointObservations: 2}


@dataclass(frozen=True)
class CohortQuery:
    """The Grit42 subject query followed by VCGen query stages, in execution order."""

    contract: ClassVar[str] = COHORT_QUERY_CONTRACT

    subject_query: SubjectQuery
    stages: tuple[QueryStage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subject_query, SubjectQuery):
            raise QueryError("subject_query must be a SubjectQuery.")
        stages = tuple(self.stages)
        if any(not isinstance(stage, tuple(_STAGE_TYPES.values())) for stage in stages):
            raise QueryError("stages must contain registered query stage blocks.")
        order = [_STAGE_ORDER[type(stage)] for stage in stages]
        if order not in ([2], [0, 2], [1, 2], [0, 1, 2]):
            raise QueryError(
                "a cohort query is an optional study_exclusion, an optional endpoint_exclusion, "
                "then exactly one endpoint_observations stage."
            )
        final = stages[-1]
        for stage in stages:
            if not isinstance(stage, EndpointExclusion):
                continue
            excluded = {item.endpoint for item in stage.entries}
            selected = set(final.requested_endpoints)
            if not set(stage.retained_endpoints) <= selected or selected & excluded:
                raise QueryError(
                    "endpoint_observations must select every requested endpoint that "
                    "endpoint_exclusion keeps and none that it excludes."
                )
        object.__setattr__(self, "stages", stages)

    @classmethod
    def compose(
        cls,
        subject_query: SubjectQuery,
        requested_endpoints: Sequence[str],
        *,
        study_exclusions: Sequence[StudyExclusionEntry] | None = None,
        endpoint_exclusions: Sequence[EndpointExclusionEntry] | None = None,
    ) -> CohortQuery:
        """Build the standard plan: subject query, then endpoint observations.

        Passing exclusion entries, even an empty list, adds the matching
        exclusion stage so the review is recorded. Excluded endpoints leave the
        endpoint selection; ``submitted_endpoints`` keeps what was asked for.
        """
        stages: list[QueryStage] = []
        names = tuple(requested_endpoints)
        if study_exclusions is not None:
            stages.append(StudyExclusion(tuple(study_exclusions)))
        if endpoint_exclusions is not None:
            exclusion = EndpointExclusion(names, tuple(endpoint_exclusions))
            if not exclusion.retained_endpoints:
                raise QueryError(
                    "every requested endpoint is excluded: "
                    f"{[item.endpoint for item in exclusion.removed]}."
                )
            stages.append(exclusion)
            names = exclusion.retained_endpoints
        stages.append(EndpointObservations(names))
        return cls(subject_query, tuple(stages))

    @classmethod
    def from_mapping(cls, value: Any) -> CohortQuery:
        raw = mapping(value, "cohort_query")
        keys(raw, {"contract", "subject_query", "stages"}, "cohort_query")
        if raw.get("contract") != COHORT_QUERY_CONTRACT:
            raise QueryError(
                f"unsupported contract {raw.get('contract')!r}; expected {COHORT_QUERY_CONTRACT!r}."
            )
        items = raw.get("stages")
        if not seq(items):
            raise QueryError("cohort_query.stages must be an array.")
        stages = []
        for index, item in enumerate(items):
            stage = mapping(item, f"cohort_query.stages[{index}]")
            stage_type = _STAGE_TYPES.get(stage.get("stage"))
            if stage_type is None:
                raise QueryError(f"cohort_query.stages[{index}].stage is unsupported.")
            stages.append(stage_type.from_mapping(stage))
        return cls(SubjectQuery.from_mapping(raw.get("subject_query")), tuple(stages))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "subject_query": self.subject_query.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
        }

    @property
    def query_id(self) -> str:
        return self.subject_query.query_id

    @property
    def endpoint_stage(self) -> EndpointObservations:
        return self.stages[-1]

    @property
    def exclusion_stages(self) -> tuple[StudyExclusion | EndpointExclusion, ...]:
        return tuple(
            stage for stage in self.stages if isinstance(stage, (StudyExclusion, EndpointExclusion))
        )

    @property
    def submitted_endpoints(self) -> tuple[str, ...]:
        """The endpoint selection as submitted, before any exclusion."""
        for stage in self.stages:
            if isinstance(stage, EndpointExclusion):
                return stage.requested_endpoints
        return self.requested_endpoints

    @property
    def requested_endpoints(self) -> tuple[str, ...]:
        return self.endpoint_stage.requested_endpoints

    @property
    def resolved_endpoints(self) -> tuple[EndpointSpec, ...]:
        return self.endpoint_stage.resolved_endpoints

    def with_requested_endpoints(self, names: Sequence[str]) -> CohortQuery:
        """Replace the endpoint selection; membership stays unchanged."""
        return replace(self, stages=(*self.stages[:-1], EndpointObservations(tuple(names))))


def parse_subject_query(raw: Mapping[str, Any]) -> SubjectQuery:
    return SubjectQuery.from_mapping(raw)


def parse_cohort_query(raw: Mapping[str, Any]) -> CohortQuery:
    return CohortQuery.from_mapping(raw)


__all__ = [
    "SOURCE_STAGE",
    "SUBJECT_CONTEXT_STAGE",
    "CohortQuery",
    "EndpointExclusion",
    "EndpointExclusionEntry",
    "EndpointObservations",
    "QueryStage",
    "StageInfo",
    "StageKind",
    "StageChange",
    "StageDrop",
    "StageOrigin",
    "StageRecord",
    "StudyExclusion",
    "StudyExclusionEntry",
    "SubjectQuery",
    "TraceUnit",
    "parse_cohort_query",
    "parse_subject_query",
]
