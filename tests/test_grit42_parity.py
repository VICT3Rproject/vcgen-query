"""Tests for the reusable live Grit42 membership-parity harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vcgen_query.grit42_parity
from vcgen_query import (
    explain_query,
    fingerprint_predicate,
    grit42_catalogue_identity,
    grit42_column,
)
from vcgen_query.grit42_parity import (
    Grit42Client,
    QueryError,
    _source_identity,
    live_connection,
    load_api_key,
    load_bank,
    load_bindings,
    write_binding_template,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PACKAGE_ROOT / "src" / "vcgen_query" / "data"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_shipped_bank_is_strict_and_has_distinct_predicates() -> None:
    bank = load_bank()

    assert bank.seed_study_id == "481152"
    assert bank.source_domains == ("DM", "EX", "TS", "LB")
    assert len(bank.cases) == 10
    fingerprints = {fingerprint_predicate(bank.query_for(case).predicate) for case in bank.cases}
    assert len(fingerprints) == len(bank.cases)
    assert {case.scout_expected_count for case in bank.cases} == {0, 10}


def test_expanded_bank_covers_query_semantics() -> None:
    bank = load_bank(DATA_ROOT / "grit42_membership_bank.expanded.v1.yaml")

    def nodes(node):
        yield node
        for child in node.get("children", ()):
            yield from nodes(child)

    assert bank.name == "grit42-expanded-query-semantics"
    assert bank.source_domains == ("DM", "EX", "TS", "LB")
    assert len(bank.cases) == 37
    assert len(
        {fingerprint_predicate(bank.query_for(case).predicate) for case in bank.cases}
    ) == len(bank.cases)
    assert {
        case.case_id: case.diagnostic_alternative_count
        for case in bank.cases
        if case.diagnostic_alternative_count is not None
    } == {"alt_and_ast": 2, "ast_and_any_numeric": 5}

    canonical = {
        case.case_id: bank.query_for(case).to_canonical_dict()["predicate"] for case in bank.cases
    }
    assert any(
        node.get("field", {}).get("value_type") == "datetime"
        for node in nodes(canonical["study_reference_timestamp"])
    )
    assert any(node.get("negated") is True for node in nodes(canonical["not_ast_above_threshold"]))
    operators = {
        node["operator"]
        for case in bank.cases
        for node in nodes(bank.query_for(case).to_canonical_dict()["predicate"])
        if "operator" in node
    }
    assert {"contains", "not_contains", "starts_with", "ends_with"} <= operators
    assert {"is_empty", "is_not_empty", "eq", "gt", "gte", "lt", "lte"} <= operators


def test_study_domain_bank_covers_three_domains_without_subject_keys() -> None:
    bank = load_bank(DATA_ROOT / "grit42_membership_bank.study_domains.v1.yaml")

    assert bank.name == "grit42-study-domain-propagation"
    assert bank.source_domains == ("DM", "EX", "TS", "TA", "TE", "TX")
    assert len(bank.cases) == 5
    assert {case.scout_expected_count for case in bank.cases} == {20}
    assert len(
        {fingerprint_predicate(bank.query_for(case).predicate) for case in bank.cases}
    ) == len(bank.cases)


def test_endpoint_catalogue_matches_observed_columns_page_inventory() -> None:
    payload = json.loads(
        (DATA_ROOT / "grit42_endpoint_catalogue.2026-08-22.v1.json").read_text(encoding="utf-8")
    )

    assert payload["contract"] == "vcgen.grit42_endpoint_catalogue.v1"
    assert payload["catalogue_id"] == "grit42-columns-2026-08-22-v1"
    assert payload["captured_on"] == "2026-08-22"
    assert payload["counts"] == {
        "domains": 36,
        "domain_endpoint_occurrences": 830,
        "unique_endpoint_codes": 602,
    }
    assert len(payload["domains"]) == 36
    assert sum(len(item["columns"]) for item in payload["domains"]) == 830
    assert len(payload["endpoints"]) == 602
    by_domain = {
        item["code"]: {column["code"]: column for column in item["columns"]}
        for item in payload["domains"]
    }
    assert by_domain["EG"]["FACTOR_CURATED"]["value_type"] == "string"
    assert by_domain["LB"]["LBSTRESN"] == {
        "code": "LBSTRESN",
        "ui_type": "numeric",
        "value_type": "number",
    }
    assert by_domain["DM"]["RFSTDTC"]["advanced_query_datetime_source"] == (
        "calendar_date_midnight"
    )
    assert payload["observed_advanced_query_semantics"][0]["saved_query_ids"] == [
        10326552,
        10326554,
        10326556,
        10326558,
        10326560,
        10326564,
    ]
    assert {(item["domain"], item["endpoint"]) for item in payload["wider_query_candidates"]} == {
        ("DM", "SEX"),
        ("EX", "EXDOSE"),
        ("TS", "TSPARMCD"),
        ("TS", "TSVAL"),
        ("LB", "LBTESTCD"),
        ("LB", "LBSTRESN"),
        ("LB", "LBMETHOD"),
    }
    assert grit42_catalogue_identity() == {
        "catalogue_id": "grit42-columns-2026-08-22-v1",
        "captured_on": "2026-08-22",
        "resource": "grit42_endpoint_catalogue.2026-08-22.v1.json",
    }
    assert grit42_column("DM", "SEX").ui_type == "entity"
    assert grit42_column("DM", "RFSTDTC").advanced_query_datetime_source == "calendar_date_midnight"
    assert grit42_column("DM", "NOT_A_COLUMN") is None


def test_columns_page_capture_is_complete_and_typed() -> None:
    payload = json.loads(
        (DATA_ROOT / "grit42_columns.2026-08-22.v1.json").read_text(encoding="utf-8")
    )

    assert payload["contract"] == "grit42.columns_page_capture.v1"
    assert payload["counts"] == {
        "domains": 36,
        "domain_endpoint_occurrences": 830,
        "unique_endpoint_codes": 602,
    }
    assert len(payload["domain_labels"]) == 36
    assert len(payload["rows"]) == 830
    assert {item["ui_type"] for item in payload["rows"]} == {
        "string",
        "numeric",
        "timestamp",
        "entity",
    }


def test_bindings_template_requires_complete_unique_positive_ids(tmp_path: Path) -> None:
    bank = load_bank()
    path = write_binding_template(tmp_path / "bindings.yaml", bank)
    payload = path.read_text(encoding="utf-8")
    assert "GRIT42" not in payload

    complete = {
        "contract": "vcgen.grit42_membership_bindings.v1",
        "bank": bank.name,
        "queries": {case.case_id: index + 1 for index, case in enumerate(bank.cases)},
    }
    path.write_text(json.dumps(complete), encoding="utf-8")
    assert load_bindings(path, bank)["baseline"] == 1

    complete["queries"]["treatment_only"] = 1
    path.write_text(json.dumps(complete), encoding="utf-8")
    with pytest.raises(QueryError, match="must be unique"):
        load_bindings(path, bank)


def test_api_client_paginates_and_sends_key_without_logging_it(monkeypatch) -> None:
    calls = []
    responses = iter(
        [
            {"data": [{"studyid": "S", "usubjid": "1"}], "count": 2},
            {"data": [{"studyid": "S", "usubjid": "2"}], "count": 2},
        ]
    )

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _Response(next(responses))

    monkeypatch.setattr(vcgen_query.grit42_parity, "urlopen", opener)
    client = Grit42Client("secret-token", page_size=1)
    records = client.domain_records("DM", filters=[])

    assert [item["usubjid"] for item in records] == ["1", "2"]
    assert len(calls) == 2
    assert all(request.get_header("X-api-key") == "secret-token" for request, _ in calls)
    assert [json.loads(request.data)["page"] for request, _ in calls] == [0, 1]
    assert "secret-token" not in repr(client)


def test_api_key_file_must_be_private(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GRIT42_API_KEY", raising=False)
    path = tmp_path / "token"
    path.write_text("token-value\n", encoding="utf-8")
    path.chmod(0o600)
    assert load_api_key(path) == "token-value"

    path.chmod(0o644)
    with pytest.raises(PermissionError, match="group or other"):
        load_api_key(path)


def test_live_slice_executes_supported_ui_field_semantics() -> None:
    bank = load_bank()
    dm = [
        {
            "studyid": "481152",
            "usubjid": subject,
            "species": "RAT",
            "strain": "WISTAR HAN",
            "sex": sex,
            "age": "8",
            "ageu": "WEEKS",
        }
        for subject, sex in (("1", "M"), ("2", "F"), ("3", "F"))
    ]
    ex = [
        {
            "studyid": "481152",
            "usubjid": subject,
            "exdose": "0",
            "exstdy": "1",
            "exroute": "Oral",
            "exdosfrq": "QD",
            "extrt": "VEHICLE",
            "extrtv": "0.5% w/v Methylcellulose 15cP in Milli-Q water",
        }
        for subject in ("1", "2", "3")
    ]
    ts = [{"studyid": "481152", "tsparmcd": "SSTYP", "tsval": "REPEAT DOSE TOXICITY"}]
    lb = [{"studyid": "481152", "usubjid": "1", "lbtestcd": "ALT", "lbstresn": "1"}]
    connection, identity, _ = live_connection({"DM": dm, "EX": ex, "TS": ts, "LB": lb})
    try:
        counts = {
            case.case_id: explain_query(
                connection,
                bank.query_for(case),
                source_data_identity=identity,
            )["subject_count"]
            for case in bank.cases
        }
    finally:
        connection.close()

    assert counts == {
        "baseline": 3,
        "treatment_only": 3,
        "treatment_rat": 3,
        "treatment_mouse": 0,
        "treatment_wistar": 3,
        "treatment_oral": 3,
        "treatment_qd": 3,
        "treatment_rat_or_mouse": 3,
        "treatment_rat_and_mouse": 0,
        "treatment_not_mouse": 3,
    }


def test_source_hash_is_order_invariant() -> None:
    first = {"DM": [{"STUDYID": "S", "USUBJID": "2"}, {"STUDYID": "S", "USUBJID": "1"}]}
    second = {"DM": list(reversed(first["DM"]))}
    assert _source_identity(first) == _source_identity(second)
