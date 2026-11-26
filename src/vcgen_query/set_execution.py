"""Set-algebra execution for subject-predicate v1 nodes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timezone
import math
import re
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import numpy as np
import pandas as pd

from .fingerprint import membership_hash
from .cohort import (
    CohortSelection,
    SubjectContext,
    _load_pre_study_endpoint_values,
    _load_subject_contexts,
    load_stored_subject_contexts,
    subject_sexes,
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
from .duckdb import (
    PATHOLOGY_DAY_TOLERANCE_DAYS,
    PRE_STUDY_ENDPOINT_TABLE,
    SUBJECT_CONTEXT_TABLE,
    python_derived_tables_current,
    q,
    quote_ident,
    register_facility_group_function,
    registered_frame,
    table_columns,
)
from .contract_helpers import SourceDataError
from .grit42_catalogue import (
    AdvancedQueryDatetimeSource,
    grit42_column,
    grit42_domain_grain,
)
from .query import StageWarning
from .identities import GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT
from .frequency_aliases import load_frequency_aliases
from .route_aliases import load_route_aliases
from .send_columns import STUDY_ID, SUBJECT_ID
from .source_validation import validate_active_source

if TYPE_CHECKING:
    from .query import SubjectQuery

_DATE_DATETIME = r"\d{4}-\d{2}-\d{2}"
_TIME_DATETIME = r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
_NAIVE_DATETIME = rf"(?:{_DATE_DATETIME}|{_DATE_DATETIME}T{_TIME_DATETIME})"
_AWARE_DATETIME = rf"{_DATE_DATETIME}T{_TIME_DATETIME}(?:Z|[+-]\d{{2}}:\d{{2}})"
DOSE_DURATION_EVIDENCE_STATUSES = (
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
    # Each EX (route, method) pair resolves through the reviewed route aliases.
    "route": ("subject_exposure_summary", "route_method_values", True, True),
    # Each exposure frequency spelling resolves through the reviewed frequency aliases.
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
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"universe", "group", "rule"}:
            raise ValueError(f"unsupported explanation node kind: {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "count": self.count, **self.details}


@dataclass(frozen=True)
class PredicateEvaluation:
    subject_keys: tuple[tuple[str, str], ...]
    universe_keys: tuple[tuple[str, str], ...]
    nodes: tuple[NodeExplanation, ...]
    warnings: tuple[StageWarning, ...] = ()
    assumptions: tuple[str, ...] = ()

    @cached_property
    def membership_hash(self) -> str:
        return membership_hash(self.subject_keys)


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


def evaluate_predicate(con: Any, query: SubjectQuery) -> PredicateEvaluation:
    """Evaluate the subject query alone. Its output is the Grit42 parity checkpoint.

    Each explanation node records its subject count.
    """
    validate_active_source(con, query)
    columns = table_columns(con, "dm")
    _required(columns, STUDY_ID, "DM")
    _required(columns, SUBJECT_ID, "DM")
    compiler = _Compiler(con, query)
    compiler.create_universe()
    plan = resolve_execution_plan(query)
    universe_keys = compiler.universe_keys
    if plan.root is None:
        return PredicateEvaluation(
            universe_keys,
            universe_keys,
            (
                NodeExplanation(
                    "predicate",
                    "universe",
                    len(universe_keys),
                    {"reason": "missing_predicate"},
                ),
            ),
            assumptions=("missing predicate selects the complete DM subject universe",),
        )
    membership = compiler.compile(plan.root)
    masks = compiler.evaluate()
    return PredicateEvaluation(
        compiler.keys(masks[membership]),
        universe_keys,
        compiler.explanations(masks),
        tuple(compiler.warnings),
        tuple(compiler.assumptions),
    )


def _subjects_by_study(subjects: Sequence[Any], select: Callable[[Any], bool]) -> dict[str, int]:
    return dict(Counter(item.study_id for item in subjects if select(item)))


def selection_for_keys(
    con: Any,
    keys: Sequence[tuple[str, str]],
    *,
    universe_keys: Sequence[tuple[str, str]],
) -> CohortSelection:
    requested = tuple(keys)
    if len(requested) != len(set(requested)):
        raise SourceDataError("predicate evaluation produced duplicate subject keys")
    if python_derived_tables_current(con):
        contexts_by_key = _stored_contexts(con, requested, universe_keys)
    else:
        contexts_by_key = _computed_contexts(con, requested, universe_keys)
    subjects = tuple(contexts_by_key[key] for key in requested)
    evidence_statuses = Counter(subject.dose_duration_evidence_status for subject in subjects)
    exposure_statuses = Counter(subject.exposure_interval_status for subject in subjects)
    incomplete_declarations = sum(
        subject.dose_duration_declaration_incomplete for subject in subjects
    )
    warnings: list[StageWarning] = []
    if incomplete_declarations:
        warnings.append(
            StageWarning(
                "dose_duration_declaration_unnormalized",
                f"{incomplete_declarations} matched subject(s) have DOSDUR values that could not be "
                "normalized. The planned duration used only the normalized declarations.",
                incomplete_declarations,
                _subjects_by_study(subjects, lambda item: item.dose_duration_declaration_incomplete),
            )
        )
    for status in ("ambiguous", "unresolved"):
        count = evidence_statuses.get(status, 0)
        if count:
            warnings.append(
                StageWarning(
                    f"dose_duration_{status}",
                    f"{count} matched subject(s) have {status} planned dose-duration evidence.",
                    count,
                    _subjects_by_study(
                        subjects, lambda item, status=status: item.dose_duration_evidence_status == status
                    ),
                )
            )
    for status in ("gapped", "incomplete"):
        count = exposure_statuses.get(status, 0)
        if count:
            warnings.append(
                StageWarning(
                    f"exposure_interval_{status}",
                    f"{count} matched subject(s) have {status} EX interval evidence. "
                    "The observed exposure span was not resolved.",
                    count,
                    _subjects_by_study(
                        subjects, lambda item, status=status: item.exposure_interval_status == status
                    ),
                )
            )
    necropsy_dated = [subject for subject in subjects if subject.terminal_record_source == "necropsy"]
    if necropsy_dated:
        warnings.append(
            StageWarning(
                "terminal_day_from_necropsy",
                f"{len(necropsy_dated)} matched subject(s) have no disposition record in the source. "
                "Their terminal day is their necropsy day: the earliest of the latest MI, MA and OM study days.",
                len(necropsy_dated),
                _subjects_by_study(necropsy_dated, lambda item: True),
            )
        )
    inconsistent = [
        subject for subject in necropsy_dated
        if (subject.pathology_day_spread_days or 0) > PATHOLOGY_DAY_TOLERANCE_DAYS
    ]
    if inconsistent:
        warnings.append(
            StageWarning(
                "pathology_days_inconsistent",
                f"{len(inconsistent)} matched subject(s) without a disposition record have latest MI, MA "
                f"and OM study days that differ by more than {PATHOLOGY_DAY_TOLERANCE_DAYS} days. "
                "A pathology day of such a subject is not its necropsy day.",
                len(inconsistent),
                _subjects_by_study(inconsistent, lambda item: True),
            )
        )
    return CohortSelection(
        subjects=subjects,
        warnings=tuple(warnings),
        dose_duration_evidence_status_counts={
            status: evidence_statuses.get(status, 0)
            for status in DOSE_DURATION_EVIDENCE_STATUSES
        },
        exposure_interval_status_counts={
            status: exposure_statuses.get(status, 0)
            for status in _EXPOSURE_INTERVAL_STATUSES
        },
        dose_duration_declaration_incomplete_subject_count=incomplete_declarations,
    )


def _computed_contexts(
    con: Any,
    requested: tuple[tuple[str, str], ...],
    universe_keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], SubjectContext]:
    contexts_by_key: dict[tuple[str, str], SubjectContext] = {}
    for context in _load_subject_contexts(con):
        key = (context.study_id, context.subject_id)
        if key in contexts_by_key:
            raise SourceDataError("subject context assembly produced duplicate subject keys")
        contexts_by_key[key] = context
    actual_universe = set(contexts_by_key)
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
    return {key: contexts_by_key[key] for key in requested}


def _stored_contexts(
    con: Any,
    requested: tuple[tuple[str, str], ...],
    universe_keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], SubjectContext]:
    """Check the stored context universe in SQL, then read the requested rows."""

    table = quote_ident(SUBJECT_CONTEXT_TABLE)
    total, distinct = con.execute(
        f"SELECT count(*), count(DISTINCT (study_id, subject_id)) FROM {table}"
    ).fetchone()
    if total != distinct:
        raise SourceDataError("subject context assembly produced duplicate subject keys")
    expected_universe = sorted(set(universe_keys))
    expected_frame = pd.DataFrame.from_records(
        expected_universe, columns=["study_id", "subject_id"]
    ).astype(str)
    with registered_frame(con, expected_frame) as expected:
        (shared,) = con.execute(
            f"SELECT count(*) FROM {table} AS stored "
            f"JOIN {expected} AS expected USING (study_id, subject_id)"
        ).fetchone()
    if shared != total or shared != len(expected_universe):
        raise SourceDataError(
            "subject context keys do not match the DM universe: "
            f"missing={len(expected_universe) - shared}, extra={total - shared}"
        )
    contexts_by_key = load_stored_subject_contexts(con, SUBJECT_CONTEXT_TABLE, requested)
    missing = len(requested) - len(contexts_by_key)
    if missing:
        raise SourceDataError(
            f"predicate membership contains {missing} subject key(s) outside the DM universe"
        )
    return contexts_by_key


@dataclass(frozen=True)
class _Leaf:
    """Source rows of one rule, lifted to universe subjects.

    ``source`` is a FROM clause body. ``study`` and ``subject`` are the key
    expressions of a row; without ``subject`` the rule has study grain, so a
    matching row selects every universe subject of its study.
    """

    source: str
    study: str
    subject: str | None
    predicate: str
    params: tuple[Any, ...]


_SetOp: TypeAlias = tuple[str, Any]


class _Compiler:
    """Compile a predicate to subject sets over the DM universe.

    Every set is a boolean mask over the universe subjects, sorted by key.
    The rules that read the same source rows are evaluated in one scan with
    one aggregate column per rule (``_run_leaves``); groups combine masks.
    """

    def __init__(self, con: Any, query: SubjectQuery) -> None:
        self.con, self.query, self.universe = con, query, "_vcgen_predicate_v1_universe"
        self.sets: list[_SetOp] = []
        self.pending: list[tuple[str, NodeKind, Mapping[str, Any], int]] = []
        self.warnings: list[StageWarning] = []
        self.universe_keys: tuple[tuple[str, str], ...] = ()
        self._columns: dict[str, list[str]] = {}
        self.pre_study_sexes: dict[tuple[str, str], str] | None = None
        self.pre_study_values: dict[str, dict[tuple[str, str], dict[str, float]]] = {}
        self.pre_study_stored: bool | None = None
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

    def columns(self, relation: str) -> list[str]:
        if relation not in self._columns:
            self._columns[relation] = table_columns(self.con, relation)
        return self._columns[relation]

    def create_universe(self) -> None:
        """The distinct DM subject keys, numbered in key order."""
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE {quote_ident(self.universe)} AS "
            "SELECT (row_number() OVER (ORDER BY study_id, subject_id) - 1)::INTEGER AS rid, "
            "study_id, subject_id FROM (SELECT DISTINCT "
            f"cast({q(STUDY_ID)} AS VARCHAR) study_id, cast({q(SUBJECT_ID)} AS VARCHAR) subject_id FROM dm)"
        )
        fetched = self.con.execute(
            f"SELECT study_id, subject_id FROM {quote_ident(self.universe)} ORDER BY rid"
        ).fetchall()
        self.universe_keys = tuple((str(study), str(subject)) for study, subject in fetched)

    def compile(self, plan: ResolvedExecutionNode) -> int:
        return self.group(plan) if isinstance(plan, GroupExecutionPlan) else self.rule(plan)

    def add(self, op: str, payload: Any) -> int:
        self.sets.append((op, payload))
        return len(self.sets) - 1

    def leaf(self, source: str, study: str, subject: str | None, predicate: str, params: Sequence[Any]) -> int:
        return self.add("leaf", _Leaf(source, study, subject, predicate, tuple(params)))

    def node(self, handle: int, path: str, kind: NodeKind, details: Mapping[str, Any]) -> None:
        self.pending.append((path, kind, details, handle))

    def group(self, plan: GroupExecutionPlan) -> int:
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
                relations.append(self.source_group(domain_rules, group, path, negated=negated))
            elif len(domain_rules) == 1:
                relations.append(self.rule(child))
            else:
                relations.append(self.source_group(domain_rules, group, path))

        handle = self.add(group, tuple(relations))
        if negated and not fully_row_scoped:
            handle = self.add("not", handle)
        self.node(
            handle,
            path,
            "group",
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
        return handle

    def source_group(
        self,
        rules: Sequence[RuleExecutionPlan],
        group: PredicateGroupOperator,
        path: str,
        *,
        negated: bool = False,
    ) -> int:
        domain = rules[0].source_domain
        if domain is None:
            raise ValueError("source group requires direct SEND rules")
        columns = self.columns(domain.lower())
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

        row_predicate = f" {group.upper()} ".join(f"({item})" for item in predicates)
        if negated:
            row_predicate = f"NOT ({row_predicate})"
        handle = self.leaf(
            f"{quote_ident(domain.lower())} rows",
            f"rows.{q(study)}",
            None if subject is None else f"rows.{q(subject)}",
            row_predicate,
            params,
        )
        self.node(
            handle,
            f"{path}.domain[{domain}]",
            "group",
            {
                "domain": domain,
                "group": group,
                "negated": negated,
                "scope": "source_row",
                "rule_paths": [rule.path for rule in rules],
            },
        )
        return handle

    def rule(self, plan: RuleExecutionPlan) -> int:
        node = plan.node
        field, operator, value = node.field, node.operator, node.value
        if isinstance(field, SendColumnField):
            handle = self.source(field, operator, value)
        elif isinstance(field, DerivedField):
            handle = self.context(field.name, operator, value, field.value_type)
        else:
            handle = self.pre_study(field, operator, value)
            self.node(
                handle,
                plan.path,
                "rule",
                {"field_kind": field.kind, "operator": operator, "endpoint": field.endpoint},
            )
            return handle
        self.node(handle, plan.path, "rule", {"field_kind": field.kind, "operator": operator})
        return handle

    def source(self, field: SendColumnField, operator: CanonicalOperator, value: Any) -> int:
        domain, column = field.domain, field.column
        columns = self.columns(domain.lower())
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
        return self.leaf(
            f"{quote_ident(domain.lower())} rows",
            f"rows.{q(study)}",
            None if subject is None else f"rows.{q(subject)}",
            predicate,
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
    ) -> int:
        if name == "study_start_year":
            return self.study_start_year(operator, value, kind)
        specification = _DERIVED.get(name)
        if specification is None:
            raise ValueError(f"unsupported derived field: {name!r}")
        view, source_name, subject_grain, list_value = specification
        columns = self.columns(view)
        if not columns:
            self.warnings.append(
                StageWarning(
                    "derived_view_unavailable",
                    f"The source data has no derived view for the field {name}, so {name} matches no subjects.",
                )
            )
            return self.add("empty", None)
        source = _required(columns, source_name, view)
        study = _required(columns, STUDY_ID, view)
        subject = _find(columns, SUBJECT_ID)
        if subject_grain and subject is None:
            raise ValueError(f"derived field {name!r} lacks subject identity")

        if name == "route":
            routes = load_route_aliases().subject_routes_sql(
                quote_ident(view), q(source), q(study), q(subject)
            )
            predicate, params = _predicate("routes.route", operator, value, kind)
            return self.leaf(f"{routes} routes", "routes.study", "routes.subject", predicate, params)
        if name == "dose_frequency":
            frequencies = load_frequency_aliases().subject_frequencies_sql(
                quote_ident(view), q(source), q(study), q(subject)
            )
            predicate, params = _predicate("frequencies.frequency", operator, value, kind)
            return self.leaf(
                f"{frequencies} frequencies", "frequencies.study", "frequencies.subject", predicate, params
            )
        subject_key = f"rows.{q(subject)}" if subject_grain else None
        if list_value:
            if name == "facility_group":
                register_facility_group_function(self.con)
                actual = "vcgen_facility_group(cast(candidate.value AS VARCHAR))"
            else:
                actual = (
                    "candidate.value"
                    if kind == "number"
                    else "upper(cast(candidate.value AS VARCHAR))"
                )
            predicate, params = _predicate(actual, operator, value, kind)
            return self.leaf(
                f"{quote_ident(view)} rows CROSS JOIN UNNEST(rows.{q(source)}) AS candidate(value)",
                f"rows.{q(study)}",
                subject_key,
                "candidate.value IS NOT NULL "
                f"AND trim(cast(candidate.value AS VARCHAR)) <> '' AND ({predicate})",
                params,
            )

        predicate, params = _predicate(f"rows.{q(source)}", operator, value, kind)
        return self.leaf(f"{quote_ident(view)} rows", f"rows.{q(study)}", subject_key, predicate, params)

    def study_start_year(self, operator: CanonicalOperator, value: Any, kind: ValueType) -> int:
        rows = study_start_year_sql(self.columns("dm"))
        if rows is None:
            self.warnings.append(
                StageWarning(
                    "study_start_unavailable",
                    "The DM domain has no RFSTDTC column, so study_start_year matches no subjects.",
                )
            )
            return self.add("empty", None)
        predicate, params = _predicate("rows.actual", operator, value, kind)
        return self.leaf(f"({rows}) rows", "rows.study_id", None, predicate, params)

    def pre_study(
        self, field: PreStudyEndpointField, operator: CanonicalOperator, value: Any
    ) -> int:
        name = field.endpoint
        if self.pre_study_stored is None:
            self.pre_study_stored = python_derived_tables_current(self.con)
        if self.pre_study_stored:
            # Stored rows hold finite values only, as the computed path keeps.
            # Compare the stored DOUBLE directly; a text round trip could change it.
            predicate, params = (
                _predicate("stored.value", operator, value, "number")
                if operator in {"is_empty", "is_not_empty"}
                else _compare("stored.value", "isfinite(stored.value)", operator, value, True)
            )
            return self.leaf(
                f"{quote_ident(PRE_STUDY_ENDPOINT_TABLE)} stored",
                f"stored.{q(STUDY_ID)}",
                f"stored.{q(SUBJECT_ID)}",
                f"stored.endpoint_name = ? AND ({predicate})",
                [name, *params],
            )
        from .endpoint_catalogue import canonical_endpoint

        if name not in self.pre_study_values:
            if self.pre_study_sexes is None:
                self.pre_study_sexes = subject_sexes(self.con)
            self.pre_study_values[name] = _load_pre_study_endpoint_values(
                self.con, self.pre_study_sexes, (canonical_endpoint(name).spec,),
            )
        matches = _python_matcher(operator, value, "number")
        return self.add(
            "keys",
            frozenset(key for key, resolved in self.pre_study_values[name].items() if matches(resolved.get(name))),
        )

    def evaluate(self) -> list[np.ndarray]:
        """The mask of every set; the sets of one source scan come from one statement."""
        size = len(self.universe_keys)
        masks: list[np.ndarray | None] = [None] * len(self.sets)
        batches: dict[tuple[str, str, str | None], list[int]] = {}
        for handle, (op, payload) in enumerate(self.sets):
            if op == "leaf":
                batches.setdefault((payload.source, payload.study, payload.subject), []).append(handle)
        for (source, study, subject), handles in batches.items():
            for handle, mask in zip(handles, self._run_leaves(source, study, subject, handles, size), strict=True):
                masks[handle] = mask
        index: dict[tuple[str, str], int] | None = None
        for handle, (op, payload) in enumerate(self.sets):
            if op == "leaf":
                continue
            if op == "empty":
                masks[handle] = np.zeros(size, dtype=bool)
            elif op == "keys":
                if index is None:
                    index = {key: position for position, key in enumerate(self.universe_keys)}
                mask = np.zeros(size, dtype=bool)
                mask[[index[key] for key in payload if key in index]] = True
                masks[handle] = mask
            elif op == "and":
                masks[handle] = np.logical_and.reduce([masks[child] for child in payload])
            elif op == "or":
                masks[handle] = np.logical_or.reduce([masks[child] for child in payload])
            elif op == "not":
                masks[handle] = ~masks[payload]
            else:
                raise ValueError(f"unsupported subject-set operation: {op!r}")
        return masks  # type: ignore[return-value]

    def _run_leaves(
        self, source: str, study: str, subject: str | None, handles: Sequence[int], size: int
    ) -> list[np.ndarray]:
        leaves: list[_Leaf] = [self.sets[handle][1] for handle in handles]
        keys = [f"cast({study} AS VARCHAR) AS study_id"]
        on = ["universe.study_id = matched.study_id"]
        if subject is not None:
            keys.append(f"cast({subject} AS VARCHAR) AS subject_id")
            on.append("universe.subject_id = matched.subject_id")
        flags = [f"bool_or(coalesce(({leaf.predicate}), false)) AS c{position}" for position, leaf in enumerate(leaves)]
        fetched = self.con.execute(
            "SELECT universe.rid, "
            + ", ".join(f"matched.c{position}" for position in range(len(leaves)))
            + f" FROM {quote_ident(self.universe)} universe JOIN ("
            f"SELECT {', '.join(keys)}, {', '.join(flags)} FROM {source} GROUP BY ALL) matched "
            f"ON {' AND '.join(on)}",
            [param for leaf in leaves for param in leaf.params],
        ).fetchnumpy()
        rids = np.asarray(fetched["rid"], dtype=np.int64)
        masks = []
        for position in range(len(leaves)):
            mask = np.zeros(size, dtype=bool)
            mask[rids[np.asarray(fetched[f"c{position}"], dtype=bool)]] = True
            masks.append(mask)
        return masks

    def keys(self, mask: np.ndarray) -> tuple[tuple[str, str], ...]:
        return tuple(self.universe_keys[position] for position in np.flatnonzero(mask))

    def explanations(self, masks: Sequence[np.ndarray]) -> tuple[NodeExplanation, ...]:
        nodes = []
        for path, kind, details, handle in self.pending:
            mask = masks[handle]
            nodes.append(NodeExplanation(path, kind, int(mask.sum()), details))
        return tuple(nodes)


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


def _python_matcher(op: str, value: Any, kind: str) -> Callable[[Any], bool]:
    """Return the test of one candidate; the rule value is checked once."""
    if op == "is_empty":
        return lambda candidate: candidate is None or candidate == ""
    if op == "is_not_empty":
        return lambda candidate: candidate is not None and candidate != ""
    numeric = kind == "number"
    test: Callable[[Any], bool] | None = None

    def match(candidate: Any) -> bool:
        nonlocal test
        if candidate is None:
            return False
        if numeric and (
            isinstance(candidate, bool)
            or not isinstance(candidate, (int, float))
            or not math.isfinite(candidate)
        ):
            return False
        if test is None:
            # The first candidate that reaches the rule checks its value, as before.
            test = _candidate_test(op, value, numeric)
        return test(candidate)

    return match


def _candidate_test(op: str, value: Any, numeric: bool) -> Callable[[Any], bool]:
    if op == "eq":
        return lambda candidate: candidate == value
    if op == "ne":
        return lambda candidate: candidate != value
    if op == "gt":
        return lambda candidate: candidate > value
    if op == "gte":
        return lambda candidate: candidate >= value
    if op == "lt":
        return lambda candidate: candidate < value
    if op == "lte":
        return lambda candidate: candidate <= value
    if op in {"between", "not_between"}:
        low, high = _bounds(value, numeric)
        return lambda candidate: (low <= candidate <= high) is (op == "between")
    if op in {"in", "not_in"}:
        items = _items(value, numeric)
        return lambda candidate: (candidate in items) is (op == "in")
    if op == "contains":
        return lambda candidate: str(value) in str(candidate)
    if op == "starts_with":
        return lambda candidate: str(candidate).startswith(str(value))
    if op == "ends_with":
        return lambda candidate: str(candidate).endswith(str(value))
    if op in {"not_contains", "not_starts_with", "not_ends_with"}:
        inner = _candidate_test(op.removeprefix("not_"), value, numeric)
        return lambda candidate: not inner(candidate)

    def unsupported(_candidate: Any) -> bool:
        raise ValueError(f"unsupported canonical operator: {op!r}")

    return unsupported


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




def study_start_year_sql(dm_columns: Sequence[str]) -> str | None:
    """Return one row per study with its start year as ``actual``.

    The year comes from DM RFSTDTC. A study has a year only when every subject
    records the same year; otherwise ``actual`` is NULL. Returns None when DM
    has no RFSTDTC column.
    """

    reference_date = _find(dm_columns, "RFSTDTC")
    if reference_date is None:
        return None
    study = _required(dm_columns, STUDY_ID, "DM")
    year = (
        f"try_cast(regexp_extract(cast({q(reference_date)} AS VARCHAR), "
        "'^[0-9]{4}', 0) AS INTEGER)"
    )
    return (
        f"SELECT cast({q(study)} AS VARCHAR) AS study_id, "
        "CASE WHEN count(*) > 0 "
        f"AND count({year}) = count(*) AND min({year}) = max({year}) "
        f"THEN min({year}) END AS actual "
        f"FROM dm GROUP BY {q(study)}"
    )


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
