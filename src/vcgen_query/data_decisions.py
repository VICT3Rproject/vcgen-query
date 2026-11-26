"""Data decisions: reviewed per-study corrections of one source archive, applied during observation extraction.

Contract ``vcgen.data_decisions.v3``: the catalogue curation of a source archive
writes one ``data_decisions.yaml`` beside its ``catalogue.json``, by the endpoint
names of that catalogue. ``load_data_decisions`` reads the file that the caller
names through the catalogue lock (``load_locked_decisions``). With the bundled
endpoint catalogue there are no data decisions. The query never runs the rules;
it reads static lists.

Actions:

A row with an unknown specimen (NO DATA, blank, unresolved) whose LBCAT is a
category of another specimen panel of the catalogue takes no panel decision of
this panel: urine pH with NO DATA never becomes blood pH.

- ``impute_specimen``: rows of the cell with an unknown specimen (NO DATA,
  blank, unresolved) take ``value``, and so do rows of the classes in
  ``relabel``. A relabel class must have the sample matrix of ``value``, so an
  imputation never moves a row to another matrix (urine never becomes serum);
- ``exclude_panel``: the rows of the cell with an unknown specimen or a class of
  the panel's sample matrix are removed (rows of another matrix are left to
  the class rule);
- ``drop_rows``: rows of the cell whose specimen class is in ``specimens`` are
  removed;
- ``drop_method_rows``: rows of ``endpoint`` in the cell whose method names
  one of ``method_contains`` (case-insensitive) are removed. A study that
  records one analyte under the test code of another is found by its method;
- ``exclude_endpoint``: the rows of ``endpoint`` in the study are removed;
- ``reinterpret_unit``: a row of ``endpoint`` in the study whose unit (or
  implicit unit) is ``unit`` gets the unit ``reading``. The reading is a
  source unit of the endpoint, so the conversion uses its compiled rule.

Panel actions (the first three) are keyed by study x specimen panel and
apply to the endpoints of the panel. Endpoint actions (the other three) are
keyed by study x endpoint and apply to any endpoint, with or without a panel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
import hashlib
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from .config import YAML_LOADER
from .contract_helpers import QueryError
from .endpoint_catalogue import (
    EndpointCatalogue, SpecimenPanel, catalogue_location, load_endpoint_catalogue, load_locked_catalogue,
)
from .query import StageWarning
from .specimen import NOT_A_SPECIMEN, real_specimen_classes, specimen_matrix

DATA_DECISIONS_CONTRACT = "vcgen.data_decisions.v3"
# The data decisions of a catalogue curation, beside its catalogue.json.
DATA_DECISIONS_FILENAME = "data_decisions.yaml"
DecisionAction = Literal["impute_specimen", "exclude_panel", "drop_rows", "drop_method_rows", "exclude_endpoint",
                         "reinterpret_unit"]
PANEL_ACTIONS = ("impute_specimen", "exclude_panel", "drop_rows")
ENDPOINT_ACTIONS = ("drop_method_rows", "exclude_endpoint", "reinterpret_unit")
# The findings of the rules (SPEC-001) and of the exceptions file (SG-001, or SG-T01 in a fixture).
_CURATED_FINDING_ID = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9]+$")
_CURATED_TOPIC = "curated"


@dataclass(frozen=True)
class Finding:
    id: str
    title: str
    upstream: str
    evidence: str


@dataclass(frozen=True)
class Decision:
    finding: str
    action: DecisionAction
    study_id: str
    specimen_panel: str | None
    value: str | None = None
    specimens: tuple[str, ...] = ()
    relabel: tuple[str, ...] = ()
    endpoint: str | None = None
    method_contains: tuple[str, ...] = ()
    unit: str | None = None
    reading: str | None = None


@dataclass(frozen=True)
class SpecimenResolution:
    """What the decisions do to one row: keep it with a class, or remove it."""

    specimen_class: str | None
    outcome: Literal["source", "imputed", "removed"]
    finding: str | None = None
    action: DecisionAction | None = None


@dataclass(frozen=True)
class DataDecisions:
    """The decision files of one query execution.

    ``cells`` holds the panel actions by study x specimen panel;
    ``endpoint_cells`` the endpoint actions by study x endpoint.
    """

    topics: tuple[str, ...]
    reviewed_sources: Mapping[str, Mapping[str, str]]
    findings: Mapping[str, Finding]
    cells: Mapping[tuple[str, str], tuple[Decision, ...]]
    md5: Mapping[str, str]
    endpoint_cells: Mapping[tuple[str, str], tuple[Decision, ...]] = field(default_factory=dict)
    contract: str = DATA_DECISIONS_CONTRACT
    # Normalized LBCAT -> specimen panel, from the catalogue of the decisions.
    category_panels: Mapping[str, str] = field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "topics": [
                {
                    "topic": topic,
                    "md5": self.md5[topic],
                    "reviewed_source": self.reviewed_sources[topic]["source"],
                }
                for topic in self.topics
            ],
        }

    def studies(self) -> frozenset[str]:
        return frozenset(study for study, _ in (*self.cells, *self.endpoint_cells))

    def source_warnings(self, source_identity: str | None) -> tuple[StageWarning, ...]:
        """Warn when the active source is not the source the decisions were reviewed on."""

        warnings = []
        for topic in self.topics:
            reviewed = self.reviewed_sources[topic]
            if source_identity == reviewed["source"]:
                continue
            warnings.append(
                StageWarning(
                    "data_decisions_source_not_reviewed",
                    f"The data decisions for {topic} were reviewed on another version of the source data. "
                    "Studies added since then are not reviewed.",
                )
            )
        return tuple(warnings)

    def removal(self, study_id: str, endpoint: str | None, method: str | None) -> Decision | None:
        """The endpoint action that removes the row: ``exclude_endpoint``, or ``drop_method_rows`` of its method."""

        method_text = (method or "").upper()
        for decision in self.endpoint_cells.get((study_id, endpoint or ""), ()):
            if decision.action == "exclude_endpoint" or (decision.action == "drop_method_rows" and any(
                text in method_text for text in decision.method_contains
            )):
                return decision
        return None

    def reading(self, study_id: str, endpoint: str, unit: str | None) -> Decision | None:
        """The ``reinterpret_unit`` decision of a row of the endpoint in the study with this unit (or None)."""

        for decision in self.endpoint_cells.get((study_id, endpoint), ()):
            if decision.action == "reinterpret_unit" and unit is not None and decision.unit == unit:
                return decision
        return None

    def resolve(
        self, study_id: str, panel: str, source_class: str, allowed: Sequence[str],
        *, endpoint: str | None = None, method: str | None = None, category: str | None = None,
    ) -> SpecimenResolution:
        if (removal := self.removal(study_id, endpoint, method)) is not None:
            return SpecimenResolution(None, "removed", removal.finding, removal.action)
        decisions = self.cells.get((study_id, panel), ())
        if not decisions or (
            source_class in NOT_A_SPECIMEN and self.category_panels.get(category or "", panel) != panel
        ):
            return SpecimenResolution(source_class, "source")
        own_matrix = source_class in NOT_A_SPECIMEN or specimen_matrix(source_class) in {
            specimen_matrix(name) for name in allowed
        }
        for decision in decisions:
            if decision.action == "exclude_panel" and own_matrix:
                return SpecimenResolution(None, "removed", decision.finding, decision.action)
        for decision in decisions:
            if decision.action == "drop_rows" and source_class in decision.specimens:
                return SpecimenResolution(None, "removed", decision.finding, decision.action)
        for decision in decisions:
            if decision.action == "impute_specimen" and source_class not in allowed and (
                source_class in NOT_A_SPECIMEN or source_class in decision.relabel
            ):
                return SpecimenResolution(decision.value, "imputed", decision.finding, decision.action)
        return SpecimenResolution(source_class, "source")


EMPTY_DATA_DECISIONS = DataDecisions((), {}, {}, {}, {})


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value.strip()


def _fields(item: Any, required: set[str], optional: set[str], location: str) -> Mapping[str, Any]:
    if not isinstance(item, Mapping) or not required <= set(item) <= required | optional:
        raise ValueError(f"{location} has incomplete or unsupported fields")
    return item


def data_decisions_from_mapping(
    document: Mapping[str, Any], *, md5: str, catalogue: EndpointCatalogue | None = None
) -> DataDecisions:
    """Validate one decision document against ``catalogue``.

    A document names the endpoints of the catalogue of its catalogue curation,
    so ``catalogue`` is that catalogue (the active catalogue when None).
    """

    if not isinstance(document, Mapping) or document.get("contract") != DATA_DECISIONS_CONTRACT:
        raise ValueError(f"data decisions contract must be {DATA_DECISIONS_CONTRACT}")
    catalogue = catalogue or load_endpoint_catalogue()
    merged = merge_data_decisions((_parse_curated(document, md5, catalogue),))
    return replace(merged, category_panels={name: panel.id for panel in catalogue.specimen_panels.values()
                                            for name in panel.categories})


def _findings(raw: Any, pattern: re.Pattern[str]) -> dict[str, Finding]:
    findings: dict[str, Finding] = {}
    for index, item in enumerate(raw or ()):
        location = f"findings[{index}]"
        item = _fields(item, {"id", "title", "upstream", "evidence"}, set(), location)
        finding_id = _text(item["id"], f"{location}.id")
        if not pattern.fullmatch(finding_id) or finding_id in findings:
            raise ValueError(f"{location}.id is invalid or repeated")
        findings[finding_id] = Finding(finding_id, *(_text(item[key], f"{location}.{key}")
                                                     for key in ("title", "upstream", "evidence")))
    return findings


def _texts(raw: Any, location: str, *, upper: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        raise ValueError(f"{location} must be a non-empty list")
    return tuple(sorted(_text(value, f"{location}[]").upper() if upper else _text(value, f"{location}[]")
                        for value in raw))


def _panel_fields(item: Mapping[str, Any], panel: SpecimenPanel, location: str
                  ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """The value, specimens, and relabel classes of a panel action."""

    value = None
    specimens: tuple[str, ...] = ()
    relabel: tuple[str, ...] = ()
    if item["action"] == "impute_specimen":
        value = _text(item["value"], f"{location}.value")
        if value not in panel.classes:
            raise ValueError(f"{location}.value {value!r} is not a class of {panel.id}")
        relabel = tuple(sorted(_text(name, f"{location}.relabel[]") for name in item.get("relabel", ())))
        for name in relabel:
            if name not in real_specimen_classes() or name in panel.classes:
                raise ValueError(f"{location}.relabel {name!r} must be a real class outside {panel.id}")
            if specimen_matrix(name) != specimen_matrix(value):
                raise ValueError(f"{location}.relabel {name!r} has another sample matrix than {value!r}")
    if item["action"] == "drop_rows":
        specimens = _texts(item["specimens"], f"{location}.specimens")
    return value, specimens, relabel


_EXTRA = {"impute_specimen": {"value"}, "drop_rows": {"specimens"}, "exclude_panel": set(),
          "drop_method_rows": {"endpoint", "method_contains"}}


_CURATED_EXTRA = {**_EXTRA, "exclude_endpoint": {"endpoint"}, "reinterpret_unit": {"endpoint", "unit", "reading"}}
# Review evidence of a decision; the query does not read it.
_CURATED_EVIDENCE = {"rows", "study_median", "centre"}


def _parse_curated(document: Mapping[str, Any], md5: str, catalogue: EndpointCatalogue
              ) -> tuple[str, Mapping[str, str], dict[str, Finding], list[Decision], str]:
    """A curated decision file; every endpoint, panel, unit, and reading is checked against ``catalogue`` (C26)."""

    document = _fields(document, {"contract", "source_snapshot_identity", "exceptions_sha256", "findings", "decisions"},
                       set(),
                       "data decisions")
    reviewed = {"source": _text(document["source_snapshot_identity"], "source_snapshot_identity"),
                "exceptions_sha256": _text(document["exceptions_sha256"], "exceptions_sha256")}
    findings = _findings(document["findings"], _CURATED_FINDING_ID)
    decisions = []
    for index, item in enumerate(document["decisions"] or ()):
        location = f"decisions[{index}]"
        action = item.get("action") if isinstance(item, Mapping) else None
        extra = _CURATED_EXTRA.get(action)
        if extra is None:
            raise ValueError(f"{location}.action is invalid")
        optional = {"rule", "exception", *_CURATED_EVIDENCE, *(("relabel",) if action == "impute_specimen" else ())}
        item = _fields(item, {"finding", "action", "study_id", "specimen_panel"} | extra, optional, location)
        if ("rule" in item) == ("exception" in item):
            raise ValueError(f"{location} needs a rule or an exception as its source, not both")
        _text(item.get("rule") or item.get("exception"), f"{location} source")
        if item["finding"] not in findings:
            raise ValueError(f"{location} names an unknown finding {item['finding']!r}")
        study = _text(item["study_id"], f"{location}.study_id")
        if action in PANEL_ACTIONS:
            panel = catalogue.specimen_panels.get(str(item["specimen_panel"]))
            if panel is None:
                raise ValueError(f"{location} names an unknown specimen panel {item['specimen_panel']!r}")
            decisions.append(Decision(item["finding"], action, study, panel.id, *_panel_fields(item, panel, location)))
            continue
        endpoint = _text(item["endpoint"], f"{location}.endpoint")
        if endpoint not in catalogue.endpoints:
            raise ValueError(f"{location} names an unknown endpoint {endpoint!r}")
        spec = catalogue.endpoints[endpoint].spec
        if item["specimen_panel"] != spec.specimen_panel:
            raise ValueError(f"{location}.specimen_panel {item['specimen_panel']!r} is not the specimen panel "
                             f"{spec.specimen_panel!r} of {endpoint}")
        method_contains: tuple[str, ...] = ()
        unit = reading = None
        if action == "drop_method_rows":
            method_contains = _texts(item["method_contains"], f"{location}.method_contains", upper=True)
        if action == "reinterpret_unit":
            unit, reading = _text(item["unit"], f"{location}.unit"), _text(item["reading"], f"{location}.reading")
            if unit not in spec.source_units:
                raise ValueError(f"{location}.unit {unit!r} is not a source unit of {endpoint}")
            if reading not in spec.source_units or reading == unit:
                raise ValueError(f"{location}.reading {reading!r} is not another source unit of {endpoint}, "
                                 "so the endpoint has no compiled rule for it")
        decisions.append(Decision(item["finding"], action, study, spec.specimen_panel, endpoint=endpoint,
                                  method_contains=method_contains, unit=unit, reading=reading))
    return _CURATED_TOPIC, reviewed, findings, decisions, md5


def merge_data_decisions(
    parsed: Sequence[tuple[str, Mapping[str, str], dict[str, Finding], list[Decision], str]],
    *, contract: str = DATA_DECISIONS_CONTRACT,
) -> DataDecisions:
    topics: list[str] = []
    reviewed: dict[str, Mapping[str, str]] = {}
    findings: dict[str, Finding] = {}
    md5: dict[str, str] = {}
    cells: dict[tuple[str, str], list[Decision]] = {}
    endpoint_cells: dict[tuple[str, str], list[Decision]] = {}
    for topic, source, topic_findings, decisions, digest in parsed:
        if topic in reviewed:
            raise ValueError(f"data decision topic {topic!r} is repeated")
        topics.append(topic)
        reviewed[topic], md5[topic] = source, digest
        for finding_id, finding in topic_findings.items():
            if finding_id in findings:
                raise ValueError(f"finding {finding_id!r} is in two decision files")
            findings[finding_id] = finding
        for decision in decisions:
            if decision.action in ENDPOINT_ACTIONS:
                endpoint_cells.setdefault((decision.study_id, decision.endpoint or ""), []).append(decision)
            else:
                cells.setdefault((decision.study_id, decision.specimen_panel or ""), []).append(decision)
    for (study, panel), decisions in cells.items():
        actions = [decision.action for decision in decisions]
        if len(actions) != len(set(actions)) or ("exclude_panel" in actions and len(actions) > 1):
            raise ValueError(f"conflicting decisions for study {study!r} and specimen panel {panel!r}")
    for (study, endpoint), decisions in endpoint_cells.items():
        keys = [(decision.action, decision.unit, decision.method_contains) for decision in decisions]
        if len(keys) != len(set(keys)):
            raise ValueError(f"conflicting decisions for study {study!r} and endpoint {endpoint!r}")
    return DataDecisions(tuple(topics), reviewed, findings, {key: tuple(value) for key, value in cells.items()}, md5,
                         {key: tuple(value) for key, value in endpoint_cells.items()}, contract)


@lru_cache(maxsize=1)
def load_data_decisions() -> DataDecisions:
    """The active data decisions: the locked ones beside the caller's catalogue, else none (bundled catalogue)."""

    location = decisions_location()
    if location is None:
        return EMPTY_DATA_DECISIONS
    return load_locked_decisions(*location, catalogue=load_endpoint_catalogue())


def decisions_location() -> tuple[Path, Path] | None:
    """The locked data decisions (the sibling of the caller's ``catalogue.json``) and the catalogue lock."""

    location = catalogue_location()
    if location is None:
        return None
    catalogue, lock = location
    return catalogue.with_name(DATA_DECISIONS_FILENAME), lock


def load_locked_decisions(path: Path, lock: Path, *, catalogue: EndpointCatalogue | None = None) -> DataDecisions:
    """Read the data decisions only when their md5 is the one of the catalogue lock; no fallback.

    The decisions are validated against ``catalogue``, which the caller has loaded from the locked
    ``catalogue.json`` beside them (``load_data_decisions`` passes the active, shared one). Without it the
    locked catalogue beside the decisions is loaded.
    """

    if not path.is_file():
        raise QueryError(f"Data decisions {path} are missing.")
    try:
        expected = yaml.safe_load(lock.read_text(encoding="utf-8"))["outputs"][DATA_DECISIONS_FILENAME]
    except (OSError, KeyError, TypeError) as error:
        raise QueryError(f"Catalogue lock {lock} is missing or names no {DATA_DECISIONS_FILENAME}.") from error
    raw = path.read_bytes()
    digest = hashlib.md5(raw).hexdigest()
    if digest != expected:
        raise QueryError(f"Data decisions {path} (md5 {digest[:12]}) differs from the lock "
                         f"{lock} ({str(expected)[:12]}).")
    document = yaml.load(raw, Loader=YAML_LOADER)
    if not isinstance(document, Mapping) or document.get("contract") != DATA_DECISIONS_CONTRACT:
        raise QueryError(f"Data decisions {path} must use {DATA_DECISIONS_CONTRACT}.")
    catalogue = catalogue or load_locked_catalogue(path.with_name("catalogue.json"), lock)
    return data_decisions_from_mapping(document, md5=digest, catalogue=catalogue)


__all__ = [
    "DATA_DECISIONS_FILENAME",
    "DATA_DECISIONS_CONTRACT",
    "DataDecisions",
    "Decision",
    "EMPTY_DATA_DECISIONS",
    "ENDPOINT_ACTIONS",
    "Finding",
    "PANEL_ACTIONS",
    "SpecimenResolution",
    "data_decisions_from_mapping",
    "decisions_location",
    "load_data_decisions",
    "load_locked_decisions",
    "merge_data_decisions",
]
