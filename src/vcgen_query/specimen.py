"""Specimen vocabulary: a raw SEND specimen string to a specimen class and matrix."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
import re
from typing import Any

import yaml

from .contract_helpers import QueryError, packaged_data

SPECIMEN_VOCABULARY_CONTRACT = "vcgen.specimen_vocabulary.v1"
SPECIMEN_VOCABULARY_FILENAME = "specimen_vocabulary.v1.yaml"
MISSING_SPECIMEN = "MISSING"
UNRESOLVED_SPECIMEN = "UNRESOLVED"
# Classes that do not name a real specimen. A class rule never accepts them.
NOT_A_SPECIMEN = frozenset({"UNKNOWN", MISSING_SPECIMEN, UNRESOLVED_SPECIMEN})


@dataclass(frozen=True)
class SpecimenVocabulary:
    """Ordered regex rules from a raw specimen string to a specimen class and matrix."""

    rules: tuple[tuple[re.Pattern[str], str, str | None], ...]
    sha256: str

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any], sha256: str) -> SpecimenVocabulary:
        if document.get("contract") != SPECIMEN_VOCABULARY_CONTRACT:
            raise ValueError(f"specimen vocabulary contract must be {SPECIMEN_VOCABULARY_CONTRACT}")
        if set(document) != {"contract", "rules"}:
            raise ValueError("specimen vocabulary has incomplete or unsupported fields")
        rules = []
        for item in document["rules"]:
            if not isinstance(item, Mapping) or set(item) != {"pattern", "class", "matrix"}:
                raise ValueError(f"specimen vocabulary rule is invalid: {item!r}")
            rules.append((re.compile(item["pattern"]), str(item["class"]), item["matrix"]))
        return cls(tuple(rules), sha256)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SpecimenVocabulary:
        raw = Path(path).read_bytes()
        return cls.from_mapping(yaml.safe_load(raw), hashlib.sha256(raw).hexdigest())

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(specimen_class for _, specimen_class, _ in self.rules) | {
            MISSING_SPECIMEN, UNRESOLVED_SPECIMEN,
        }

    def classify(self, raw: Any) -> tuple[str, str | None]:
        if raw is None or not str(raw).strip():
            return MISSING_SPECIMEN, None
        text = re.sub(r"\s+", " ", str(raw)).strip().upper()
        for pattern, specimen_class, matrix in self.rules:
            if pattern.search(text):
                return specimen_class, matrix
        return UNRESOLVED_SPECIMEN, None


@lru_cache(maxsize=1)
def load_specimen_vocabulary() -> SpecimenVocabulary:
    raw, sha256 = packaged_data(SPECIMEN_VOCABULARY_FILENAME)
    return SpecimenVocabulary.from_mapping(yaml.safe_load(raw), sha256)


@lru_cache(maxsize=4096)
def specimen_class(raw: str | None) -> str:
    """Return the packaged specimen class of one raw specimen string."""

    return load_specimen_vocabulary().classify(raw)[0]


_PANEL_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def specimen_panel_id(value: Any, location: str) -> str:
    """Validate a specimen panel identifier (lowercase snake case)."""

    if not isinstance(value, str) or not _PANEL_ID.fullmatch(value):
        raise QueryError(f"{location} must be a lowercase specimen panel identifier.")
    return value


@lru_cache(maxsize=256)
def specimen_matrix(specimen_class: str) -> str | None:
    """Return the sample matrix of a specimen class (BLOOD for SERUM, PLASMA, WHOLE_BLOOD)."""

    for _, rule_class, matrix in load_specimen_vocabulary().rules:
        if rule_class == specimen_class:
            return matrix
    return None


def real_specimen_classes() -> frozenset[str]:
    return load_specimen_vocabulary().classes - NOT_A_SPECIMEN


__all__ = [
    "NOT_A_SPECIMEN",
    "SPECIMEN_VOCABULARY_CONTRACT",
    "SPECIMEN_VOCABULARY_FILENAME",
    "SpecimenVocabulary",
    "load_specimen_vocabulary",
    "real_specimen_classes",
    "specimen_class",
    "specimen_matrix",
    "specimen_panel_id",
]
