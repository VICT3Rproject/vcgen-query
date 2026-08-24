# vcgen-query

`vcgen-query` selects animals from SEND data and returns their recorded
measurements as pandas DataFrames. It accepts local VCGen queries and saved
Grit42 queries. It does not train models or generate virtual controls.

**Status:** public alpha (`0.3.0a0`).

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
python -m pip install "vcgen-query @ git+https://github.com/lewis-k-s/vcgen-query.git@v0.3.0a0"
# or, in a uv project
uv add "vcgen-query @ git+https://github.com/lewis-k-s/vcgen-query.git@v0.3.0a0"
```

For development:

```bash
git clone https://github.com/lewis-k-s/vcgen-query.git
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

A query has two separate parts:

- `predicate` selects the cohort.
- `observation_contract` selects the measurements to return for that cohort.

An omitted predicate selects all subjects in the SEND Demographics (`DM`)
domain.

```yaml
contract: vcgen.cohort_query.v4
query_id: wistar_han_28d
predicate:
  group: and
  children:
    - field: {kind: derived, name: dose_duration_days, value_type: number}
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
observation_contract:
  endpoints:
    - name: BW
      domain: BW
      test_codes: [BW]
    - name: LB_HCT
      domain: LB
      test_codes: [HCT]
      specimens: [BLOOD, WHOLE BLOOD]
      specimen_match: exact
      source_units: ["%"]
      target_unit: "%"
      day_window: {min: 1, max: 28}
```

Endpoint names are uppercase identifiers chosen by the query author. SEND
domains, columns, and value types must exist in the Grit42 column catalogue
included with the package. Day-window limits are inclusive.

### Specimen matching

Endpoint test codes, test names, and specimen values are normalized for case,
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

The [saved-query guide](GRIT42_SAVED_QUERY_GUIDE.md) explains how to retrieve,
import, and execute a query. The
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
| [`01_gurjanov_28d_generation_core.ipynb`](notebooks/01_gurjanov_28d_generation_core.ipynb) | Run the generation-core query and prepare daily body-weight and terminal laboratory tables. |
| [`02_editable_cohort_query.ipynb`](notebooks/02_editable_cohort_query.ipynb) | Define and run a small editable query. |

The 28-day filters use the SEND `DOSDUR` study-summary value. The package
accepts durations stated in days or weeks. It does not infer an exact number of
days from month or year units.

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
uv build
uv run python scripts/audit_release_artifacts.py dist/*
```

The test suite executes both notebooks against synthetic SEND data.

## License

This repository is publicly readable, but `vcgen-query` is proprietary. No
open-source license is granted.
