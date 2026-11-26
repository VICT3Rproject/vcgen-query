"""VCGen typed cohort-query API."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .config import (
    load_cohort_query,
    load_query_mapping,
    load_subject_query,
    schema_dict,
    schema_resources,
)
from .contract_helpers import SourceDataError
from .contract import (
    CanonicalOperator,
    DERIVED_FIELD_TYPES,
    DerivedField,
    DerivedFieldName,
    EndpointSpec,
    FieldRef,
    PreStudyEndpointField,
    Predicate,
    PredicateGroup,
    PredicateGroupOperator,
    PredicateRule,
    QueryError,
    QueryImportProvenance,
    SendColumnField,
    SpecimenMatch,
    ValueType,
    normalize_category,
    normalize_operator,
    parse_field,
    parse_predicate,
)
from .query import (
    SOURCE_STAGE,
    SUBJECT_CONTEXT_STAGE,
    SUPPORT_SELECTION_STAGE,
    CohortQuery,
    EndpointExclusion,
    EndpointExclusionEntry,
    EndpointObservations,
    QueryStage,
    StageChange,
    StageInfo,
    StageDrop,
    TraceItem,
    trace_items,
    StageKind,
    StageOrigin,
    StageRecord,
    StageRefusal,
    StageWarning,
    REFUSAL_CODES,
    StudyExclusion,
    StudyExclusionEntry,
    WARNING_CODES,
    SubjectQuery,
    TraceUnit,
    parse_cohort_query,
    parse_subject_query,
    thaw,
)
from .fingerprint import (
    fingerprint_observation_contract,
    fingerprint_predicate,
    predicate_semantic_json,
    predicate_semantic_payload,
)
from .grit42 import (
    import_grit42_advanced_subject_query,
    import_grit42_form_subject_query,
    import_grit42_subject_query,
)
from .grit42_catalogue import (
    GRIT42_CATALOGUE_FILENAME,
    GRIT42_CATALOGUE_ID,
    grit42_column,
)
from .endpoint_catalogue import (
    ENDPOINT_CATALOGUE_CONTRACT,
    CanonicalEndpoint,
    EndpointDerivation,
    canonical_endpoint,
    endpoint_catalogue_from_mapping,
    endpoint_catalogue_identity,
    endpoint_dependency_closure,
    load_endpoint_catalogue,
    specimen_panels,
    SpecimenPanel,
    resolve_endpoint_specs,
)
from .identities import (
    COHORT_QUERY_CONTRACT,
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
    SUBJECT_PREDICATE_CONTRACT,
    SUBJECT_QUERY_CONTRACT,
)

__version__ = "0.5.0a0"
__all__ = [
    "CanonicalOperator",
    "CohortQuery",
    "EndpointExclusion",
    "EndpointExclusionEntry",
    "EndpointObservations",
    "QueryStage",
    "SOURCE_STAGE",
    "SUBJECT_CONTEXT_STAGE",
    "SUPPORT_SELECTION_STAGE",
    "SUBJECT_QUERY_CONTRACT",
    "StageChange",
    "StageInfo",
    "StageDrop",
    "TraceItem",
    "trace_items",
    "StageKind",
    "StageOrigin",
    "StageRecord",
    "StageRefusal",
    "StageWarning",
    "REFUSAL_CODES",
    "StudyExclusion",
    "StudyExclusionEntry",
    "WARNING_CODES",
    "SubjectQuery",
    "TraceUnit",
    "COHORT_QUERY_CONTRACT",
    "GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT",
    "GRIT42_CATALOGUE_FILENAME",
    "GRIT42_CATALOGUE_ID",
    "GRIT42_FORM_SUBJECT_QUERY_CONTRACT",
    "GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT",
    "SUBJECT_PREDICATE_CONTRACT",
    "DERIVED_FIELD_TYPES",
    "DerivedField",
    "DerivedFieldName",
    "CanonicalEndpoint",
    "EndpointDerivation",
    "ENDPOINT_CATALOGUE_CONTRACT",
    "EndpointFacts",
    "EndpointSpec",
    "FieldRef",
    "PreStudyEndpointField",
    "Predicate",
    "PredicateGroup",
    "PredicateGroupOperator",
    "PredicateRule",
    "QueryError",
    "QueryImportProvenance",
    "QueryMetadata",
    "QueryMembership",
    "QueryReceipt",
    "QueryResult",
    "StudySupport",
    "ResultColumn",
    "ResultTable",
    "SendColumnField",
    "SpecimenMatch",
    "SourceDataError",
    "ValueType",
    "__version__",
    "execute_query",
    "evaluate_membership",
    "fingerprint_observation_contract",
    "fingerprint_predicate",
    "grit42_column",
    "canonical_endpoint",
    "endpoint_catalogue_from_mapping",
    "endpoint_catalogue_identity",
    "endpoint_dependency_closure",
    "load_endpoint_catalogue",
    "specimen_panels",
    "SpecimenPanel",
    "resolve_endpoint_specs",
    "predicate_semantic_json",
    "predicate_semantic_payload",
    "load_cohort_query",
    "load_subject_query",
    "load_query_mapping",
    "normalize_category",
    "normalize_operator",
    "parse_field",
    "parse_predicate",
    "parse_cohort_query",
    "parse_subject_query",
    "thaw",
    "import_grit42_advanced_subject_query",
    "import_grit42_form_subject_query",
    "import_grit42_subject_query",
    "schema_dict",
    "schema_resources",
    "query_result_schema",
]


# Pure configuration loading must not import pandas or DuckDB. Runtime exports
# retain the public API and load their implementation only when requested.
_RUNTIME_EXPORTS = {'EndpointFacts': 'result', 'QueryMetadata': 'result', 'QueryMembership': 'result', 'QueryReceipt': 'result', 'QueryResult': 'result', 'StudySupport': 'result', 'evaluate_membership': 'result', 'execute_query': 'result', 'ResultColumn': 'table_schema', 'ResultTable': 'table_schema', 'query_result_schema': 'table_schema'}

def __getattr__(name: str):
    module = _RUNTIME_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_RUNTIME_EXPORTS))


if TYPE_CHECKING:
    from .result import (
        EndpointFacts as EndpointFacts,
        QueryMetadata as QueryMetadata,
        QueryMembership as QueryMembership,
        QueryReceipt as QueryReceipt,
        QueryResult as QueryResult,
        StudySupport as StudySupport,
        evaluate_membership as evaluate_membership,
        execute_query as execute_query,
    )
    from .table_schema import (
        ResultColumn as ResultColumn,
        ResultTable as ResultTable,
        query_result_schema as query_result_schema,
    )
