"""Live Grit42 subject-membership parity harness.

The Grit42 API can retrieve saved subject queries and filter one SEND domain,
but it cannot create arbitrary saved subject queries. Query authoring therefore
remains a UI step. This module keeps the API reads, local execution, and exact
membership comparison reproducible after those queries have stable IDs.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import duckdb
import pandas as pd
import yaml

from .contract import Query, QueryError, parse_query, seq
from .duckdb import create_derived_views
from .fingerprint import fingerprint_predicate
from .grit42 import compare_membership, import_grit42_subject_query
from .identities import (
    GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
    GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
)
from .result import explain_query

BANK_CONTRACT = "vcgen.grit42_membership_bank.v1"
BINDINGS_CONTRACT = "vcgen.grit42_membership_bindings.v1"
DEFAULT_BASE_URL = "https://vict3r.upf.edu"
CASE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class BankCase:
    case_id: str
    title: str
    intent: str
    scout_expected_count: int
    conditions: tuple[Mapping[str, Any], ...] = ()
    diagnostic_alternative_count: int | None = None


@dataclass(frozen=True)
class QueryBank:
    name: str
    seed_study_id: str
    source_domains: tuple[str, ...]
    scout_source_identity: str
    base_rules: tuple[Mapping[str, Any], ...]
    cases: tuple[BankCase, ...]

    def query_for(self, case: BankCase) -> Query:
        children = [dict(rule) for rule in self.base_rules]
        children.extend(dict(condition) for condition in case.conditions)
        return parse_query(
            {
                "contract": "vcgen.cohort_query.v4",
                "query_id": f"grit42-parity:{self.name}:{case.case_id}",
                "predicate": {"group": "and", "children": children},
                "import_provenance": {
                    "contract": GRIT42_SUBJECT_QUERY_IMPORT_CONTRACT,
                    "source_contract": GRIT42_ADVANCED_SUBJECT_QUERY_CONTRACT,
                    "source_payload_sha256": "0" * 64,
                },
            }
        )


class Grit42Client:
    """Minimal read-only client for the API operations used by parity."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        page_size: int = 1_000,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.page_size = page_size

    def saved_query(self, query_id: int) -> dict[str, Any]:
        payload = self._request_json(f"/api/v1/subject_queries/{_query_id(query_id)}")
        return dict(_mapping(payload, "saved query response"))

    def study_slice(self, study_id: str, domains: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        filters = [{"property": "studyid", "operator": "eq", "value": study_id}]
        return {domain.upper(): self.domain_records(domain, filters=filters) for domain in domains}

    def domain_records(
        self,
        domain: str,
        *,
        filters: Sequence[Mapping[str, Any]],
        subject_query_id: int | None = None,
    ) -> list[dict[str, Any]]:
        domain_name = _domain(domain)
        if subject_query_id is None:
            path = f"/api/v1/send_domain_{domain_name.lower()}/query"
        else:
            path = (
                f"/api/v1/send_domain_{domain_name.lower()}"
                f"/subject_query/{_query_id(subject_query_id)}/query"
            )
        records: list[dict[str, Any]] = []
        page = 0
        total: int | None = None
        while total is None or len(records) < total:
            response = self._request_json(
                path,
                method="POST",
                body={
                    "filters": [dict(item) for item in filters],
                    "sort": [],
                    "limit": self.page_size,
                    "page": page,
                },
            )
            payload = _mapping(response, f"{domain_name} response page {page}")
            data = payload.get("data")
            count = payload.get("count")
            if not seq(data) or any(not isinstance(item, Mapping) for item in data):
                raise RuntimeError(f"{domain_name} response data must be an array of records")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError(f"{domain_name} response count must be a non-negative integer")
            if total is None:
                total = count
            elif count != total:
                raise RuntimeError(f"{domain_name} response count changed during pagination")
            page_records = [dict(item) for item in data]
            records.extend(page_records)
            if not page_records and len(records) < total:
                raise RuntimeError(f"{domain_name} pagination stopped before count {total}")
            page += 1
        if len(records) != total:
            raise RuntimeError(
                f"{domain_name} returned {len(records)} records but declared count {total}"
            )
        return records

    def subject_membership(self, query_id: int) -> tuple[tuple[str, str], ...]:
        records = self.domain_records("DM", filters=(), subject_query_id=query_id)
        return _membership_keys(records, "Grit42 DM subject-query response")

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "X-API-KEY": self._api_key}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as error:
            raise RuntimeError(
                f"Grit42 API returned HTTP {error.code} for {method} {path}"
            ) from error
        except URLError as error:
            raise RuntimeError(
                f"Grit42 API request failed for {method} {path}: {error.reason}"
            ) from error
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Grit42 API returned invalid JSON for {method} {path}") from error


def default_bank_path() -> Path:
    return Path(str(files("vcgen_query").joinpath("data/grit42_membership_bank.v1.yaml")))


def load_bank(path: str | Path | None = None) -> QueryBank:
    source = default_bank_path() if path is None else Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload = _mapping(raw, "query bank")
    _exact_keys(
        payload,
        {
            "contract",
            "name",
            "seed_study_id",
            "source_domains",
            "scout_source_identity",
            "base_rules",
            "cases",
        },
        "query bank",
    )
    if payload["contract"] != BANK_CONTRACT:
        raise QueryError(f"query bank contract must be {BANK_CONTRACT!r}")
    name = _nonempty_text(payload["name"], "query bank name")
    seed_study_id = _nonempty_text(payload["seed_study_id"], "seed_study_id")
    domains_raw = _items(payload["source_domains"], "source_domains")
    domains = tuple(_domain(item) for item in domains_raw)
    if len(domains) != len(set(domains)) or "DM" not in domains:
        raise QueryError("source_domains must be unique and include DM")
    base_rules_raw = _items(payload["base_rules"], "base_rules")
    base_rules = tuple(dict(_mapping(item, "base rule")) for item in base_rules_raw)
    if not base_rules:
        raise QueryError("base_rules must be non-empty")
    cases_raw = _items(payload["cases"], "cases")
    cases: list[BankCase] = []
    seen: set[str] = set()
    for index, item in enumerate(cases_raw):
        case_raw = _mapping(item, f"cases[{index}]")
        allowed = {
            "id",
            "title",
            "intent",
            "scout_expected_count",
            "diagnostic_alternative_count",
            "condition",
            "conditions",
        }
        if unsupported := sorted(set(case_raw) - allowed):
            raise QueryError(f"cases[{index}] has unsupported key(s): {unsupported}")
        case_id = _nonempty_text(case_raw.get("id"), f"cases[{index}].id")
        if not CASE_ID.fullmatch(case_id) or case_id in seen:
            raise QueryError(f"cases[{index}].id must be unique lower snake case")
        seen.add(case_id)
        expected = _count(case_raw.get("scout_expected_count"), "scout_expected_count")
        alternative_raw = case_raw.get("diagnostic_alternative_count")
        alternative = (
            None
            if alternative_raw is None
            else _count(alternative_raw, "diagnostic_alternative_count")
        )
        condition_raw = case_raw.get("condition")
        conditions_raw = case_raw.get("conditions")
        if condition_raw is not None and conditions_raw is not None:
            raise QueryError(f"cases[{index}] cannot contain condition and conditions")
        if conditions_raw is not None:
            conditions = tuple(
                dict(_mapping(item, f"cases[{index}].conditions[{item_index}]"))
                for item_index, item in enumerate(
                    _items(conditions_raw, f"cases[{index}].conditions")
                )
            )
            if not conditions:
                raise QueryError(f"cases[{index}].conditions must be non-empty")
        elif condition_raw is not None:
            conditions = (dict(_mapping(condition_raw, f"cases[{index}].condition")),)
        else:
            conditions = ()
        cases.append(
            BankCase(
                case_id,
                _nonempty_text(case_raw.get("title"), f"cases[{index}].title"),
                _nonempty_text(case_raw.get("intent"), f"cases[{index}].intent"),
                expected,
                conditions,
                alternative,
            )
        )
    if not cases:
        raise QueryError("cases must be non-empty")
    bank = QueryBank(
        name,
        seed_study_id,
        domains,
        _nonempty_text(payload["scout_source_identity"], "scout_source_identity"),
        base_rules,
        tuple(cases),
    )
    for case in bank.cases:
        bank.query_for(case)
    return bank


def load_bindings(path: str | Path, bank: QueryBank) -> dict[str, int]:
    payload = _mapping(yaml.safe_load(Path(path).read_text(encoding="utf-8")), "bindings")
    _exact_keys(payload, {"contract", "bank", "queries"}, "bindings")
    if payload["contract"] != BINDINGS_CONTRACT:
        raise QueryError(f"bindings contract must be {BINDINGS_CONTRACT!r}")
    if payload["bank"] != bank.name:
        raise QueryError(f"bindings bank must be {bank.name!r}")
    queries = _mapping(payload["queries"], "bindings.queries")
    expected = {case.case_id for case in bank.cases}
    if set(queries) != expected:
        raise QueryError("bindings.queries must contain every bank case exactly once")
    result = {case_id: _query_id(value) for case_id, value in queries.items()}
    if len(set(result.values())) != len(result):
        raise QueryError("bindings query IDs must be unique")
    return result


def write_binding_template(path: str | Path, bank: QueryBank) -> Path:
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing bindings: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract": BINDINGS_CONTRACT,
        "bank": bank.name,
        "queries": {case.case_id: None for case in bank.cases},
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def load_api_key(path: str | Path | None = None) -> str:
    environment = os.environ.get("GRIT42_API_KEY", "").strip()
    if environment:
        return environment
    source = Path(".grit42-api-token") if path is None else Path(path)
    if not source.is_file():
        raise FileNotFoundError(
            "Grit42 API key is unavailable; set GRIT42_API_KEY or provide --api-key-file"
        )
    mode = stat.S_IMODE(source.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"API key file must not grant group or other permissions: {source}")
    value = source.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"API key file is empty: {source}")
    return value


def live_connection(
    records_by_domain: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Any, str, dict[str, list[dict[str, Any]]]]:
    normalized = {
        _domain(domain): [_uppercase_record(record, domain) for record in records]
        for domain, records in records_by_domain.items()
    }
    empty_domains = sorted(domain for domain, records in normalized.items() if not records)
    if empty_domains:
        raise QueryError(f"live study slice has no records for domain(s): {empty_domains}")
    if "DM" not in normalized:
        raise QueryError("live study slice must contain DM")
    _membership_keys(normalized["DM"], "live study DM records")
    connection = duckdb.connect()
    for domain, records in normalized.items():
        frame = pd.DataFrame.from_records(records)
        connection.register(domain.lower(), frame)
    create_derived_views(connection)
    return connection, _source_identity(normalized), normalized


def probe_bank(
    bank: QueryBank,
    client: Grit42Client,
    output_dir: str | Path,
) -> dict[str, Any]:
    output = Path(output_dir)
    records = client.study_slice(bank.seed_study_id, bank.source_domains)
    connection, source_identity, normalized = live_connection(records)
    try:
        cases = []
        for case in bank.cases:
            explanation = explain_query(
                connection,
                bank.query_for(case),
                include_subject_keys=False,
                source_data_identity=source_identity,
            )
            cases.append(
                {
                    "case_id": case.case_id,
                    "subject_count": explanation["subject_count"],
                    "scout_expected_count": case.scout_expected_count,
                    "diagnostic_alternative_count": case.diagnostic_alternative_count,
                    "matches_scout_count": explanation["subject_count"]
                    == case.scout_expected_count,
                }
            )
    finally:
        connection.close()
    _write_source(output, normalized)
    summary = {
        "contract": "vcgen.grit42_membership_probe.v1",
        "bank": bank.name,
        "seed_study_id": bank.seed_study_id,
        "source_identity": source_identity,
        "cases": cases,
    }
    _write_json(output / "probe-summary.json", summary)
    return summary


def run_parity(
    bank: QueryBank,
    bindings: Mapping[str, int],
    client: Grit42Client,
    output_dir: str | Path,
) -> dict[str, Any]:
    output = Path(output_dir)
    records = client.study_slice(bank.seed_study_id, bank.source_domains)
    connection, source_identity, normalized = live_connection(records)
    _write_source(output, normalized)
    summaries = []
    try:
        for case in bank.cases:
            query_id = _query_id(bindings[case.case_id])
            saved = client.saved_query(query_id)
            imported = import_grit42_subject_query(saved)
            expected = bank.query_for(case)
            expected_fingerprint = fingerprint_predicate(expected.predicate)
            imported_fingerprint = fingerprint_predicate(imported.predicate)
            reference = client.subject_membership(query_id)
            explanation = explain_query(
                connection,
                imported,
                include_subject_keys=True,
                source_data_identity=source_identity,
            )
            local = tuple(
                (item["study_id"], item["subject_id"])
                for item in explanation["nodes"][-1]["subject_keys"]
            )
            comparison = compare_membership(local, reference)
            predicate_matches = imported_fingerprint == expected_fingerprint
            status = (
                "match"
                if predicate_matches and comparison["matches"]
                else "predicate_mismatch"
                if not predicate_matches
                else "membership_gap"
            )
            case_dir = output / "cases" / case.case_id
            _write_json(case_dir / "saved-query.json", saved)
            _write_json(
                case_dir / "reference-membership.json",
                [
                    {"study_id": study_id, "subject_id": subject_id}
                    for study_id, subject_id in reference
                ],
            )
            _write_json(
                case_dir / "details.json",
                {
                    "query_id": query_id,
                    "expected_predicate_fingerprint": expected_fingerprint,
                    "imported_predicate_fingerprint": imported_fingerprint,
                    "predicate_matches_bank": predicate_matches,
                    "comparison": _jsonable(comparison),
                    "local_explanation": explanation,
                },
            )
            summaries.append(
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "intent": case.intent,
                    "query_id": query_id,
                    "status": status,
                    "scout_expected_count": case.scout_expected_count,
                    "diagnostic_alternative_count": case.diagnostic_alternative_count,
                    "predicate_matches_bank": predicate_matches,
                    "local_count": comparison["local_count"],
                    "reference_count": comparison["reference_count"],
                    "local_hash": comparison["local_hash"],
                    "reference_hash": comparison["reference_hash"],
                    "missing_count": len(comparison["missing_keys"]),
                    "extra_count": len(comparison["extra_keys"]),
                }
            )
    finally:
        connection.close()
    summary = {
        "contract": "vcgen.grit42_membership_parity.v1",
        "bank": bank.name,
        "seed_study_id": bank.seed_study_id,
        "source_identity": source_identity,
        "case_count": len(summaries),
        "match_count": sum(item["status"] == "match" for item in summaries),
        "cases": summaries,
    }
    _write_json(output / "summary.json", summary)
    return summary


def _write_source(output: Path, records: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    for domain, rows in records.items():
        _write_json(output / "source" / f"{domain}.json", list(rows))




def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _source_identity(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    normalized = {
        domain: sorted(
            (_jsonable(dict(row)) for row in rows),
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
        for domain, rows in sorted(records.items())
    }
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"grit42-study-slice-sha256:{digest}"


def _uppercase_record(raw: Mapping[str, Any], domain: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        normalized = str(key).upper()
        if normalized in result:
            raise QueryError(f"{domain} API record contains duplicate case-insensitive keys")
        result[normalized] = value
    if "STUDYID" not in result:
        raise QueryError(f"{domain} API record is missing STUDYID")
    return result


def _membership_keys(
    records: Sequence[Mapping[str, Any]], location: str
) -> tuple[tuple[str, str], ...]:
    keys = set()
    for index, raw in enumerate(records):
        row = {str(key).upper(): value for key, value in raw.items()}
        study = row.get("STUDYID")
        subject = row.get("USUBJID")
        if study is None or subject is None or not str(study).strip() or not str(subject).strip():
            raise QueryError(f"{location}[{index}] must contain non-empty STUDYID and USUBJID")
        keys.add((str(study), str(subject)))
    return tuple(sorted(keys))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        return value.item()
    return value


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QueryError(f"{location} must be an object")
    return value


def _items(value: Any, location: str) -> Sequence[Any]:
    if not seq(value):
        raise QueryError(f"{location} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise QueryError(f"{location} must contain exactly {sorted(expected)}")


def _nonempty_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueryError(f"{location} must be a non-empty string")
    return value.strip()


def _count(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryError(f"{location} must be a non-negative integer")
    return value


def _query_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QueryError("Grit42 query ID must be a positive integer")
    return value


def _domain(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", value):
        raise QueryError("SEND domain must be an identifier")
    return value.upper()


def _timestamped_output() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("tmp/grit42-parity") / timestamp


def _client(args: argparse.Namespace) -> Grit42Client:
    return Grit42Client(
        load_api_key(args.api_key_file),
        base_url=args.base_url,
        timeout=args.timeout,
        page_size=args.page_size,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, default=default_bank_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-bank")
    validate.set_defaults(action="validate")

    bindings = subparsers.add_parser("init-bindings")
    bindings.add_argument("--output", type=Path, required=True)
    bindings.set_defaults(action="bindings")

    for name in ("probe", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--base-url", default=DEFAULT_BASE_URL)
        command.add_argument("--api-key-file", type=Path)
        command.add_argument("--timeout", type=float, default=30.0)
        command.add_argument("--page-size", type=int, default=1_000)
        command.add_argument("--output-dir", type=Path, default=None)
        if name == "run":
            command.add_argument("--bindings", type=Path, required=True)
        command.set_defaults(action=name)

    args = parser.parse_args(argv)
    bank = load_bank(args.bank)
    if args.action == "validate":
        print(f"validated {len(bank.cases)} cases in {bank.name}")
        return 0
    if args.action == "bindings":
        print(write_binding_template(args.output, bank))
        return 0
    output = _timestamped_output() if args.output_dir is None else args.output_dir
    if args.action == "probe":
        summary = probe_bank(bank, _client(args), output)
        print(f"probed {len(summary['cases'])} cases at {output}")
        return 0
    bound = load_bindings(args.bindings, bank)
    summary = run_parity(bank, bound, _client(args), output)
    print(f"matched {summary['match_count']}/{summary['case_count']} cases at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
