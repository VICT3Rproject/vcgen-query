# Bundled VCGen query data

This directory is the package-owned source of query contracts and supporting
catalogues. Do not copy these query files into the parent application's
`data/` directory. A duplicate copy can drift from the query package that
parses and executes it.

## Cohort-query v5 documents

Every YAML file under `examples/` and `queries/` is a complete
`vcgen.cohort_query.v5` document with a nonempty ordered
`requested_endpoints` list. `vcgen_query.load_query()` validates the document,
the endpoint-catalogue identity, the predicate, and the requested endpoints.

| Category | File | Requested endpoints | Intended use |
|---|---|---:|---|
| Minimal example | `examples/all_subjects.yaml` | 1 | Smallest valid all-subject query |
| Predicate example | `examples/wistar_han_28d_cohort.yaml` | 1 | Wistar Han 28-day cohort selection |
| Observation example | `examples/wistar_han_28d_with_observations.yaml` | 2 | Predicate plus BW and LB_HCT selection |
| Staging debug | `queries/hosted_debug_wistar_han_28d.yaml` | 2 | Fast hosted-service check |
| Service generation | `queries/gurjanov_28d_generation_core.yaml` | 48 | Current staging generation set |
| Research benchmark | `queries/gurjanov_28d_benchmark_full.yaml` | 65 | Broader Gurjanov endpoint benchmark |
| Research discovery | `queries/wistar_han_28d_historical.yaml` | 65 | Historical-cohort discovery profile |

The service accepts the current supported cohort-query contract, v5. A future
contract version needs an explicit parser and schema update before the service
can accept it. The catalogue marks generation endpoints as requestable and can
also retain predicate-only endpoint definitions. Each query selects its exact
active endpoint set; deterministic derived parents are added internally when
required.

## Grit42 source records

`saved_grit42_queries/` contains complete Grit42 source records for importer
and parity work. These files are not service inputs. Convert one with
`vcgen_query.import_grit42_subject_query()` and supply an explicit endpoint
set before it crosses the service boundary.

## Supporting data

- `vcgen.cohort_query.v5.schema.json` and
  `vcgen.subject_predicate.v1.schema.json` define the local query contracts.
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
