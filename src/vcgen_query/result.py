"""Public cohort-query execution and explain facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, TypedDict
from zipfile import is_zipfile

import pandas as pd

from .contract import (
    Query,
    QueryError,
    ident,
    machine_ident,
    normalized_texts,
    rules,
)
from .fingerprint import fingerprint_observation_contract, fingerprint_predicate
from .duckdb import archive_sha256, open_archive, open_directory
from .cohort import SubjectContext
from .identities import COHORT_QUERY_CONTRACT, SUBJECT_PREDICATE_CONTRACT
from .observation import OBSERVATION_COLUMNS, extract_observations
from .send_semantics import normalize_human_category
from .set_execution import PredicateEvaluation, evaluate_predicate, selection_for_keys
from .unit_catalogue import _CompiledUnitRule, catalogue_identity, observation_unit_semantics


class EndpointMetadata(TypedDict, total=False):
    endpoint_name: str
    domain: str
    sex_applicability: list[str]
    canonical_unit: str
    conversion_rules: list[dict[str, Any]]
    unit_catalogue: dict[str, str]


class QueryMetadata(TypedDict):
    contract: Literal["vcgen.cohort_query.v4"]
    predicate_contract: Literal["vcgen.subject_predicate.v1"]
    query_id: str
    predicate_fingerprint: str
    observation_fingerprint: str
    import_provenance: dict[str, str] | None
    source_identity: str | None
    membership_hash: str
    canonical_query: dict[str, Any]
    subject_count: int
    unit_catalogue: dict[str, str]
    unit_semantics: dict[str, Any]


class QueryReceipt(TypedDict):
    query_id: str
    contract: Literal["vcgen.cohort_query.v4"]
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


_SUBJECT_COLUMNS = tuple(f.name for f in fields(SubjectContext))


@dataclass(frozen=True)
class EndpointFacts:
    endpoint_name: str
    domain: str
    sex_applicability: tuple[str, ...] = ()
    target_unit: str | None = None
    compiled_unit_rules: tuple[_CompiledUnitRule, ...] = ()

    def __post_init__(self) -> None:
        endpoint_name = machine_ident(self.endpoint_name, "endpoint_facts.endpoint_name")
        if endpoint_name in {"STUDY_ID", "SUBJECT_ID", "STUDY_DAY"}:
            raise QueryError("endpoint_facts.endpoint_name cannot use a daily key column.")
        if self.target_unit is not None and not isinstance(self.target_unit, str):
            raise QueryError("endpoint_facts.target_unit must be a string when supplied.")
        if any(not isinstance(rule, _CompiledUnitRule) for rule in self.compiled_unit_rules):
            raise QueryError("endpoint_facts.compiled_unit_rules contains an invalid rule.")
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
        return metadata


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


def execute_query(
    source: Any,
    query: Query,
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
        domains=_query_domains(query),
    )
    source_identity, source_warning = _source_identity(source, query, source_data_identity)
    try:
        evaluation = evaluate_predicate(con, query)
        selection = selection_for_keys(con, evaluation.subject_keys)
        observations = extract_observations(con, selection, query.observation_contract)
    finally:
        if opened and close_connection:
            con.close()
    warnings = (
        tuple(query.warnings)
        + evaluation.warnings
        + observations.warnings
        + ((source_warning,) if source_warning else ())
    )
    receipt = _receipt(query, evaluation, source_identity, warnings, observations.to_receipt())
    subjects = pd.DataFrame.from_records(
        (item.to_dict() for item in selection.subjects),
        columns=_SUBJECT_COLUMNS,
    )
    observation_rows = (item.to_dict() for item in observations.records)
    observation_frame = pd.DataFrame.from_records(
        observation_rows,
        columns=OBSERVATION_COLUMNS,
    )
    return QueryResult(
        subjects=subjects,
        observations=observation_frame,
        metadata={
            "contract": COHORT_QUERY_CONTRACT,
            "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
            "query_id": query.query_id,
            "predicate_fingerprint": fingerprint_predicate(query.predicate),
            "observation_fingerprint": fingerprint_observation_contract(query),
            "import_provenance": _import_provenance(query),
            "source_identity": source_identity,
            "membership_hash": evaluation.membership_hash,
            "canonical_query": query.to_canonical_dict(),
            "subject_count": len(evaluation.subject_keys),
            "unit_catalogue": catalogue_identity(),
            "unit_semantics": observation_unit_semantics(
                query.observation_contract.endpoints
            ),
        },
        receipt=receipt,
        endpoint_names=observations.endpoint_names,
        endpoint_facts=tuple(
            EndpointFacts(
                item.name,
                item.domain,
                item.sexes,
                item.target_unit,
                item.compiled_unit_rules,
            )
            for item in query.observation_contract.endpoints
        ),
        warnings=warnings,
    )


def explain_query(
    source: Any,
    query: Query,
    *,
    extract_dir: str | Path | None = None,
    database: str | Path | None = None,
    close_connection: bool = True,
    include_subject_keys: bool = False,
    source_data_identity: str | None = None,
) -> dict[str, Any]:
    """Return the exact predicate evaluation trace used by execution."""
    con, opened = _open_source(
        source,
        extract_dir=extract_dir,
        database=database,
        domains=_query_domains(query),
    )
    source_identity, source_warning = _source_identity(source, query, source_data_identity)
    try:
        evaluation = evaluate_predicate(con, query)
    finally:
        if opened and close_connection:
            con.close()
    warnings = (
        tuple(query.warnings) + evaluation.warnings + ((source_warning,) if source_warning else ())
    )
    return {
        "contract": COHORT_QUERY_CONTRACT,
        "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
        "query_id": query.query_id,
        "predicate_fingerprint": fingerprint_predicate(query.predicate),
        "observation_fingerprint": fingerprint_observation_contract(query),
        "import_provenance": _import_provenance(query),
        "source_identity": source_identity,
        "unit_catalogue": catalogue_identity(),
        "unit_semantics": observation_unit_semantics(query.observation_contract.endpoints),
        "assumptions": list(query.assumptions) + list(evaluation.assumptions),
        "warnings": list(warnings),
        "membership_hash": evaluation.membership_hash,
        "subject_count": len(evaluation.subject_keys),
        "matched_subject_count": len(evaluation.subject_keys),
        "candidate_subject_count": len(evaluation.universe_keys),
        "nodes": [
            node.to_dict(include_subject_keys=include_subject_keys) for node in evaluation.nodes
        ],
    }


def _receipt(
    query: Query,
    evaluation: PredicateEvaluation,
    source_identity: str | None,
    warnings: tuple[str, ...],
    observation: Mapping[str, Any],
) -> QueryReceipt:
    return {
        "query_id": query.query_id,
        "contract": COHORT_QUERY_CONTRACT,
        "predicate_contract": SUBJECT_PREDICATE_CONTRACT,
        "predicate_fingerprint": fingerprint_predicate(query.predicate),
        "observation_fingerprint": fingerprint_observation_contract(query),
        "import_provenance": _import_provenance(query),
        "source_identity": source_identity,
        "assumptions": list(query.assumptions) + list(evaluation.assumptions),
        "warnings": list(warnings),
        "membership_hash": evaluation.membership_hash,
        "subject_count": len(evaluation.subject_keys),
        "study_count": len({study for study, _ in evaluation.subject_keys}),
        "observations": dict(observation),
        "unit_catalogue": catalogue_identity(),
        "unit_semantics": observation_unit_semantics(query.observation_contract.endpoints),
    }


def _import_provenance(query: Query) -> dict[str, str] | None:
    return None if query.import_provenance is None else query.import_provenance.to_canonical_dict()


def _source_identity(
    source: Any, query: Query, override: str | None
) -> tuple[str | None, str | None]:
    if override is not None:
        if not str(override).strip():
            raise ValueError("source_data_identity must be non-empty when supplied")
        return str(override), None
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file() and is_zipfile(path):
            return f"archive-sha256:{archive_sha256(path)}", None
    if query.source_identity:
        return query.source_identity, None
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
            return open_directory(path, database=database), True
        if is_zipfile(path):
            return open_archive(
                path,
                Path(extract_dir) if extract_dir else path.parent / ".vcgen_query_cache",
                domains=domains,
                database=database,
            ), True
        if path.exists():
            raise ValueError(f"source {path} is neither a directory nor a zip archive")
        raise FileNotFoundError(f"source not found: {path}")
    raise TypeError("source must be an open DuckDB connection, CSV directory, or SEND zip archive")


def _query_domains(query: Query) -> set[str]:
    """Return only raw domains needed by the query and observation contract."""
    domains = {endpoint.domain for endpoint in query.observation_contract.endpoints}
    for rule in rules(query.predicate):
        domain = getattr(rule.field, "domain", None)
        if domain is not None:
            domains.add(str(domain).upper())
        if getattr(rule.field, "name", None) == "baseline_body_weight_g":
            domains.add("BW")
    return domains


def _is_connection(value: Any) -> bool:
    return hasattr(value, "execute") and callable(value.execute)


__all__ = [
    "EndpointMetadata",
    "QueryMetadata",
    "QueryReceipt",
    "EndpointFacts",
    "QueryResult",
    "execute_query",
    "explain_query",
]
