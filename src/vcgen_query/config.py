"""Loading and locally bundled schema access for query contracts."""

from __future__ import annotations
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .contract import Query, QueryError, parse_query
from .identities import (
    COHORT_QUERY_CONTRACT,
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
    SUBJECT_PREDICATE_CONTRACT,
)

SCHEMA_RESOURCES = {
    GRIT42_FORM_SUBJECT_QUERY_CONTRACT: "data/grit42.form_subject_query.v1.schema.json",
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT: "data/grit42.advanced_subject_query.v1.schema.json",
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT: "data/grit42.subject_query_import.v1.schema.json",
    SUBJECT_PREDICATE_CONTRACT: "data/vcgen.subject_predicate.v1.schema.json",
    COHORT_QUERY_CONTRACT: "data/vcgen.cohort_query.v5.schema.json",
}


def load_query_mapping(path: str | Path) -> Mapping[str, Any]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QueryError(f"cannot read query file {path}: {error}") from error
    if path.suffix.lower() == ".json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise QueryError(f"invalid JSON query file {path}: {error}") from error
    elif path.suffix.lower() in {".yaml", ".yml"}:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise QueryError(f"invalid YAML query file {path}: {error}") from error
    else:
        raise QueryError(f"query file {path} must use .json, .yaml, or .yml.")
    if not isinstance(raw, Mapping):
        raise QueryError(f"query file {path} must contain an object.")
    return raw


def load_query(path: str | Path) -> Query:
    return parse_query(load_query_mapping(path))


def schema_dict(contract: str) -> dict[str, Any]:
    try:
        resource = SCHEMA_RESOURCES[contract]
    except KeyError as error:
        raise QueryError(f"unsupported schema contract {contract!r}.") from error
    try:
        return json.loads(
            resources.files("vcgen_query").joinpath(resource).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise QueryError(f"unable to load bundled schema {contract!r}: {error}") from error


def schema_resources() -> dict[str, dict[str, Any]]:
    schemas = (schema_dict(contract) for contract in SCHEMA_RESOURCES)
    return {schema["$id"]: schema for schema in schemas}


__all__ = [
    "SCHEMA_RESOURCES",
    "load_query",
    "load_query_mapping",
    "schema_dict",
    "schema_resources",
]
