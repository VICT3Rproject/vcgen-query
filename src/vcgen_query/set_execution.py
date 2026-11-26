"""Set-algebra execution for subject-predicate v1 nodes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import pandas as pd

from .cohort import (
    CohortSelection,
    _load_pre_study_endpoint_values,
    _load_study_contexts,
    _load_subject_contexts,
)
from .contract import (
    CanonicalOperator,
    DerivedField,
    Predicate,
    PredicateGroup,
    PredicateGroupOperator,
    PredicateRule,
    PreStudyEndpointField,
    SendColumnField,
    ValueType,
)
from .duckdb import q, quote_ident, register_facility_group_function, table_columns
from .contract_helpers import SourceDataError
from .grit42_catalogue import (
    AdvancedQueryDatetimeSource,
    grit42_column,
    grit42_domain_grain,
)
from .identities import GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT
from .send_columns import STUDY_ID, SUBJECT_ID
from .source_validation import validate_active_source

if TYPE_CHECKING:
    from .query import SubjectQuery

_DATE_DATETIME = r"\d{4}-\d{2}-\d{2}"
_TIME_DATETIME = r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
_NAIVE_DATETIME = rf"(?:{_DATE_DATETIME}|{_DATE_DATETIME}T{_TIME_DATETIME})"
_AWARE_DATETIME = rf"{_DATE_DATETIME}T{_TIME_DATETIME}(?:Z|[+-]\d{{2}}:\d{{2}})"
_DOSE_DURATION_EVIDENCE_STATUSES = (
    "declared_only",
    "corroborated",
    "shorter_than_planned",
    "ambiguous",
    "unresolved",
)
_EXPOSURE_INTERVAL_STATUSES = (
    "complete_contiguous",
    "gapped",
    "incomplete",
    "unavailable",
)
_DERIVED: dict[str, tuple[str, str, bool, bool]] = {
    "age_days": ("subject_demographics", "age_days", True, False),
    "baseline_body_weight_g": (
        "subject_baseline_body_weight",
        "baseline_body_weight_g",
        True,
        False,
    ),
    "planned_dose_duration_days": (
        "subject_exposure_summary",
        "planned_dose_duration_days",
        True,
        False,
    ),
    "supplier": ("study_design_summary", "supplier_values", False, True),
    "study_type": ("study_design_summary", "study_type_values", False, True),
    "test_facility": ("study_design_summary", "test_facility_values", False, True),
    "facility_group": ("study_design_summary", "test_facility_values", False, True),
    "glp_status": ("study_design_summary", "glp_status_values", False, True),
    "species": ("subject_demographics", "SPECIES", True, False),
    "sex": ("subject_demographics", "SEX", True, False),
    "strain": ("subject_demographics", "STRAIN", True, False),
    "route": ("subject_exposure_summary", "route_values", True, True),
    "dose_frequency": ("subject_exposure_summary", "dose_frequency_values", True, True),
    "treatment_vehicle": ("subject_exposure_summary", "treatment_values", True, True),
    "disposition_categories": (
        "subject_disposition_summary",
        "disposition_category_values",
        True,
        True,
    ),
    "terminal_sacrifice_offset_days": (
        "subject_disposition_summary",
        "terminal_sacrifice_offset_day_values",
        True,
        True,
    ),
}

NodeKind: TypeAlias = Literal["universe", "group", "rule"]
ExecutionScope: TypeAlias = Literal["source_row", "subject_set"]
IdentityScope: TypeAlias = Literal["study", "subject"]


@dataclass(frozen=True)
class RuleExecutionPlan:
    """One predicate rule with its resolved row and identity scopes."""

    node: PredicateRule
    path: str
    execution_scope: ExecutionScope
    identity_scope: IdentityScope
    source_domain: str | None = None


@dataclass(frozen=True)
class GroupExecutionPlan:
    """One unchanged predicate group with typed child execution scopes."""

    node: PredicateGroup
    path: str
    children: tuple[ResolvedExecutionNode, ...]
    execution_scope: ExecutionScope
    negation_scope: ExecutionScope | None


ResolvedExecutionNode: TypeAlias = RuleExecutionPlan | GroupExecutionPlan


@dataclass(frozen=True)
class PredicateExecutionPlan:
    """Resolved predicate execution without Boolean rewriting or flattening."""

    root: ResolvedExecutionNode | None


def resolve_execution_plan(query: SubjectQuery) -> PredicateExecutionPlan:
    """Resolve execution scope while preserving every predicate group boundary."""

    def resolve(node: Predicate, path: str) -> ResolvedExecutionNode:
        if isinstance(node, PredicateRule):
            if isinstance(node.field, SendColumnField):
                domain = node.field.domain
                return RuleExecutionPlan(
                    node=node,
                    path=path,
                    execution_scope="source_row",
                    identity_scope=grit42_domain_grain(domain),
                    source_domain=domain,
                )
            identity_scope: IdentityScope = (
                "study"
                if isinstance(node.field, DerivedField)
                and not _DERIVED.get(node.field.name, ("", "", True, False))[2]
                else "subject"
            )
            return RuleExecutionPlan(
                node=node,
                path=path,
                execution_scope="subject_set",
                identity_scope=identity_scope,
            )

        children = tuple(
            resolve(child, f"{path}.children[{index}]")
            for index, child in enumerate(node.children)
        )
        row_domains = {
            child.source_domain
            for child in children
            if isinstance(child, RuleExecutionPlan)
            and child.execution_scope == "source_row"
        }
        fully_row_scoped = len(row_domains) == 1 and all(
            isinstance(child, RuleExecutionPlan)
            and child.execution_scope == "source_row"
            for child in children
        )
        return GroupExecutionPlan(
            node=node,
            path=path,
            children=children,
            execution_scope="source_row" if fully_row_scoped else "subject_set",
            negation_scope=(
                "source_row" if fully_row_scoped else "subject_set"
            )
            if node.negated
            else None,
        )

    return PredicateExecutionPlan(
        root=None if query.predicate is None else resolve(query.predicate, "predicate")
    )


@dataclass(frozen=True)
class NodeExplanation:
    path: str
    kind: NodeKind
    count: int
    subject_keys: tuple[tuple[str, str], ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"universe", "group", "rule"}:
            raise ValueError(f"unsupported explanation node kind: {self.kind!r}")

    def to_dict(self, *, include_subject_keys: bool) -> dict[str, Any]:
        result = {
            "path": self.path,
            "kind": self.kind,
            "count": self.count,
            **self.details,
        }
        if include_subject_keys:
            result["subject_keys"] = [
                {"study_id": a, "subject_id": b} for a, b in self.subject_keys
            ]
        return result


@dataclass(frozen=True)
class PredicateEvaluation:
    subject_keys: tuple[tuple[str, str], ...]
    universe_keys: tuple[tuple[str, str], ...]
    nodes: tuple[NodeExplanation, ...]
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @property
    def membership_hash(self) -> str:
        return membership_hash(self.subject_keys)


def membership_hash(keys: Sequence[tuple[str, str]]) -> str:
    payload = json.dumps(
        sorted(set(keys)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def subject_universe(con: Any) -> tuple[tuple[str, str], ...]:
    """Return the DM subject universe in the same form as predicate evaluation."""
    columns = table_columns(con, "dm")
    _required(columns, STUDY_ID, "DM")
    _required(columns, SUBJECT_ID, "DM")
    return tuple(
        (str(study), str(subject))
        for study, subject in con.execute(
            f"SELECT DISTINCT cast({q(STUDY_ID)} AS VARCHAR), cast({q(SUBJECT_ID)} AS VARCHAR) "
            "FROM dm ORDER BY 1, 2"
        ).fetchall()
    )


def evaluate_predicate(
    con: Any, query: SubjectQuery, *, validate_source: bool = True
) -> PredicateEvaluation:
    """Evaluate the subject query alone. Its output is the Grit42 parity checkpoint."""
    if validate_source:
        validate_active_source(con, query)
    columns = table_columns(con, "dm")
    _required(columns, STUDY_ID, "DM")
    _required(columns, SUBJECT_ID, "DM")
    compiler = _Compiler(con, query)
    compiler.create_universe()
    plan = resolve_execution_plan(query)
    if plan.root is None:
        keys = compiler.rows(compiler.universe)
        return PredicateEvaluation(
            keys,
            keys,
            (
                NodeExplanation(
                    "predicate",
                    "universe",
                    len(keys),
                    keys,
                    {"reason": "missing_predicate"},
                ),
            ),
            assumptions=("missing predicate selects the complete DM subject universe",),
        )
    membership = compiler.compile(plan.root)
    return PredicateEvaluation(
        compiler.rows(membership),
        compiler.rows(compiler.universe),
        tuple(compiler.nodes),
        tuple(compiler.warnings),
        tuple(compiler.assumptions),
    )


def selection_for_keys(
    con: Any,
    keys: Sequence[tuple[str, str]],
    *,
    universe_keys: Sequence[tuple[str, str]],
) -> CohortSelection:
    requested = tuple(keys)
    if len(requested) != len(set(requested)):
        raise SourceDataError("predicate evaluation produced duplicate subject keys")
    observed_keys: list[tuple[str, str]] = []
    contexts = _load_subject_contexts(
        con, _load_study_contexts(con), selected_keys=set(requested), observed_keys=observed_keys,
    )
    actual_universe = set(observed_keys)
    if len(observed_keys) != len(actual_universe):
        raise SourceDataError("subject context assembly produced duplicate subject keys")
    contexts_by_key = {}
    for context in contexts:
        key = (context.study_id, context.subject_id)
        if key in contexts_by_key:
            raise SourceDataError("subject context assembly produced duplicate subject keys")
        contexts_by_key[key] = context

    expected_universe = set(universe_keys)
    if actual_universe != expected_universe:
        raise SourceDataError(
            "subject context keys do not match the DM universe: "
            f"missing={len(expected_universe - actual_universe)}, "
            f"extra={len(actual_universe - expected_universe)}"
        )

    missing = set(requested) - actual_universe
    if missing:
        raise SourceDataError(
            f"predicate membership contains {len(missing)} subject key(s) outside the DM universe"
        )
    subjects = tuple(contexts_by_key[key] for key in requested)
    evidence_statuses = Counter(subject.dose_duration_evidence_status for subject in subjects)
    exposure_statuses = Counter(subject.exposure_interval_status for subject in subjects)
    incomplete_declarations = sum(
        subject.dose_duration_declaration_incomplete for subject in subjects
    )
    warnings: list[str] = []
    if incomplete_declarations:
        warnings.append(
            f"{incomplete_declarations} matched subject(s) have non-empty DOSDUR values "
            "that could not be normalized; planned duration resolution used only "
            "normalized declarations"
        )
    for status in ("ambiguous", "unresolved"):
        count = evidence_statuses.get(status, 0)
        if count:
            warnings.append(
                f"{count} matched subject(s) have {status} planned dose-duration evidence"
            )
    for status in ("gapped", "incomplete"):
        count = exposure_statuses.get(status, 0)
        if count:
            warnings.append(
                f"{count} matched subject(s) have {status} EX interval evidence; "
                "observed exposure span was not resolved"
            )
    return CohortSelection(
        subjects=subjects,
        warnings=tuple(warnings),
        counts={
            "candidate_subject_count": len(contexts_by_key),
            "matched_subject_count": len(subjects),
            "matched_study_count": len({x.study_id for x in subjects}),
        },
        dose_duration_evidence_status_counts={
            status: evidence_statuses.get(status, 0)
            for status in _DOSE_DURATION_EVIDENCE_STATUSES
        },
        exposure_interval_status_counts={
            status: exposure_statuses.get(status, 0)
            for status in _EXPOSURE_INTERVAL_STATUSES
        },
        dose_duration_declaration_incomplete_subject_count=incomplete_declarations,
    )


class _Compiler:
    def __init__(self, con: Any, query: SubjectQuery) -> None:
        self.con, self.query, self.index, self.universe = (
            con,
            query,
            0,
            "_vcgen_predicate_v1_universe",
        )
        self.nodes: list[NodeExplanation] = []
        self.warnings: list[str] = []
        self.pre_study_sexes: dict[tuple[str, str], str] | None = None
        self.pre_study_values: dict[str, dict[tuple[str, str], dict[str, float]]] = {}
        self.grit42_advanced_import = (
            query.import_provenance is not None
            and query.import_provenance.source_contract
            == GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT
        )
        self.assumptions = [
            "direct SEND rules from the same domain and group are evaluated against one source row",
            "nested groups, derived rules, pre-study endpoint rules, and different SEND domains lift independently to subject-key sets",
            "catalogue-declared study-grain domains propagate matching STUDYID rows to every DM subject",
            "catalogue-declared subject-grain domains require valid STUDYID and USUBJID values",
            "missing, malformed, or duplicate DM subject keys reject execution",
            "NOT on a fully row-scoped group is evaluated before subject lifting; other NOT groups use the complete DM-universe complement",
        ]

    def create_universe(self) -> None:
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE {quote_ident(self.universe)} AS "
            f"SELECT cast({q(STUDY_ID)} AS VARCHAR) study_id, "
            f"cast({q(SUBJECT_ID)} AS VARCHAR) subject_id FROM dm"
        )

    def compile(self, plan: ResolvedExecutionNode) -> str:
        return self.group(plan) if isinstance(plan, GroupExecutionPlan) else self.rule(plan)

    def group(self, plan: GroupExecutionPlan) -> str:
        node = plan.node
        path = plan.path
        group = node.group
        source_rules: dict[str, list[RuleExecutionPlan]] = {}
        for child in plan.children:
            if isinstance(child, RuleExecutionPlan) and child.source_domain is not None:
                source_rules.setdefault(child.source_domain, []).append(child)

        negated = node.negated
        fully_row_scoped = plan.execution_scope == "source_row"

        relations = []
        compiled_domains: set[str] = set()
        for child in plan.children:
            domain = child.source_domain if isinstance(child, RuleExecutionPlan) else None
            if domain is None:
                relations.append(self.compile(child))
                continue
            if domain in compiled_domains:
                continue
            compiled_domains.add(domain)
            domain_rules = source_rules[domain]
            if fully_row_scoped:
                relations.append(
                    self.source_group(
                        domain_rules,
                        group,
                        path,
                        negated=negated,
                    )
                )
            elif len(domain_rules) == 1:
                relations.append(self.rule(child))
            else:
                relations.append(self.source_group(domain_rules, group, path))

        statement = f" {'INTERSECT' if group == 'and' else 'UNION'} ".join(
            f"SELECT study_id, subject_id FROM {quote_ident(relation)}" for relation in relations
        )
        if negated and not fully_row_scoped:
            statement = f"SELECT study_id, subject_id FROM {quote_ident(self.universe)} EXCEPT ({statement})"
        relation = self.new_relation()
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE {quote_ident(relation)} AS {statement}")
        keys = self.rows(relation)
        self.nodes.append(
            NodeExplanation(
                path,
                "group",
                len(keys),
                keys,
                {
                    "group": group,
                    "negated": negated,
                    "negation_scope": plan.negation_scope,
                    "scope": "subject_set",
                    "row_scoped_domains": [
                        {"domain": domain, "rule_count": len(rules)}
                        for domain, rules in source_rules.items()
                    ],
                },
            )
        )
        return relation

    def source_group(
        self,
        rules: Sequence[RuleExecutionPlan],
        group: PredicateGroupOperator,
        path: str,
        *,
        negated: bool = False,
    ) -> str:
        domain = rules[0].source_domain
        if domain is None:
            raise ValueError("source group requires direct SEND rules")
        columns = table_columns(self.con, domain.lower())
        study = _required(columns, STUDY_ID, domain)
        subject = (
            _required(columns, SUBJECT_ID, domain)
            if grit42_domain_grain(domain) == "subject"
            else None
        )
        predicates: list[str] = []
        params: list[Any] = []

        for rule_plan in rules:
            rule = rule_plan.node
            field = rule.field
            if not isinstance(field, SendColumnField) or field.domain != domain:
                raise ValueError("source group rules must use one SEND domain")
            predicate, rule_params = _predicate(
                f"rows.{q(_required(columns, field.column, domain))}",
                rule.operator,
                rule.value,
                field.value_type,
                datetime_source=self.advanced_query_datetime_source(field),
            )
            predicates.append(predicate)
            params.extend(rule_params)

            # Keep the independent rule match as diagnostic evidence. The
            # domain-group node below is the relation used for membership.
            self.rule(rule_plan)

        join = f"cast(rows.{q(study)} AS VARCHAR) = universe.study_id"
        if subject is not None:
            join += f" AND cast(rows.{q(subject)} AS VARCHAR) = universe.subject_id"
        row_predicate = f" {group.upper()} ".join(f"({item})" for item in predicates)
        if negated:
            row_predicate = f"NOT ({row_predicate})"
        relation = self.new_relation()
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE {quote_ident(relation)} AS "
            "SELECT DISTINCT universe.study_id, universe.subject_id "
            f"FROM {quote_ident(domain.lower())} rows "
            f"JOIN {quote_ident(self.universe)} universe ON {join} "
            f"WHERE {row_predicate}",
            params,
        )
        keys = self.rows(relation)
        self.nodes.append(
            NodeExplanation(
                f"{path}.domain[{domain}]",
                "group",
                len(keys),
                keys,
                {
                    "domain": domain,
                    "group": group,
                    "negated": negated,
                    "scope": "source_row",
                    "rule_paths": [rule.path for rule in rules],
                },
            )
        )
        return relation

    def rule(self, plan: RuleExecutionPlan) -> str:
        node = plan.node
        path = plan.path
        field, operator, value = node.field, node.operator, node.value
        if isinstance(field, SendColumnField):
            sql, params = self.source(field, operator, value)
        elif isinstance(field, DerivedField):
            sql, params = self.context(field.name, operator, value, field.value_type)
        else:
            relation = self.pre_study(field, operator, value)
            keys = self.rows(relation)
            self.nodes.append(
                NodeExplanation(
                    path,
                    "rule",
                    len(keys),
                    keys,
                    {
                        "field_kind": field.kind,
                        "operator": operator,
                        "endpoint": field.endpoint,
                    },
                )
            )
            return relation
        relation = self.new_relation()
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE {quote_ident(relation)} AS {sql}", params)
        keys = self.rows(relation)
        self.nodes.append(
            NodeExplanation(
                path,
                "rule",
                len(keys),
                keys,
                {"field_kind": field.kind, "operator": operator},
            )
        )
        return relation

    def source(
        self, field: SendColumnField, operator: CanonicalOperator, value: Any
    ) -> tuple[str, list[Any]]:
        domain, column = field.domain, field.column
        columns = table_columns(self.con, domain.lower())
        source, study, subject = (
            _required(columns, column, domain),
            _required(columns, STUDY_ID, domain),
            _required(columns, SUBJECT_ID, domain)
            if grit42_domain_grain(domain) == "subject"
            else None,
        )
        predicate, params = _predicate(
            f"rows.{q(source)}",
            operator,
            value,
            field.value_type,
            datetime_source=self.advanced_query_datetime_source(field),
        )
        join = f"cast(rows.{q(study)} AS VARCHAR) = universe.study_id"
        if subject is not None:
            join += f" AND cast(rows.{q(subject)} AS VARCHAR) = universe.subject_id"
        return (
            f"SELECT DISTINCT universe.study_id, universe.subject_id FROM {quote_ident(domain.lower())} rows JOIN {quote_ident(self.universe)} universe ON {join} WHERE {predicate}",
            params,
        )

    def advanced_query_datetime_source(
        self, field: SendColumnField
    ) -> AdvancedQueryDatetimeSource | None:
        if not self.grit42_advanced_import or field.value_type != "datetime":
            return None
        definition = grit42_column(field.domain, field.column)
        if definition is None:
            raise ValueError(f"grit42 catalogue is missing {field.domain}.{field.column}")
        source = definition.advanced_query_datetime_source
        if source is not None:
            assumption = (
                f"imported grit42 {field.domain}.{field.column} comparisons use "
                f"catalogue source semantics {source}"
            )
            if assumption not in self.assumptions:
                self.assumptions.append(assumption)
        return source

    def context(
        self, name: str, operator: CanonicalOperator, value: Any, kind: ValueType
    ) -> tuple[str, list[Any]]:
        if name == "study_start_year":
            return self.study_start_year(operator, value, kind)
        specification = _DERIVED.get(name)
        if specification is None:
            raise ValueError(f"unsupported derived field: {name!r}")
        view, source_name, subject_grain, list_value = specification
        columns = table_columns(self.con, view)
        if not columns:
            self.warnings.append(f"derived view {view!r} unavailable; {name} matches no subjects")
            return (
                f"SELECT study_id, subject_id FROM {quote_ident(self.universe)} WHERE FALSE",
                [],
            )
        source = _required(columns, source_name, view)
        study = _required(columns, STUDY_ID, view)
        subject = _find(columns, SUBJECT_ID)
        if subject_grain and subject is None:
            raise ValueError(f"derived field {name!r} lacks subject identity")

        join = f"cast(rows.{q(study)} AS VARCHAR) = universe.study_id"
        if subject_grain:
            join += f" AND cast(rows.{q(subject)} AS VARCHAR) = universe.subject_id"
        if list_value:
            if name == "facility_group":
                register_facility_group_function(self.con)
            actual = (
                "candidate.value" if kind == "number" else "upper(cast(candidate.value AS VARCHAR))"
            )
            if name == "facility_group":
                actual = "vcgen_facility_group(cast(candidate.value AS VARCHAR))"
            predicate, params = _predicate(actual, operator, value, kind)
            return (
                "SELECT DISTINCT universe.study_id, universe.subject_id "
                f"FROM {quote_ident(view)} rows "
                f"CROSS JOIN UNNEST(rows.{q(source)}) AS candidate(value) "
                f"JOIN {quote_ident(self.universe)} universe ON {join} "
                "WHERE candidate.value IS NOT NULL "
                "AND trim(cast(candidate.value AS VARCHAR)) <> '' "
                f"AND ({predicate})",
                params,
            )

        predicate, params = _predicate(f"rows.{q(source)}", operator, value, kind)
        return (
            "SELECT DISTINCT universe.study_id, universe.subject_id "
            f"FROM {quote_ident(view)} rows "
            f"JOIN {quote_ident(self.universe)} universe ON {join} "
            f"WHERE {predicate}",
            params,
        )

    def study_start_year(
        self, operator: CanonicalOperator, value: Any, kind: ValueType
    ) -> tuple[str, list[Any]]:
        columns = table_columns(self.con, "dm")
        reference_date = _find(columns, "RFSTDTC")
        if reference_date is None:
            self.warnings.append(
                "DM does not provide RFSTDTC; study_start_year matches no subjects"
            )
            return (
                f"SELECT study_id, subject_id FROM {quote_ident(self.universe)} WHERE FALSE",
                [],
            )
        study = _required(columns, STUDY_ID, "DM")
        year = (
            f"try_cast(regexp_extract(cast({q(reference_date)} AS VARCHAR), "
            "'^[0-9]{4}', 0) AS INTEGER)"
        )
        predicate, params = _predicate("rows.actual", operator, value, kind)
        return (
            "WITH rows AS ("
            f"SELECT cast({q(study)} AS VARCHAR) AS study_id, "
            "CASE WHEN count(*) > 0 "
            f"AND count({year}) = count(*) AND min({year}) = max({year}) "
            f"THEN min({year}) END AS actual "
            f"FROM dm GROUP BY {q(study)}"
            ") "
            "SELECT DISTINCT universe.study_id, universe.subject_id "
            f"FROM rows JOIN {quote_ident(self.universe)} universe "
            "ON rows.study_id = universe.study_id "
            f"WHERE {predicate}",
            params,
        )

    def pre_study(
        self, field: PreStudyEndpointField, operator: CanonicalOperator, value: Any
    ) -> str:
        name = field.endpoint
        from .endpoint_catalogue import canonical_endpoint

        if name not in self.pre_study_values:
            if self.pre_study_sexes is None:
                self.pre_study_sexes = {
                    (str(study or ""), str(subject or "")): str(sex or "")
                    for study, subject, sex in self.con.execute(
                        f"SELECT {q(STUDY_ID)}, {q(SUBJECT_ID)}, {q('SEX')} FROM subject_demographics"
                    ).fetchall()
                }
            self.pre_study_values[name] = _load_pre_study_endpoint_values(
                self.con, self.pre_study_sexes, ((canonical_endpoint(name).spec, None),),
            )
        values = self.pre_study_values[name]
        relation = self.new_relation()
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE {quote_ident(relation)} (study_id VARCHAR, subject_id VARCHAR)"
        )
        selected = [key for key, resolved in values.items()
                    if _python_match(resolved.get(name), operator, value, "number")]
        if selected:
            registered = relation + "_keys"
            self.con.register(registered, pd.DataFrame.from_records(
                selected, columns=("study_id", "subject_id"),
            ))
            try:
                self.con.execute(
                    f"INSERT INTO {quote_ident(relation)} SELECT study_id, subject_id FROM {quote_ident(registered)}"
                )
            finally:
                self.con.unregister(registered)
        return relation

    def new_relation(self) -> str:
        self.index += 1
        return f"_vcgen_predicate_v1_node_{self.index}"

    def rows(self, relation: str) -> tuple[tuple[str, str], ...]:
        fetched = self.con.execute(
            f"SELECT DISTINCT study_id, subject_id FROM {quote_ident(relation)} ORDER BY 1, 2"
        ).fetchall()
        if not fetched:
            return ()
        studies, subjects = zip(*fetched)
        return tuple(zip(map(str, studies), map(str, subjects)))

def _predicate(
    expr: str,
    op: CanonicalOperator,
    value: Any,
    value_type: ValueType,
    *,
    datetime_source: AdvancedQueryDatetimeSource | None = None,
) -> tuple[str, list[Any]]:
    kind = value_type
    if op in {"is_empty", "is_not_empty"}:
        test = f"({expr} IS NULL OR cast({expr} AS VARCHAR) = '')"
        return (test if op == "is_empty" else f"NOT {test}"), []
    if kind == "number":
        cast = f"try_cast(nullif(trim(cast({expr} AS VARCHAR)), '') AS DOUBLE)"
        return _compare(cast, f"isfinite({cast})", op, value, True)
    if kind == "datetime":
        return _datetime_predicate(expr, op, value, source=datetime_source)
    return _compare(f"cast({expr} AS VARCHAR)", f"{expr} IS NOT NULL", op, value, False)

def _datetime_predicate(
    expr: str,
    op: CanonicalOperator,
    value: Any,
    *,
    source: AdvancedQueryDatetimeSource | None,
) -> tuple[str, list[Any]]:
    mode, value = _datetime_values(value, op)
    text = f"cast({expr} AS VARCHAR)"
    if source == "calendar_date_midnight" and mode == "naive":
        date_text = f"substr({text}, 1, 10)"
        cast = (
            f"CASE WHEN regexp_full_match({date_text}, {_pattern_sql(_DATE_DATETIME)}) "
            f"THEN try_cast({date_text} AS TIMESTAMP) END"
        )
        return _compare(cast, f"({cast}) IS NOT NULL", op, value, False)
    pattern = _AWARE_DATETIME if mode == "aware" else _NAIVE_DATETIME
    cast_type = "TIMESTAMPTZ" if mode == "aware" else "TIMESTAMP"
    cast = (
        f"CASE WHEN regexp_full_match({text}, {_pattern_sql(pattern)}) "
        f"THEN try_cast({text} AS {cast_type}) END"
    )
    comparable = f"epoch_us({cast})" if mode == "aware" else cast
    return _compare(comparable, f"({cast}) IS NOT NULL", op, value, mode == "aware")

def _datetime_values(value: Any, op: CanonicalOperator) -> tuple[str, Any]:
    if op in {"in", "not_in", "between", "not_between"}:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"{op} datetime value must be an array")
        parsed = [_datetime_scalar(item) for item in value]
    else:
        parsed = [_datetime_scalar(value)]
    modes = {mode for mode, _ in parsed}
    if len(modes) != 1:
        raise ValueError("datetime predicate values must not mix aware and naive timestamps")
    mode = modes.pop()
    values = [item for _, item in parsed]
    return (mode, values if op in {"in", "not_in", "between", "not_between"} else values[0])


def _datetime_scalar(value: Any) -> tuple[str, datetime | int]:
    if isinstance(value, datetime):
        parsed = value
        aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
    else:
        text = str(value)
        aware = re.fullmatch(_AWARE_DATETIME, text) is not None
        if not aware and re.fullmatch(_NAIVE_DATETIME, text) is None:
            raise ValueError("datetime predicate value is not a supported strict datetime")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if not aware:
        return "naive", parsed.replace(tzinfo=None)
    normalized = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return "aware", microseconds


def _compare(expr: str, valid: str, op: str, value: Any, numeric: bool) -> tuple[str, list[Any]]:
    if op in {"eq", "ne", "gt", "gte", "lt", "lte"}:
        signs = {
            "eq": "=",
            "ne": "!=",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
        }
        return f"({valid}) AND ({expr} {signs[op]} ?)", [_scalar(value, numeric)]
    if op in {"between", "not_between"}:
        low, high = _bounds(value, numeric)
        inside = f"({expr} >= ? AND {expr} <= ?)"
        negation = "NOT " if op == "not_between" else ""
        return f"({valid}) AND ({negation}{inside})", [low, high]
    if op in {"in", "not_in"}:
        values = _items(value, numeric)
        inside = f"{expr} IN ({', '.join('?' for _ in values)})"
        negation = "NOT " if op == "not_in" else ""
        return f"({valid}) AND ({negation}({inside}))", values
    if op in {
        "contains",
        "not_contains",
        "starts_with",
        "not_starts_with",
        "ends_with",
        "not_ends_with",
    }:
        raw = str(_scalar(value, False))
        escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base = op.removeprefix("not_")
        patterns = {
            "contains": f"%{escaped}%",
            "starts_with": f"{escaped}%",
            "ends_with": f"%{escaped}",
        }
        negation = "NOT " if op.startswith("not_") else ""
        return (
            f"({valid}) AND ({negation}({expr} LIKE ? ESCAPE '\\'))",
            [patterns[base]],
        )
    raise ValueError(f"unsupported canonical operator: {op!r}")


def _pattern_sql(pattern: str) -> str:
    return "'" + pattern.replace("'", "''") + "'"


def _python_match(candidate: Any, op: str, value: Any, kind: str) -> bool:
    if op == "is_empty":
        return candidate is None or candidate == ""
    if op == "is_not_empty":
        return candidate is not None and candidate != ""
    if candidate is None:
        return False
    numeric = kind == "number"
    if numeric and (
        isinstance(candidate, bool)
        or not isinstance(candidate, (int, float))
        or not math.isfinite(candidate)
    ):
        return False
    if op == "eq":
        return candidate == value
    if op == "ne":
        return candidate != value
    if op == "gt":
        return candidate > value
    if op == "gte":
        return candidate >= value
    if op == "lt":
        return candidate < value
    if op == "lte":
        return candidate <= value
    if op in {"between", "not_between"}:
        low, high = _bounds(value, numeric)
        result = low <= candidate <= high
        return not result if op == "not_between" else result
    if op == "in":
        return candidate in _items(value, numeric)
    if op == "not_in":
        return candidate not in _items(value, numeric)
    if op in {"contains", "starts_with", "ends_with"}:
        return {
            "contains": str(value) in str(candidate),
            "starts_with": str(candidate).startswith(str(value)),
            "ends_with": str(candidate).endswith(str(value)),
        }[op]
    if op in {"not_contains", "not_starts_with", "not_ends_with"}:
        return not _python_match(candidate, op.removeprefix("not_"), value, kind)
    raise ValueError(f"unsupported canonical operator: {op!r}")



def _scalar(value: Any, numeric: bool) -> Any:
    if numeric:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("numeric predicate values must be finite")
        return value if isinstance(value, int) else float(value)
    return value if isinstance(value, datetime) else str(value)


def _bounds(value: Any, numeric: bool) -> tuple[Any, Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError("between value must have two bounds")
    low, high = _scalar(value[0], numeric), _scalar(value[1], numeric)
    if low > high:
        raise ValueError("between lower bound exceeds upper bound")
    return low, high


def _items(value: Any, numeric: bool) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("set value must be non-empty")
    return [_scalar(item, numeric) for item in value]




def _find(columns: Sequence[str], wanted: str) -> str | None:
    return next((value for value in columns if value.casefold() == wanted.casefold()), None)


def _required(columns: Sequence[str], wanted: str, relation: str) -> str:
    found = _find(columns, wanted)
    if found is None:
        raise SourceDataError(f"{relation} does not provide required column {wanted!r}")
    return found


__all__ = [
    "NodeExplanation",
    "PredicateEvaluation",
    "evaluate_predicate",
    "membership_hash",
    "selection_for_keys",
    "subject_universe",
]
