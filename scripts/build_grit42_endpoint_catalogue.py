"""Build the versioned grit42 endpoint catalogue from captured UI metadata.

The Columns-page capture is the source for domain-column membership and UI
types. The repository SEND lookup supplies the durable descriptions that the
live page exposes only through per-column hover tooltips.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = REPOSITORY_ROOT / "packages" / "vcgen-query" / "src" / "vcgen_query" / "data"
CAPTURE = DATA_ROOT / "grit42_columns.2026-08-22.v1.json"
DESCRIPTIONS = REPOSITORY_ROOT / "docs" / "send_column_lookup.csv"
OUTPUT = DATA_ROOT / "grit42_endpoint_catalogue.2026-08-22.v1.json"

UI_TO_VALUE_TYPE = {
    "string": "string",
    "entity": "string",
    "numeric": "number",
    "timestamp": "datetime",
}

WIDER_QUERY_CANDIDATES = [
    {
        "domain": "DM",
        "endpoint": "SEX",
        "purpose": "Split a narrow study cohort by sex.",
    },
    {
        "domain": "EX",
        "endpoint": "EXDOSE",
        "purpose": "Anchor treatment and control cohorts by administered dose.",
    },
    {
        "domain": "TS",
        "endpoint": "TSPARMCD",
        "purpose": "Select a trial-summary parameter.",
    },
    {
        "domain": "TS",
        "endpoint": "TSVAL",
        "purpose": "Constrain the value for a selected trial-summary parameter.",
    },
    {
        "domain": "LB",
        "endpoint": "LBTESTCD",
        "purpose": "Select a laboratory analyte with a compact controlled code.",
    },
    {
        "domain": "LB",
        "endpoint": "LBSTRESN",
        "purpose": "Test numeric laboratory-result thresholds.",
    },
    {
        "domain": "LB",
        "endpoint": "LBMETHOD",
        "purpose": "Test empty and non-empty laboratory method values.",
    },
]

ADVANCED_QUERY_DATETIME_SOURCES = {
    ("DM", "RFSTDTC"): {
        "source": "calendar_date_midnight",
        "observed_on": "2026-08-22",
        "saved_query_ids": [10326552, 10326554, 10326556, 10326558, 10326560, 10326564],
        "note": (
            "Saved-query comparisons use midnight on the RFSTDTC calendar date, "
            "while the domain API and source export expose an offset-aware time."
        ),
    }
}


def main() -> None:
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    if capture.get("contract") != "grit42.columns_page_capture.v1":
        raise RuntimeError("unsupported grit42 Columns-page capture contract")

    labels = capture.get("domain_labels")
    rows = capture.get("rows")
    if not isinstance(labels, dict) or not isinstance(rows, list):
        raise RuntimeError("grit42 Columns-page capture is malformed")

    with DESCRIPTIONS.open(newline="", encoding="utf-8-sig") as stream:
        description_rows = list(csv.DictReader(stream))
    endpoints = {
        row["column_code"]: {
            "description": row["description"],
            "term_label": row["term_label"],
            "status": row["status"],
            "provenance_url": row["provenance_url"] or None,
            "provenance_note": row["provenance_note"],
        }
        for row in description_rows
    }

    columns_by_domain: dict[str, list[dict[str, str]]] = {domain: [] for domain in labels}
    pair_types: dict[tuple[str, str], dict[str, str]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise RuntimeError(f"capture row {index} must be an object")
        domain = raw.get("domain")
        endpoint = raw.get("endpoint")
        ui_type = raw.get("ui_type")
        if domain not in labels or endpoint not in endpoints or ui_type not in UI_TO_VALUE_TYPE:
            raise RuntimeError(f"capture row {index} is unsupported: {raw!r}")
        pair = (domain, endpoint)
        if pair in pair_types:
            raise RuntimeError(f"duplicate domain-endpoint pair in capture: {pair!r}")
        column = {
            "code": endpoint,
            "ui_type": ui_type,
            "value_type": UI_TO_VALUE_TYPE[ui_type],
        }
        datetime_source = ADVANCED_QUERY_DATETIME_SOURCES.get(pair)
        if datetime_source is not None:
            if column["value_type"] != "datetime":
                raise RuntimeError(f"datetime source override is not a timestamp: {pair!r}")
            column["advanced_query_datetime_source"] = datetime_source["source"]
        pair_types[pair] = column
        columns_by_domain[domain].append(column)

    domains = [
        {
            "code": domain,
            "description": description,
            "columns": sorted(columns_by_domain[domain], key=lambda item: item["code"]),
        }
        for domain, description in labels.items()
    ]
    unique_codes = {endpoint for _, endpoint in pair_types}
    expected_counts = capture.get("counts")
    actual_counts = {
        "domains": len(domains),
        "domain_endpoint_occurrences": len(pair_types),
        "unique_endpoint_codes": len(unique_codes),
    }
    if expected_counts != actual_counts:
        raise RuntimeError(
            f"catalogue count mismatch: expected={expected_counts}, actual={actual_counts}"
        )

    candidates = []
    for candidate in WIDER_QUERY_CANDIDATES:
        pair = (candidate["domain"], candidate["endpoint"])
        column = pair_types.get(pair)
        if column is None:
            raise RuntimeError(f"wider query candidate is absent from capture: {pair!r}")
        candidates.append({**candidate, **column})

    payload = {
        "contract": "vcgen.grit42_endpoint_catalogue.v1",
        "catalogue_id": "grit42-columns-2026-08-22-v1",
        "captured_on": capture["captured_on"],
        "source": {
            "columns_page": capture["source_url"],
            "columns_page_capture": str(CAPTURE.relative_to(REPOSITORY_ROOT)),
            "description_lookup": str(DESCRIPTIONS.relative_to(REPOSITORY_ROOT)),
            "description_note": (
                "Descriptions come from the repository SEND lookup, not from a "
                "bulk export of grit42 hover tooltips."
            ),
        },
        "counts": actual_counts,
        "wider_query_candidates": candidates,
        "observed_advanced_query_semantics": [
            {"domain": domain, "endpoint": endpoint, **details}
            for (domain, endpoint), details in sorted(ADVANCED_QUERY_DATETIME_SOURCES.items())
        ],
        "domains": domains,
        "endpoints": {code: endpoints[code] for code in sorted(unique_codes)},
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
