# Bundled VCGen query data

This directory is the package-owned source of query contracts and reviewed
tables. Do not copy these files into the parent application's `data/`
directory. A copy can drift from the query package that parses and executes it.

| File or directory | Owner | Review command |
|---|---|---|
| `examples/`, `queries/` | Local query documents (`vcgen.subject_query.v1`, `vcgen.cohort_query.v7`). The format and the file list are in the [package README](../../../README.md#included-examples-and-notebooks). They hold no assumptions or warnings; execution receipts record runtime diagnostics. | |
| `saved_grit42_queries/` | Complete Grit42 source records for `import_grit42_subject_query()` only. The procedure is in the [saved-query guide](../../../GRIT42_SAVED_QUERY_GUIDE.md). The [VCG Service](../../../../../docs/service/README.md) does not accept these records. | |
| `vcgen.subject_query.v1.schema.json`, `vcgen.cohort_query.v7.schema.json`, `vcgen.subject_predicate.v1.schema.json` | `config.py`, `contract.py`, `query.py` | |
| `grit42.*.schema.json` | `grit42.py` | |
| `grit42_columns.*.json` (Grit42 column catalogue), `grit42_endpoint_catalogue.*.json` (Grit42 endpoint catalogue) | `grit42_catalogue.py`; `scripts/build_grit42_endpoint_catalogue.py` rebuilds the endpoint file | |
| `specimen_vocabulary.v1.yaml` | `specimen.py`: maps a raw LBSPEC string to a specimen class and a matrix | |
| `route_aliases.ex.v1.yaml` | `route_aliases.py`: maps the reviewed EXROUTE and EXMETHOD pairs, and the TS `ROUTE` values, to one canonical route | `vcgen_query.route_aliases.route_alias_inventory()` lists the EX pairs and the TS values with separate EX and TS counts. Run it when the source archive changes. |
| `dose_frequency_aliases.v1.yaml` | `frequency_aliases.py`: maps the reviewed EXDOSFRQ and TS PDOSFRQ spellings to one canonical dose frequency. A control-only study takes its route, frequency and treatment from TS values (the TS exposure fallback, [Query pipeline stages](../../../../../docs/query_pipeline_stages.md)). | `vcgen_query.frequency_aliases.frequency_alias_inventory()` lists the EX and TS `PDOSFRQ` values. Run it when the source archive changes. |
| `unit_catalogue.v3.json` (unit catalogue) | `unit_catalogue.py`; `tests/test_unit_catalogue.py` compares it with a UCUM reference implementation and with examples from the US National Library of Medicine UCUM service | |

Both alias tables use the loader `alias_table.py`. A value that is not in an
alias table maps to no route or no frequency.

## Unit catalogue annotations

A UCUM code with an annotation names one analyte or one scaled quantity, and
its scale holds the conversion factor: `g/L{Hb}` is an amount concentration
(1 / 16,114.5 g/mol of the haemoglobin monomer), and `%{SG}` is specific
gravity times 100. In plain UCUM an annotation has no effect, so a plain UCUM
tool refuses these codes or converts them incorrectly. The unit catalogue is
the authority for them. Do not convert a `source_ucum` value with another UCUM
library.

## Not in this package

The package has no endpoint catalogue and no data decisions file. They are
outputs of the curate step of the parent project
(`scripts/curate_endpoint_catalogue.py`), stored in DVC under
`data/catalogue/`:

- `catalogue.json` (`vcgen.canonical_endpoint_catalogue.v6`), read by
  `load_endpoint_catalogue()`.
- `data_decisions.yaml` (`vcgen.data_decisions.v3`), read by
  `load_data_decisions()`. The actions and their keys are in the docstring of
  [`data_decisions.py`](../data_decisions.py). The query applies them during
  observation extraction.

Both loaders read `VCGEN_ENDPOINT_CATALOGUE`, else
`<VCGEN_SOURCE_ROOT>/data/catalogue/` (else the working directory), only when
each md5 is the one of the lock (`config/curation/catalogue/lock.yaml`,
`VCGEN_ENDPOINT_CATALOGUE_LOCK`). There is no fallback. The endpoint catalogue
owns each endpoint's canonical unit and its definition. How a query trace
reports the decisions is in
[`docs/service/README.md`](../../../../../docs/service/README.md).
`eda/upstream_report/build_grit42_data_report.py` replays the decisions for
the Grit42 upstream report.
