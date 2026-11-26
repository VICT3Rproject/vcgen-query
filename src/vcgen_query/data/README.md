# Bundled VCGen query data

This directory is the package-owned source of query contracts and supporting
catalogues. Do not copy these query files into the parent application's
`data/` directory. A duplicate copy can drift from the query package that
parses and executes it.

## Query documents

A `vcgen.subject_query.v1` document selects subjects only. It is the
Grit42-compatible layer, and the package keeps it exactly as written. A
`vcgen.cohort_query.v7` document nests one subject query and lists the later
stages, currently one `endpoint_observations` stage with the requested
endpoints. Load them with `load_subject_query()` and `load_cohort_query()`.

| Category | File | Contract | Requested endpoints | Intended use |
|---|---|---|---:|---|
| Minimal example | `examples/all_subjects.yaml` | subject query | – | Smallest valid all-subject query |
| Predicate example | `examples/wistar_han_28d_cohort.yaml` | subject query | – | Wistar Han 28-day cohort selection |
| Observation example | `examples/wistar_han_28d_with_observations.yaml` | cohort query | 2 | The same cohort, then BW and LB_HCT |
| Staging debug | `queries/hosted_debug_wistar_han_28d.yaml` | subject query | – | Fast hosted-service check |
| Research discovery | `queries/wistar_han_28d_historical.yaml` | cohort query | 65 | Historical-cohort discovery profile |

The service accepts a subject query plus a separate endpoint selection. The
catalogue marks generation endpoints as requestable and can also retain
predicate-only endpoint definitions. Each request selects its exact active
endpoint set; deterministic derived parents are added internally when
required.

## Grit42 source records

`saved_grit42_queries/` contains complete Grit42 source records for importer
and parity work. These files are not service inputs. Convert one with
`vcgen_query.import_grit42_subject_query()` to a subject query. Parity compares
membership at the subject-query stage.

## Supporting data

- `vcgen.subject_query.v1.schema.json`, `vcgen.cohort_query.v7.schema.json`
  and `vcgen.subject_predicate.v1.schema.json` define the local query contracts.
- `grit42.*.schema.json` files define Grit42 import contracts.
- `grit42_columns.*.json` and `grit42_endpoint_catalogue.*.json` are versioned
  source validation catalogues.
- `grit42_membership_bank.*.yaml` files are parity fixtures, not executable
  service queries.
- `canonical_endpoints.*.json` owns each endpoint's canonical unit, temporal
  behavior, and primitive or deterministic-derived definition. Ratio
  definitions contain their ordered parents and fixed scale.
- `unit_catalogue.v2.json` defines the UCUM codes, aliases, dimensions, and
  affine conversions used to validate those canonical units.
