# Bundled VCGen query data

This directory is the package-owned source of query contracts and reviewed
tables. Do not copy these files into the data directory of a caller. A copy can
drift from the query package that parses and executes it.

| File or directory | Owner | Review command |
|---|---|---|
| `examples/`, `queries/` | Local query documents (`vcgen.subject_query.v1`, `vcgen.cohort_query.v7`). The format and the file list are in the [package README](../../../README.md#included-examples-and-notebooks). They hold no assumptions or warnings; execution receipts record runtime diagnostics. | |
| `saved_grit42_queries/` | Complete Grit42 source records for `import_grit42_subject_query()` only. The procedure is in the [saved-query guide](../../../GRIT42_SAVED_QUERY_GUIDE.md). They are not query documents. | |
| `vcgen.subject_query.v1.schema.json`, `vcgen.cohort_query.v7.schema.json`, `vcgen.subject_predicate.v1.schema.json` | `config.py`, `contract.py`, `query.py` | |
| `grit42.*.schema.json` | `grit42.py` | |
| `grit42_columns.*.json` (Grit42 column catalogue), `grit42_endpoint_catalogue.*.json` (Grit42 endpoint catalogue) | `grit42_catalogue.py`; `scripts/build_grit42_endpoint_catalogue.py` rebuilds the endpoint file | |
| `specimen_vocabulary.v1.yaml` | `specimen.py`: maps a raw LBSPEC string to a specimen class and a matrix | |
| `route_aliases.ex.v1.yaml` | `route_aliases.py`: maps the reviewed EXROUTE and EXMETHOD pairs, and the TS `ROUTE` values, to one canonical route | `vcgen_query.route_aliases.route_alias_inventory()` lists the EX pairs and the TS values with separate EX and TS counts. Run it when the source archive changes. |
| `dose_frequency_aliases.v1.yaml` | `frequency_aliases.py`: maps the reviewed EXDOSFRQ and TS PDOSFRQ spellings to one canonical dose frequency. A control-only study takes its route, frequency and treatment from TS values (the TS exposure fallback). | `vcgen_query.frequency_aliases.frequency_alias_inventory()` lists the EX and TS `PDOSFRQ` values. Run it when the source archive changes. |
| `endpoint_catalogue/catalogue.json.gz` (bundled endpoint catalogue, `vcgen.canonical_endpoint_catalogue.v6`) | `endpoint_catalogue.py` (`bundled_catalogue_bytes()`); the gzip of the exact bytes of one locked endpoint catalogue, written with its catalogue lock. Its identity is the md5 of the decompressed bytes. Do not edit it. | The caller that owns the catalogue lock checks that the decompressed bytes have the locked md5. |
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

## Endpoint catalogue and data decisions

`load_endpoint_catalogue()` and `load_data_decisions()` return the active
endpoint catalogue and data decisions:

- A caller names a locked endpoint catalogue with both
  `VCGEN_ENDPOINT_CATALOGUE` (the path of `catalogue.json`) and
  `VCGEN_ENDPOINT_CATALOGUE_LOCK` (the path of its catalogue lock). The data
  decisions are `data_decisions.yaml` (`vcgen.data_decisions.v3`) beside
  `catalogue.json`. The package reads each file only when its md5 is the one
  that the catalogue lock names under `outputs` (`catalogue.json`,
  `data_decisions.yaml`). There is no fallback.
- A caller that sets only one of the two variables gets a `QueryError`.
- Without the two variables, the package uses its bundled endpoint catalogue
  and no data decisions. Data decisions name the studies of one reviewed
  source, so the package does not ship them.

The actions and the keys of the data decisions are in the docstring of
[`data_decisions.py`](../data_decisions.py). The query applies them during
observation extraction. The query metadata names the active data decisions
(`data_decisions`), and the endpoint observations stage record lists the
decisions that it applied (`data_decisions_applied`). The endpoint catalogue
owns each endpoint's canonical unit and its definition.
