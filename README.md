# vcgen-query

`vcgen-query` selects animals from SEND data and returns their recorded
measurements as pandas DataFrames. It accepts local VCGen queries and saved
Grit42 queries. It does not train models or generate virtual controls.

**Status:** public alpha (`0.4.0a0`).

## Vocabulary

The [glossary](../../GLOSSARY.md) defines the domain terms. These terms are
specific to this package:

| Term | Meaning in this package |
|---|---|
| SEND | Standard for Exchange of Nonclinical Data. SEND stores study data in named tables called domains: `DM` holds subject information, `BW` body weight, `LB` laboratory results, `TS` study summaries. A subject is one animal in a source study. |
| Predicate | The filter rules of a subject query. |
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

For development, clone `https://github.com/VICT3Rproject/vcgen-query.git` and
use the commands in [AGENTS.md](AGENTS.md#development-commands).

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
    query = vq.load_cohort_query(query_path)

membership = vq.evaluate_membership("/data/rodent_db_export.zip", query.subject_query)
result = vq.execute_query("/data/rodent_db_export.zip", query, membership=membership)

result.subjects.head()       # one row per selected animal
result.observations.head()   # one row per matching source measurement
result.receipt               # source, query, counts, and warnings
result.trace                 # one StageRecord per query stage, with counts
```

For a ZIP source, the default extraction directory is `.vcgen_query_cache`
next to the archive. It can become large when queries use many SEND domains.
Set an explicit location when the cache must be isolated or stored on another
volume:

```python
membership = vq.evaluate_membership(
    "/data/rodent_db_export.zip",
    query.subject_query,
    extract_dir="/scratch/vcgen-query/rodent-export",
)
result = vq.execute_query(
    "/data/rodent_db_export.zip",
    query,
    membership=membership,
    extract_dir="/scratch/vcgen-query/rodent-export",
)
```

The extraction cache is content-aware and is rebuilt when the archive changes.
It is not a UV package cache and is not part of a wheel or source distribution.

## Define a query

A cohort query is an ordered list of stage blocks. Each block carries display
metadata (`info`: id, name, kind, origin, description) and its parameters. The
stages, the stage records, and the curated exclusions are in
[Query pipeline stages](../../docs/query_pipeline_stages.md). The query library
runs four blocks: `SubjectQuery` (`vcgen.subject_query.v1`), `StudyExclusion`
(optional), `EndpointExclusion` (optional), and `EndpointObservations`.
The `SubjectQuery` block is the Grit42-compatible layer: the package keeps it
exactly as submitted, and its output membership is the Grit42 parity checkpoint.
`evaluate_membership()` computes the membership; `execute_query()` takes it as
its `membership` argument and does not evaluate the predicate itself. Endpoint
selection is not part of the subject query.

`CohortQuery.compose(subject_query, ["BW_BW", "LB_HCT_BLOOD"])` builds the
standard plan. Pass `study_exclusions=` and `endpoint_exclusions=` (a list,
possibly empty) to add the matching stage; the stage then records the review
even when nothing applies. `CohortQuery.submitted_endpoints` keeps what was
asked for, and `requested_endpoints` what is extracted. `execute_query()` runs
the blocks in order and returns one `StageRecord` per stage in `result.trace`.
When the caller passes a `StudySupport`, it adds a `support_selection` stage.

An omitted predicate selects all subjects in the SEND Demographics (`DM`)
domain.

```yaml
contract: vcgen.subject_query.v1
query_id: wistar_han_28d
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

The serialized cohort query nests the subject query and lists the later stages:

```yaml
contract: vcgen.cohort_query.v7
subject_query: {contract: vcgen.subject_query.v1, query_id: wistar_han_28d}
stages:
- stage: endpoint_observations
  requested_endpoints: [BW_BW, LB_HCT_BLOOD]
  endpoint_catalogue:
    contract: vcgen.canonical_endpoint_catalogue.v6
    catalogue_id: vcgen-endpoints-6b1a42fc119c
    md5: 5e17fc402368d6d1e1d5b2a651ac4907
```

Predicate groups keep their submitted child order, so explanation node paths
match the document. Fingerprints use an order-free canonical form, so a
reordered predicate has the same fingerprint.

Endpoint names are uppercase canonical identifiers from the locked endpoint
catalogue (see [`src/vcgen_query/data/README.md`](src/vcgen_query/data/README.md)).
A request must contain at least one unique name. Empty does not mean all. SEND
predicate domains, columns, and value types must exist in the Grit42 column
catalogue. Raw predicate columns do not become output endpoints.

### Source matching by the endpoint catalogue

The query YAML does not define source selectors. The locked endpoint catalogue stores
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
in the [NCI/CDISC SEND Controlled Terminology](https://evs.nci.nih.gov/ftp1/CDISC/SEND/SEND%20Terminology.html).

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

The `facility_group` derived predicate compares categorical source-facility
groups. It accepts a group key such as `site:charles_river:edinburgh` or a raw
test-facility spelling, which the query parser normalizes with the same rule
used for returned subject metadata. Case, punctuation, spacing, and reviewed
aliases map to one key. Unrecognized text becomes an `unresolved_label`
category; the parser does not infer a site from a similar spelling. The raw
`test_facilities` values remain available for audit. Group evidence is
`explicit_site`, `entity_only`, `opaque_code`, or `unresolved_label`.

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
`target_unit` is an optional UCUM code for the returned values. The package
converts a value only through the unit catalogue and raises an error instead of
guessing when a unit or conversion is not defined. The receipt identifies the
unit catalogue and records each conversion. The files are indexed in the
[data README](src/vcgen_query/data/README.md).

## Returned data structures

`QueryResult` contains the selected subjects and their measurements:

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

The subjects table has one row per `(study_id, subject_id)`. The observations
table has one row per matched source measurement. It retains the source result,
unit, status, and audit fields. Repeated measurements are separate rows, and
the package does not combine measurements of the same study day. Units remain
source units in `observations`; canonical target units and conversion rules are
available through `endpoint_facts`. `query_result_schema()` documents every
column, dtype, and nullability. A missing text value is `pd.NA`; do not convert
text with `astype(str)`, which writes `pd.NA` as `NaN`.

`result.membership` is the supported parity handoff when an application needs
the selected subject keys and complete source universe. It is the `membership`
that was passed to `execute_query()`. `evaluate_membership()` evaluates the
subject query alone, for tools that cannot execute the full observation query.
Subject keys are row-level data and must not be included in aggregate reports.

### Source identity validation

VCGen validates identity columns before it evaluates a query. It validates DM,
the active predicate and observation domains, and the source domains used for
derived subject context. A subject-grain domain must contain valid `STUDYID`
and `USUBJID` values. A study-grain predicate domain joins by `STUDYID` as
declared by the Grit42 column catalogue. The columns that happen to be in a
source file do not change this grain.

Execution raises `SourceDataError` when an active identity column is missing,
blank, padded with whitespace, or when DM contains duplicate
`(STUDYID, USUBJID)` keys. VCGen does not repair these errors. An observation
domain that is absent still produces a warning. A present observation domain
with invalid subject identity produces an error.

## Saved Grit42 queries

Use `import_grit42_subject_query()` with the complete saved-query record. The
importer detects whether it is a form or advanced query, validates its filter
rules, and records where the query came from. The
[saved-query guide](GRIT42_SAVED_QUERY_GUIDE.md) gives the steps to download,
import, and run a saved query.

## Included examples and notebooks

| Resource | Use |
|---|---|
| [`all_subjects.yaml`](src/vcgen_query/data/examples/all_subjects.yaml) | Select every subject. |
| [`wistar_han_28d_cohort.yaml`](src/vcgen_query/data/examples/wistar_han_28d_cohort.yaml) | A broad 28-day Wistar Han subject query. |
| [`wistar_han_28d_with_observations.yaml`](src/vcgen_query/data/examples/wistar_han_28d_with_observations.yaml) | A cohort query (v7): the broad 28-day Wistar Han subject query, then body weight and haematocrit. |
| [`wistar_han_28d_historical.yaml`](src/vcgen_query/data/queries/wistar_han_28d_historical.yaml) | A cohort query (v7) that finds candidate Wistar Han studies for source acquisition. |
| [`hosted_debug_wistar_han_28d.yaml`](src/vcgen_query/data/queries/hosted_debug_wistar_han_28d.yaml) | The subject query compiled from the checked-in Grit42 form fixture. |
| [`hosted_debug_wistar_han_28d.json`](src/vcgen_query/data/saved_grit42_queries/hosted_debug_wistar_han_28d.json) | Import an example Grit42 form record into a subject query. |
| [`02_editable_cohort_query.ipynb`](notebooks/02_editable_cohort_query.ipynb) | Define and run a small editable query. |

The 28-day filters use the resolved planned SEND `DOSDUR` value. The package
accepts durations stated in days or weeks. It does not infer an exact number of
days from month or year units, and it does not convert observed EX follow-up
into a planned duration.

Files under `data/examples` and `data/queries` are local query documents. Load
a `vcgen.subject_query.v1` file with `load_subject_query()` and a
`vcgen.cohort_query.v7` file with `load_cohort_query()`. Files under
`data/saved_grit42_queries` are Grit42 source records for
`import_grit42_subject_query()` only. The [VCG Service request
contract](../../docs/service/README.md) does not accept them.

The notebook uses a study-specific day 21-35 terminal window only for its
display table. It does not remove or change observations in the query result.

The wheel and the source distribution include the notebook under
`vcgen_query/notebooks`. It accepts `VCGEN_SEND_ARCHIVE` as the path to a SEND
ZIP archive or extracted CSV directory.

## License

This repository is publicly readable, but `vcgen-query` is proprietary. No
open-source license is granted.
