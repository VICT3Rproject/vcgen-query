# vcgen-query

`vcgen-query` selects animals from SEND data and returns their recorded
measurements as pandas DataFrames. It accepts local VCGen queries and saved
Grit42 queries. It does not train models or generate virtual controls.

**Status:** public alpha (`0.4.0a0`).

## Vocabulary

| Term | Meaning in this package |
|---|---|
| SEND | Standard for Exchange of Nonclinical Data. SEND stores nonclinical study data in named tables called domains. |
| Subject | An animal in a source study. |
| Domain | A SEND table. For example, `DM` contains subject information, `BW` contains body weight, `LB` contains laboratory results, and `TS` contains study summaries. |
| Endpoint | A measurement requested from the source data, such as body weight or haematocrit. |
| Predicate | The filter rules that select subjects. |
| Cohort | The subjects selected by a predicate. |
| Observation | One recorded measurement for one subject. |
| Grit42 | The VICT3R system that stores SEND data and supports saved subject queries. |
| UCUM | The Unified Code for Units of Measure, used to identify units and valid conversions. |

## Install

Until a package is published on PyPI, install it from the public GitHub
repository. Pin the release tag so that a later change to `main` cannot change
the installed package:

```bash
python -m pip install "vcgen-query @ git+https://github.com/VICT3Rproject/vcgen-query.git@v0.4.0a0"
# or, in a uv project
uv add "vcgen-query @ git+https://github.com/VICT3Rproject/vcgen-query.git@v0.4.0a0"
```

For development:

```bash
git clone https://github.com/VICT3Rproject/vcgen-query.git
cd vcgen-query
uv sync --extra dev
```

## Getting started

Load a query and execute it against a SEND ZIP archive or an extracted CSV
directory:

```python
from importlib.resources import as_file, files

import vcgen_query as vq

query_resource = files("vcgen_query").joinpath(
    "data/examples/wistar_han_28d_with_observations.yaml"
)
with as_file(query_resource) as query_path:
    query = vq.load_query(query_path)

result = vq.execute_query("/data/rodent_db_export.zip", query)

result.subjects.head()       # one row per selected animal
result.observations.head()   # one row per matching source measurement
result.receipt               # source, query, counts, and warnings
```

For a ZIP source, the default extraction directory is `.vcgen_query_cache`
next to the archive. It can become large when queries use many SEND domains.
Set an explicit location when the cache must be isolated or stored on another
volume:

```python
result = vq.execute_query(
    "/data/rodent_db_export.zip",
    query,
    extract_dir="/scratch/vcgen-query/rodent-export",
)
```

The extraction cache is content-aware and is rebuilt when the archive changes.
It is not a UV package cache and is not part of a wheel or source distribution.

The observations table retains the source result, unit, status, and audit
fields. It does not combine measurements that occur on the same study day.

Use `to_daily_frame()` when an analysis needs one column per endpoint and one
row per subject and study day:

```python
daily = vq.to_daily_frame(result, on_conflict="raise")

daily.values       # selected numeric values
daily.states       # source-result states for the same endpoint cells
daily.cell_audit   # unit conversion and duplicate-resolution details
daily.receipt      # projection policy and counts
```

The daily tables keep `study_id`, `subject_id`, and `study_day` as ordinary
columns. They do not set a pandas index.

## Define a query

A query has three package-owned parts:

- `predicate` selects the cohort.
- `requested_endpoints` is the required, ordered set of canonical measurements to return.
- `endpoint_catalogue` binds the query to the package catalogue that resolves those names.

An omitted predicate selects all subjects in the SEND Demographics (`DM`)
domain.

```yaml
contract: vcgen.cohort_query.v5
query_id: wistar_han_28d
requested_endpoints: [BW, LB_HCT]
endpoint_catalogue:
  contract: vcgen.canonical_endpoint_catalogue.v4
  catalogue_id: grit42-canonical-endpoints-2026-09-17-v4
  sha256: 8ef29868c68b1b02f7dba9aa7790b70c046d4898aa04b3f3e784ea67ec7152e4
predicate:
  group: and
  children:
    - field: {kind: derived, name: planned_dose_duration_days, value_type: number}
      operator: between
      value: [28, 28]
    - field: {kind: send_column, domain: DM, column: SPECIES, value_type: string}
      operator: eq
      value: RAT
    - field: {kind: send_column, domain: DM, column: STRAIN, value_type: string}
      operator: eq
      value: WISTAR HAN
    - field: {kind: send_column, domain: DM, column: SEX, value_type: string}
      operator: in
      value: [F, M]
```

Endpoint names are uppercase canonical identifiers from the package endpoint
catalogue. A request must contain at least one unique name. Empty does not mean
all. SEND predicate domains, columns, and value types must exist in the separate
Grit42 column catalogue. Raw predicate columns do not become output endpoints.

### Catalogue-owned source matching

The query YAML does not define source selectors. The package catalogue stores
each endpoint's domain, test aliases, specimen and method rules, source units,
canonical UCUM target unit, display label, primitive or deterministic-derived
methodology, and provenance. Timing and parent-alignment choices belong to the
generation representation, not to this query package. Ratio endpoints define their
ordered parents and fixed scale in this catalogue. Endpoint test
codes, test names, and specimen values are normalized for case,
spacing, and Unicode width before comparison. When `specimens` is present,
`specimen_match` controls the comparison:

| Mode | Behaviour |
|---|---|
| `exact` | Accept a specimen that equals one listed value. This is the default. |
| `contains` | Accept a specimen that contains one listed value as a substring. |
| `not` | Accept a present specimen that does not equal any listed value. |

A missing specimen does not match any of these modes. Use `contains` only when
the broader match is intentional. For example, configured `BLOOD` also matches
`WHOLE BLOOD` and `ARTERIAL BLOOD` in `contains` mode. The bundled HCT example
therefore uses exact `BLOOD` and `WHOLE BLOOD` values. These are distinct values
in the [NCI/CDISC SEND Controlled Terminology](https://evs.nci.nih.gov/ftp1/CDISC/SEND/SEND%20Terminology.pdf).

## How subject filters work

Groups combine rules with `and` or `or`. Set `negated: true` to select subjects
that do not match a group.

Three kinds of field are available:

| Field kind | Use |
|---|---|
| `send_column` | Match a value in a SEND domain and column. |
| `derived` | Match a value that the package derives from the source data, such as exposure duration in days. |
| `pre_study_endpoint` | Match a measurement recorded before the study treatment period. |

Rules from the same group and SEND domain must match the same source row. For
example, a laboratory test-code rule for `HCT` and a result-value rule must
match one laboratory record. Put the rules in separate nested groups when they
may match different records for the same subject.

Rules from different domains are combined at subject level. A matching row in
a study-level domain, such as `TS`, applies to all subjects in that study.

The `planned_dose_duration_days` predicate uses planned protocol duration from
SEND `DOSDUR`. A single numeric declaration applies directly, including when
another declaration is non-numeric. If a study declares multiple numeric
durations, complete subject-level EX interval evidence can select one declared
value. EX evidence alone never creates a planned value.

Returned subject metadata keeps the planned and observed facts separate:

- `planned_dose_duration_days` is the resolved `DOSDUR` value used by the
  predicate and downstream `DOSE_DURATION_DAYS` model condition.
- `observed_exposure_span_days` is present only when normalized EX intervals
  form one complete continuous span. Date bounds are preferred. Study-day
  bounds are a fallback and cannot cross the missing SEND day zero.
- `dose_duration_evidence_status` reports `declared_only`, `corroborated`,
  `shorter_than_planned`, `ambiguous`, or `unresolved`.

The package preserves raw `DOSDUR` declarations and normalized EX intervals in
its internal DuckDB views. SEND `SLENGTH` remains the separate overall
`study_length_days` field.

The main comparison operators are:

| Operator | Meaning |
|---|---|
| `eq`, `ne` | Equal, not equal. |
| `in`, `not_in` | In, or not in, a list of values. |
| `contains`, `starts_with`, `ends_with` | Text matching. The corresponding `not_` forms are also available. |
| `gt`, `gte`, `lt`, `lte` | Numeric or date-time comparisons. |
| `between`, `not_between` | Inside or outside an inclusive range. |
| `is_empty`, `is_not_empty` | Missing or present source values. |

Text comparisons are exact and case-sensitive. Missing or invalid values do
not satisfy a comparison. Date-time values use ISO 8601 notation. A timestamp
ending in `Z` is in Coordinated Universal Time (UTC) and is equivalent to an
offset of `+00:00`.

## Unit conversion

`source_units` lists the exact unit labels accepted from the source data.
`target_unit` is an optional UCUM code for the returned daily values.

VCGen includes a versioned catalogue of supported transformations based on
UCUM. Its tests compare the catalogue with a UCUM reference implementation and
with examples from the US National Library of Medicine UCUM service. The
package performs a conversion only when the catalogue defines the source and
target units as compatible. It raises an error instead of guessing when a unit
or conversion is not defined.

The query receipt and daily audit identify the catalogue and record which
conversion was applied.

## Returned data structures

`QueryResult` contains the selected cohort and its measurements:

| Attribute | Contents |
|---|---|
| `subjects` | One row per selected subject, with subject, study, and treatment information. |
| `observations` | One row per matching source measurement. |
| `metadata` | The normalized query, source identity, and stable query identifiers. |
| `receipt` | Counts, warnings, assumptions, and source information. |
| `endpoint_facts` | The declared domain, sex applicability, and unit for each endpoint. |

The two DataFrames have stable ordered columns and pandas dtypes, including
when they contain no rows. Inspect the complete data dictionary in code:

```python
subjects_schema = vq.query_result_schema("subjects")
observations_schema = vq.query_result_schema("observations")

subjects_schema.grain
subjects_schema.keys
[(column.name, column.pandas_dtype, column.nullable) for column in subjects_schema.columns]
```

The subjects table has one row per `(study_id, subject_id)`. Its normalized
context collections, such as `routes`, are `list[str]` values in object-backed
columns. Integer and numeric fields use pandas nullable `Int64` and `Float64`
dtypes.

The observations table has one row per matched source measurement. Repeated
measurements are separate rows. `study_day` uses nullable `Int64`,
`numeric_value` uses nullable `Float64`, and factual flags use nullable Boolean
dtype. Optional text is object-backed and uses Python `None`; this keeps the
pandas-to-record handoff convenient for applications. `source_order` is a
tuple used for deterministic ordering, not a scientific value. Units remain
source units in `observations`; canonical target units and conversion rules are
available through `endpoint_facts`.

`result.membership` is the supported parity handoff when an application needs
the selected subject keys and complete source universe. It comes from the same
predicate execution as `subjects` and `observations`. Prefer it over
`evaluate_query_membership()`, which performs a separate evaluation for parity
tools that cannot execute the full observation query. Subject keys are
row-level data and must not be included in aggregate reports.

### Source identity validation

VCGen validates identity columns before it evaluates a query. It validates DM,
the active predicate and observation domains, and the source domains used for
derived subject context. A subject-grain domain must contain valid `STUDYID`
and `USUBJID` values. A study-grain predicate domain joins by `STUDYID` as
declared by the versioned column catalogue. The columns that happen to be in a
source file do not change this grain.

Execution raises `SourceDataError` when an active identity column is missing,
blank, padded with whitespace, or when DM contains duplicate
`(STUDYID, USUBJID)` keys. VCGen does not repair these errors. An observation
domain that is absent still produces a warning. A present observation domain
with invalid subject identity produces an error.

Repeated observation rows remain in the long-form query result. The daily and
model layers keep their existing duplicate audit and resolution policies.

`DailyFrame` contains analysis-ready views:

| Attribute | Contents |
|---|---|
| `values` | Numeric endpoint values in wide format. |
| `states` | Labels such as `numeric_observed`, `censored_observed`, or `explicitly_failed`. |
| `cell_audit` | Source values, units, conversions, duplicates, and conflicts. |
| `receipt` | The projection policy and summary counts. |

The daily tables are sparse because endpoints follow different collection
schedules. `NaN` in `values` means that no numeric value was selected for that
endpoint and day. A state can still be present when a source record contains a
non-numeric, censored, or failed result.

## Saved Grit42 queries

Use `import_grit42_subject_query()` with the complete saved-query record. The
importer detects whether it is a form or advanced query, validates its filter
rules, and records where the query came from.

Download the query definition with
`GET /api/v1/subject_queries/{id}`, save the complete JSON response, and pass
the decoded response to `import_grit42_subject_query()`. Run the imported query
against a local SEND ZIP archive or extracted CSV directory with
`execute_query()`. The API download contains the query definition, not the SEND
records that match it.

The [saved-query guide](GRIT42_SAVED_QUERY_GUIDE.md) provides copyable code to
download, save, import, and execute a query. The
[membership-parity guide](GRIT42_MEMBERSHIP_PARITY.md) is a maintainer runbook
for comparing local subject selection with live Grit42 results.

## Included examples and notebooks

| Resource | Use |
|---|---|
| [`all_subjects.yaml`](src/vcgen_query/data/examples/all_subjects.yaml) | Select every subject. |
| [`wistar_han_28d_cohort.yaml`](src/vcgen_query/data/examples/wistar_han_28d_cohort.yaml) | Select a broad 28-day Wistar Han cohort. |
| [`wistar_han_28d_with_observations.yaml`](src/vcgen_query/data/examples/wistar_han_28d_with_observations.yaml) | Select the broad cohort and return body weight and haematocrit. |
| [`wistar_han_28d_historical.yaml`](src/vcgen_query/data/queries/wistar_han_28d_historical.yaml) | Find candidate Wistar Han studies for source acquisition. |
| [`gurjanov_28d_benchmark_full.yaml`](src/vcgen_query/data/queries/gurjanov_28d_benchmark_full.yaml) | Run the strict 65-endpoint local benchmark. |
| [`gurjanov_28d_generation_core.yaml`](src/vcgen_query/data/queries/gurjanov_28d_generation_core.yaml) | Return the 48 endpoints used for virtual-control generation. |
| [`hosted_debug_wistar_han_28d.yaml`](src/vcgen_query/data/queries/hosted_debug_wistar_han_28d.yaml) | Load the v5 contract compiled from the checked-in Grit42 form fixture. |
| [`hosted_debug_wistar_han_28d.json`](src/vcgen_query/data/saved_grit42_queries/hosted_debug_wistar_han_28d.json) | Import an example Grit42 form record into a local v5 query. |
| [`01_gurjanov_28d_generation_core.ipynb`](notebooks/01_gurjanov_28d_generation_core.ipynb) | Run the generation-core query and prepare daily body-weight and terminal laboratory tables. |
| [`02_editable_cohort_query.ipynb`](notebooks/02_editable_cohort_query.ipynb) | Define and run a small editable query. |

The 28-day filters use the resolved planned SEND `DOSDUR` value. The package
accepts durations stated in days or weeks. It does not infer an exact number of
days from month or year units, and it does not convert observed EX follow-up
into a planned duration.

Files under `data/queries` are local `vcgen.cohort_query.v5` documents for
`load_query()` and direct upload to the debug service client. Files under
`data/saved_grit42_queries` are Grit42 source records for
`import_grit42_subject_query()` only. The service no longer accepts raw Grit42
records. A service request repeats the v5 query's non-empty
`requested_endpoints` list in its parallel request field; the two lists must
match exactly.

The first notebook uses a study-specific day 21-35 terminal window only for its
display table. It does not remove or change observations in the query result.

The notebooks are included in the source distribution and wheel under
`vcgen_query/notebooks`. They accept `VCGEN_SEND_ARCHIVE` as the path to a SEND
ZIP archive or extracted CSV directory.

## Development

Run checks from the repository root:

```bash
uv run --extra dev ruff check .
uv run --extra dev python -m pytest -q
uv build --out-dir dist
uv run python scripts/audit_release_artifacts.py dist/*
```

The test suite executes both notebooks against synthetic SEND data.

## License

This repository is publicly readable, but `vcgen-query` is proprietary. No
open-source license is granted.
