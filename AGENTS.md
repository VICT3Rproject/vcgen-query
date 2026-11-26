# Repository Guidelines

This package is public alpha software. Keep its runtime dependency set small (`pyproject.toml`). Scientific modelling belongs in the parent project, not this package. The [README](README.md) owns the public API and query semantics.

## Architecture & Data Flow

```text
query YAML/JSON or Grit42 record
  -> config.py / contract.py / query.py / grit42.py (load, import, validate)
  -> result.evaluate_membership() evaluates the subject predicate (or the caller loads a cached
     PredicateEvaluation); result.execute_query(membership=...) never evaluates it. It runs
     the stage blocks in order and records a trace. The stage order is in
     docs/query_pipeline_stages.md of the parent repository.
  -> duckdb.py (open ZIP, CSV directory, or injected connection)
  -> set_execution.py (compile predicates to temporary subject-key relations)
  -> cohort.py (build subject and study context)
  -> observation.py (stream and parse matching endpoint rows)
  -> QueryResult (subjects, observations, same-execution membership, metadata,
     receipt, endpoint facts)
```

`src/vcgen_query/__init__.py` is the public API boundary. Keep orchestration in `result.py`; keep source access in `duckdb.py`; keep contract validation in `contract.py`; keep SEND parsing rules in `send_semantics.py` and `endpoint_rows.py`.

Semantic rules:

- Subject-filter matching rules (same row within one predicate group and SEND domain, subject level across domains) are in the README, "How subject filters work".
- Preserve long-form source observations. Keep stable fingerprints, source identities, warnings, assumptions, and unit-catalogue identity in result metadata and receipts.
- Use bound SQL parameters and quoted identifiers. Do not interpolate query values into SQL.

State is local and synchronous: frozen dataclasses plus per-execution DuckDB temporary tables. There is no async execution or global state manager. Dependency injection is the existing `source` argument, which accepts an open DuckDB-like connection, a CSV directory, or a SEND ZIP archive.

## Key Directories

- `src/vcgen_query/`: library implementation and public API.
- `src/vcgen_query/data/`: bundled schemas, query documents, and reviewed tables; `src/vcgen_query/data/README.md` indexes them. The endpoint catalogue and the data decisions are not here.
- `tests/`: flat, behavior-oriented pytest suite with synthetic SEND data.
- `tests/fixtures/`: durable external golden data, currently including UCUM comparisons.
- `notebooks/`: the executable user example `02_editable_cohort_query.ipynb`, which the wheel includes. No test executes it.
- `scripts/`: maintainer-only data-generation tools. These are not installed console commands.
- `tmp/`: ignored investigation and parity output. Put local temporary work here.

## Development Commands

Use `uv` for every environment and Python command. Run commands from this package root unless stated otherwise.

```bash
uv sync --extra dev
uv run --extra dev ruff check .
uv run --extra dev python -m pytest -q
uv run --extra dev python -m pytest -q tests/test_result.py
uv build --out-dir dist
uv run python scripts/audit_release_artifacts.py dist/*
```

There is no installed CLI for normal query execution. Use the Python API in the [README](README.md#getting-started).

Regenerate the dated Grit42 endpoint catalogue only from the parent checkout layout:

```bash
uv run python scripts/build_grit42_endpoint_catalogue.py
```

## Code Conventions & Common Patterns

- Use `snake_case` for modules, functions, and variables; `PascalCase` for classes and dataclasses; leading underscores for private helpers.
- Use uppercase identifiers for SEND domains/columns and query endpoint names. Canonical operators remain lowercase, such as `eq` and `not_in`.
- Model validated contracts and result facts as typed, frozen dataclasses. Do not add mutable service containers or speculative interfaces.
- Parse external input fail-closed. Raise `QueryError` for query/contract errors; preserve exception causes.
- Record recoverable assumptions and warnings in receipts. Do not silently guess units, missing fields, source identity, or ambiguous measurements.
- Keep the subject query exactly as submitted and keep fingerprints deterministic. Fingerprints and parity hashes depend on both ([Query pipeline stages](../../docs/query_pipeline_stages.md)).
- Keep endpoint selection out of the subject query. Add later query steps as stage blocks in `query.py` (register the class in `_STAGE_TYPES`, with `info` metadata) and a runner in `result.py` (`_STAGE_STEPS`).
- Stream large endpoint tables with existing batch helpers. Do not materialize complete SEND domains without need.
- Keep execution synchronous. Do not introduce async wrappers around local DuckDB or pandas work.
- Reuse `importlib.resources` for bundled schemas and data. Do not depend on source-checkout-relative paths in library code.
- Keep ZIP extraction caches in an explicit ignored location when possible. `.vcgen_query_cache/`, `tmp/`, build output, DuckDB files, checkpoints, raw SEND data, API responses, query bindings, tokens, and subject keys must not be committed.
- Keep Grit42 credentials in `GRIT42_API_KEY` or the ignored local token file. Never put secrets in source, notebooks, command arguments, or logs.
- Treat bundled versioned tables and generated manifests as generated evidence. Update their source inputs and builder, then regenerate; do not hand-edit outputs.

## Testing & QA

Pytest discovers `tests/test_*.py` and `test_*` functions. Tests are behavior-oriented and usually exercise the public API against deterministic synthetic SEND data.

- Reuse `tests/conftest.py` fixtures: `make_connection` for in-memory DuckDB data and `basic_subjects` for the standard multi-study subject set.
- Use `tmp_path` for ZIP, CSV, cache, API, and other file-backed cases. Mock network calls; tests must not require live Grit42 access.
- Add regression coverage for contract, scientific-semantic, provenance, cache, unit, or security defects. Assert DataFrame schemas, receipts, errors, hashes, and observable membership rather than implementation text.
- Keep the bundled data checks, such as `test_locked_catalogue.py` and `test_unit_catalogue.py`, consistent with data changes.
- There are no custom pytest markers and no configured coverage target. Hypothesis is installed but is not an established test pattern here.

Run the smallest affected test first, then the full pytest and Ruff commands before delivery. Build with `uv build --out-dir dist` and audit the resulting archives when package data, notebooks, guides, or `pyproject.toml` changes.
