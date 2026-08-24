# VCGEN-QUERY

`vcgen-query` selects factual historical subjects from SEND-like data and returns their measurements as dataframes.
It is compatible with the two grit42 query formats and extends their functionality to deduplicate rows and perform unit conversion

**Status:** public alpha (`0.3.0a0`).

## Install

```bash
pip install vcgen-query
# or
uv add vcgen-query
```

## Core API

```python
vq.parse_query(mapping) -> Query
vq.load_query(path) -> Query
vq.fingerprint_predicate(query.predicate) -> str
vq.execute_query(source, query) -> QueryResult
vq.to_daily_frame(result, *, on_conflict="raise") -> DailyFrame
vq.import_grit42_subject_query(saved_query) -> Query
vq.import_grit42_form_subject_query(saved_form_query) -> Query
vq.import_grit42_advanced_subject_query(saved_advanced_query) -> Query
vq.explain_query(source, query) -> dict[str, Any]
```

`DailyFrame` contains `values`, `states`, `cell_audit`, `receipt`, and
`warnings`, as described in [Returned data structures](#returned-data-structures).
The wide frames use ordinary columns and do not set an implicit pandas index.


## Getting started

A basic query flow:

```python
import pandas as pd
import vcgen_query as vq

query = vq.load_query("rat_28d.yaml")
result: vq.QueryResult = vq.execute_query("/data/rodent_db_export.zip", query)

subjects: pd.DataFrame = result.subjects          # one row per subject key
observations: pd.DataFrame = result.observations  # lossless factual measurements
receipt: vq.QueryReceipt = result.receipt         # source identity, hashes, assumptions
```

To get a dataframe indexed by time, transform the result object like so:

```python
daily: vq.DailyFrame = vq.to_daily_frame(result, on_conflict="raise")
values: pd.DataFrame = daily.values          # selected numeric endpoint values
states: pd.DataFrame = daily.states          # status labels for matching source records
audit: pd.DataFrame = daily.cell_audit       # source values, units, and resolution
receipt: vq.DailyReceipt = daily.receipt     # query/source identity, counts, and policy
```

Queries matching the grit42 server format are accepted:
- `grit42.form_subject_query.v1` basic grit42 query
- `grit42.advanced_subject_query.v1` advanced grit42 query with arbitrary nested predicate terms

Both of these are parsed into our own `vcgen.cohort_query.v4`.
The most important component is `vcgen.subject_predicate.v1` which matches the predicate logic of the grit42 advanced query.
`grit42.subject_query_import.v1` records the query parsing provenance.

The query field `predicate` filters subjects by columns which are usually, but not always, SEND endpoints.
The field `observation_contract` requests specific endpoints for extraction after filtering. It also requests units to report in.

```yaml
# rat_28d.yaml
contract: vcgen.cohort_query.v4
query_id: rat_28d
predicate:
  group: and
  children:
    - field: {kind: derived, name: dose_duration_days, value_type: number}
      operator: between
      value: [28, 28]
    - field: {kind: send_column, domain: DM, column: SPECIES, value_type: string}
      operator: eq
      value: RAT
    - field: {kind: send_column, domain: DM, column: SEX, value_type: string}
      operator: in
      value: [F, M]
observation_contract:
  endpoints:
    - name: BW
      domain: BW
      test_codes: [BW]
    - name: LB_HCT
      domain: LB
      test_codes: [HCT]
      specimens: [BLOOD]
      source_units: [g/dL, g/L]
      target_unit: g/dL
      day_window: {min: 1, max: 28}
```

Endpoint names are uppercase machine identifiers. Columns are verified to be members of the grit42 endpoint catalogue.
Endpoints not in the VICT3R database will be rejected.
Day-window bounds are inclusive integers; a configured window excludes records that have no parsed study day.

`source_units` contains exact source labels. Parsing trims surrounding whitespace only.
It does not change case, Unicode symbols, or internal whitespace. `target_unit` is an
optional, case-sensitive UCUM code from the package catalogue. When it is present,
`source_units` must be non-empty. The parser compiles all compatible affine rules.
Unknown, incompatible, arbitrary, nonlinear, and mass-to-mole conversions fail closed.
The runtime has no UCUM dependency. Receipts and daily audit data record the catalogue
identity, the resolved UCUM codes, and the compiled scale and offset.


## Returned data structures

`QueryResult` is a typed wrapper around two pandas DataFrames and their
provenance. Both DataFrames keep the same columns when the result is empty.

| Attribute | Shape | Purpose |
|---|---|---|
| `subjects` | One row per `(study_id, subject_id)` | Selected DM subjects and factual study context. |
| `observations` | One row per matching SEND measurement | Lossless, long-form endpoint records. |
| `metadata` | `QueryMetadata` dictionary | Canonical query, source identity, fingerprints, and membership hash. |
| `receipt` | `QueryReceipt` dictionary | Counts, assumptions, warnings, and observation summary. |
| `endpoint_facts` | Tuple of `EndpointFacts` | Declared domain, sex applicability, and unit conversion for each endpoint. |

The `subjects` columns have four groups:

| Group | Columns |
|---|---|
| Keys | `sample_id`, `study_id`, `subject_id` |
| Subject facts | `species`, `strain`, `sex`, `age_days`, `baseline_body_weight_g` |
| Study facts | `study_year`, `study_length_days`, `study_types`, `test_facilities`, `glp_statuses` |
| Treatment facts | `suppliers`, `routes`, `dose_frequencies`, `treatment_vehicles`, `first_exposure_day` |

`observations` preserves the source result and the information needed to audit
it. Its main columns are:

| Group | Columns |
|---|---|
| Keys | `study_id`, `subject_id`, `sample_id`, `study_day`, `endpoint_name` |
| SEND identity | `domain`, `test_code`, `test_name`, `specimen`, `measurement_identity`, `endpoint_key` |
| Result | `result_state`, `numeric_value`, `raw_value`, `unit`, `excluded` |
| Audit | `value_column`, `unit_column`, `exclusion_reason`, `failure_reason`, `source_order`, `explicit_baseline` |

Other columns retain sex, pool, method, laterality, portion, fasting, and
laboratory context. `result_state` distinguishes numeric, qualitative,
censored, failed, and absent source results. The table does not collapse
records into subject-day cells.

Both wide tables are sparse because endpoints use different collection
schedules. In `values`, an endpoint cell contains the selected number; `NaN`
means no numeric value was selected for that endpoint and day. In `states`, an
endpoint cell contains one or more comma-separated source-record labels such as
`numeric_observed`, `censored_observed`, or `explicitly_failed`; `NaN` means no
matching source record mapped to that cell. A populated state can therefore
correspond to `NaN` in `values`. Use `cell_audit` to distinguish conversion,
duplicate, and reported-conflict outcomes.

## Predicate AST

Groups use `group: and` or `group: or`, a non-empty `children` list, and
optional `negated: true`. Rules have `field`, `operator`, and `value`; there is
no separate rule discriminator. Field variants are:

| Field | Required keys | Meaning |
|---|---|---|
| `send_column` | `domain`, `column`, `value_type` | An approved SEND column. |
| `derived` | `name`, `value_type` | Existing package-derived semantics. |
| `pre_study_endpoint` | `endpoint`, `value_type` | Existing endpoint/baseline semantics. |

`pre_study_endpoint.endpoint` must exactly name a declared
`observation_contract.endpoints` entry. The package rejects arbitrary
endpoint-code derivation. SEND domain and column identifiers must pass
conservative uppercase validation before execution quotes them. Each
`send_column` `(domain, column)` pair and its `value_type` must also match the
packaged `grit42_endpoint_catalogue.2026-08-22.v1.json` capture. Use
`grit42_catalogue_identity()` to inspect the catalogue version and capture
date used by the parser.

The canonical operators are `eq`, `ne`, `in`, `not_in`, `contains`,
`not_contains`, `starts_with`, `not_starts_with`, `ends_with`, `not_ends_with`,
`is_empty`, `is_not_empty`, `gt`, `gte`, `lt`, `lte`, `between`, and
`not_between`. Values have type `string`, `number`, or `datetime`; `in`/`not_in`
use typed lists and `between`/`not_between` use exactly two bounds. `is_empty`
and `is_not_empty` need no value.

String operations are exact and case-sensitive. Numeric values must be finite;
`between` is inclusive. Datetimes must use supported strict formats. Missing or
invalid values never satisfy comparisons, including negative comparisons.
`is_empty` means NULL or a zero-length string only.

Offset-aware datetime rules compare normalized instants, so equivalent `Z` and
numeric-offset values match. Date-only and timezone-naive rules compare naive
wall-clock values. One list or range cannot mix aware and naive values, and an
aware rule does not coerce a naive source timestamp through the machine's local
timezone.

Imported advanced grit42 queries also use dated, endpoint-specific source
semantics from the packaged catalogue. Live saved-query probes show that
`DM.RFSTDTC` is compared as midnight on its calendar date, although the domain
API and source export expose an offset-aware time. This adjustment is limited
to that catalogue column and import contract. Canonical queries keep the strict
datetime behavior above, and `explain_query()` records the applied assumption.

Direct `send_column` rules in one group and one SEND domain are evaluated
against one source row before the result is lifted to subject membership. This
keeps, for example, `LBTESTCD == AST` paired with `LBSTRESN > 1.8` on the same
LB record. Negation on a group that contains only direct rules from one domain
is also evaluated on each source row before the existential subject lift. A
subject with no row in that domain does not satisfy that row-level negation. A
nested group creates a separate subject set, even when its rules use the same
domain as a sibling group. Different SEND domains, derived rules, and
`pre_study_endpoint` rules also lift independently. Study-level relations
without `USUBJID` propagate each matching `STUDYID` to every DM subject in that
study. Other parent-group negation uses the DM-universe complement.

`explain_query()` labels the effective domain-level relation as
`scope: source_row` and the parent Boolean relation as `scope: subject_set`.
The individual rule nodes remain diagnostic existential counts; the
`source_row` node is authoritative when several direct rules share a domain.

Normalization sorts commutative group children by semantic digest and removes
exact duplicates. `fingerprint_predicate(query.predicate)` hashes a
`vcgen.subject_predicate.v1` payload. It excludes query ID, observations, and
import provenance.

## Saved Grit42 queries

Use the complete saved-query payload. The package guide
[Grit42 saved-query export guide](https://github.com/lewis-k-s/vcgen-query/blob/main/GRIT42_SAVED_QUERY_GUIDE.md) describes
retrieval, import, and local execution for both saved form and advanced
records. The separate
[membership-parity guide](https://github.com/lewis-k-s/vcgen-query/blob/main/GRIT42_MEMBERSHIP_PARITY.md) defines the narrow live
query bank and the API-backed comparison workflow.


```python
import json
from pathlib import Path

import vcgen_query as vq

saved = json.loads(Path("grit42-saved-query.json").read_text())
query = vq.import_grit42_subject_query(saved)
assert query.contract == "vcgen.cohort_query.v4"
assert query.import_provenance.contract == "grit42.subject_query_import.v1"
print(query.import_provenance.source_contract)
print(query.import_provenance.source_payload_sha256)
print(vq.fingerprint_predicate(query.predicate))
print(vq.explain_query("/data/frozen-wider-source.zip", query))
```

`import_grit42_subject_query(saved)` dispatches a complete saved form or
advanced record. The specific entry points are
`import_grit42_form_subject_query(saved)` and
`import_grit42_advanced_subject_query(saved)`. There is no standalone
`form_values` bypass. The source-payload hash and predicate fingerprint remain
separate.

## Included examples and notebooks

| Resource | Use |
|---|---|
| [`all_subjects.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/examples/all_subjects.yaml) | Minimal unrestricted v4 query. |
| [`rat_28d_cohort.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/examples/rat_28d_cohort.yaml) | Subject-selection v4 query. |
| [`rat_28d_with_observations.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/examples/rat_28d_with_observations.yaml) | v4 membership plus body-weight and haematocrit extraction. |
| [`wistar_han_28d_historical.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/queries/wistar_han_28d_historical.yaml) | Broad Wistar Han discovery and source-acquisition query. |
| [`gurjanov_28d_benchmark_full.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/queries/gurjanov_28d_benchmark_full.yaml) | Strict cohort with the full 65-endpoint local benchmark mapping. |
| [`gurjanov_28d_generation_core.yaml`](https://github.com/lewis-k-s/vcgen-query/blob/main/src/vcgen_query/data/queries/gurjanov_28d_generation_core.yaml) | Production cohort with the 48-endpoint generation core. |
| [`01_bundled_wistar_han_28d_query.ipynb`](https://github.com/lewis-k-s/vcgen-query/blob/main/notebooks/01_bundled_wistar_han_28d_query.ipynb) | Run the Gurjanov-based generation query, index daily body weight, select terminal laboratory rows, and reshape them to long form. |
| [`02_editable_cohort_query.ipynb`](https://github.com/lewis-k-s/vcgen-query/blob/main/notebooks/02_editable_cohort_query.ipynb) | Define a small editable v4 query and index its daily result. |

The Wistar notebook keeps the factual query result unchanged. It applies a
study-specific day 21--35 terminal window only while it prepares a compact
laboratory table for display.
`dose_duration_days` is sourced from SEND `DOSDUR` with strict day and week
normalization. Month and year durations fail closed because they do not define
an exact day count without dates. Dose duration does not claim that the
complete source study lasted only that many elapsed days.

When a study reports several `DOSDUR` values, the derived predicate evaluates
the complete set with the same existential study-level semantics as source TS
rows. It does not silently select the smallest duration. The scalar subject
context duration is present only when the normalized values agree.

The notebooks ship in the source distribution and wheel under
`vcgen_query/notebooks`. For an installed wheel, locate and copy one to a
writable directory before editing:

```python
from importlib.resources import files

print(files("vcgen_query").joinpath("notebooks"))
```

Place `rodent_db_export.zip` in the repository root, or export the
`VCGEN_SEND_ARCHIVE` environment variable with a SEND `.zip` archive or an
extracted CSV directory. For a ZIP source, the package extracts only the
required CSV domains into a content-addressed cache and opens an in-memory
DuckDB connection. It does not need a DuckDB file path.

## License

This repository is publicly readable, but `vcgen-query` is proprietary. No
open-source license is granted.

## Development

Run package tests from the package directory:

```bash
cd packages/vcgen-query
uv run --extra dev python -m pytest -q
```
