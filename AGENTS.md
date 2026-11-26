# Repository Guidelines

## Project Overview

`vcgen-query` is a Python library that selects animal cohorts and observations from local SEND data. It accepts VCGen YAML/JSON contracts and complete saved Grit42 query records, then returns provenance-aware pandas DataFrames. It does not train models or generate virtual controls.

The package is public alpha software. Keep its runtime dependency set small: DuckDB, pandas, and PyYAML only. Scientific modelling belongs in the parent project, not this package.

## Architecture & Data Flow

```text
query YAML/JSON or Grit42 record
  -> config.py / contract.py / query.py / grit42.py (load, import, validate)
  -> result.execute_query() runs the stage blocks in order and records a trace:
     source -> subject_query (Grit42 parity checkpoint) -> [study_exclusion]
     -> subject_context -> [endpoint_exclusion] -> endpoint_observations
  -> duckdb.py (open ZIP, CSV directory, or injected connection)
  -> set_execution.py (compile predicates to temporary subject-key relations)
  -> cohort.py (build subject and study context)
  -> observation.py (stream and parse matching endpoint rows)
  -> QueryResult (subjects, observations, same-execution membership, metadata,
     receipt, endpoint facts)
  -> daily.to_daily_frame() when a sparse daily projection is required
```

`src/vcgen_query/__init__.py` is the public API boundary. Keep orchestration in `result.py`; keep source access in `duckdb.py`; keep contract validation in `contract.py`; keep SEND parsing rules in `send_semantics.py` and `endpoint_rows.py`.

Important semantic rules:

- Rules in one predicate group and SEND domain must match the same source row.
- Rules from different domains combine at subject level. Study-level rows apply to all subjects in that study.
- Preserve long-form source observations. Daily projection is a separate, explicit step.
- Keep stable fingerprints, source identities, warnings, assumptions, and unit-catalogue identity in result metadata and receipts.
- Use bound SQL parameters and quoted identifiers. Do not interpolate query values into SQL.

State is local and synchronous: frozen dataclasses plus per-execution DuckDB temporary tables. There is no async execution or global state manager. Dependency injection is the existing `source` argument, which accepts an open DuckDB-like connection, a CSV directory, or a SEND ZIP archive.

## Key Directories

- `src/vcgen_query/`: library implementation and public API.
- `src/vcgen_query/data/`: bundled schemas, versioned catalogues, query examples, query profiles, and Grit42 membership banks.
- `tests/`: flat, behavior-oriented pytest suite with synthetic SEND data.
- `tests/fixtures/`: durable external golden data, currently including UCUM comparisons.
- `notebooks/`: executable user examples; both notebooks are package artifacts and test targets.
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

There is no installed CLI for normal query execution. Use the Python API:

```python
import vcgen_query as vq

subject = vq.load_subject_query("src/vcgen_query/data/examples/all_subjects.yaml")
query = vq.CohortQuery.compose(subject, ["BW"])
result = vq.execute_query("/path/to/send.zip", query, extract_dir="tmp/send-cache")
daily = vq.to_daily_frame(result, on_conflict="raise")
```

Regenerate the dated Grit42 endpoint catalogue only from the parent checkout layout:

```bash
uv run python scripts/build_grit42_endpoint_catalogue.py
```

## Code Conventions & Common Patterns

- Use `snake_case` for modules, functions, and variables; `PascalCase` for classes and dataclasses; leading underscores for private helpers.
- Use uppercase identifiers for SEND domains/columns and query endpoint names. Canonical operators remain lowercase, such as `eq` and `not_in`.
- Model validated contracts and result facts as typed, frozen dataclasses. Do not add mutable service containers or speculative interfaces.
- Parse external input fail-closed. Raise `QueryError` for query/contract errors; preserve exception causes. Use the dedicated daily conflict/unit errors for those projection failures.
- Record recoverable assumptions and warnings in receipts. Do not silently guess units, missing fields, source identity, or ambiguous measurements.
- Keep the subject query exactly as submitted: `SubjectQuery.to_dict()` returns the submitted document and predicate groups keep their child order. Keep fingerprints deterministic: `to_canonical_dict()` sorts and deduplicates where contract semantics permit it. Fingerprints and parity hashes depend on this behavior.
- Keep endpoint selection out of the subject query. Add later query steps as stage blocks in `query.py` with `info` metadata and a runner in `result.py`.
- Stream large endpoint tables with existing batch helpers. Do not materialize complete SEND domains without need.
- Keep execution synchronous. Do not introduce async wrappers around local DuckDB or pandas work.
- Reuse `importlib.resources` for bundled schemas and data. Do not depend on source-checkout-relative paths in library code.

## Important Files

- `src/vcgen_query/__init__.py`: supported public exports.
- `src/vcgen_query/contract.py`: predicate and field model, parsing, validation, and canonical forms.
- `src/vcgen_query/query.py`: stage blocks (`SubjectQuery`, `EndpointObservations`), `CohortQuery` (v7), `StageInfo`, and `StageRecord`.
- `src/vcgen_query/result.py`: `execute_query()`, `explain_query()`, `QueryResult`, metadata, and receipts.
- `src/vcgen_query/duckdb.py`: archive/directory loading, cache identity, raw views, and derived SEND views.
- `src/vcgen_query/set_execution.py`: predicate compilation, set algebra, and explanation traces.
- `src/vcgen_query/observation.py`: long-form endpoint extraction and observation receipts.
- `src/vcgen_query/daily.py`: unit-aware daily projection and duplicate/conflict policy.
- `src/vcgen_query/grit42.py`: fail-closed saved-query import and deterministic membership comparison.
- `pyproject.toml`: package metadata, dependencies, Hatchling build contents, and pytest paths.
- `README.md`: public query semantics, API examples, result contracts, and notebook behavior.
- `GRIT42_SAVED_QUERY_GUIDE.md` and `GRIT42_MEMBERSHIP_PARITY.md`: saved-query and live-parity operating rules.

## Runtime/Tooling Preferences

- Required runtime: Python `>=3.11`.
- Package/environment manager: `uv`; never use bare `pip` or manually activate `.venv`.
- Build backend: Hatchling through `uv build --out-dir dist`.
- Linting: Ruff from the `dev` extra. The repository has no custom Ruff configuration.
- Testing: pytest from the `dev` extra. No type checker, coverage threshold, or console-script entry point is configured.
- Keep ZIP extraction caches in an explicit ignored location when possible. `.vcgen_query_cache/`, `tmp/`, build output, DuckDB files, checkpoints, raw SEND data, API responses, query bindings, tokens, and subject keys must not be committed.
- Keep Grit42 credentials in `GRIT42_API_KEY` or the ignored local token file. Never put secrets in source, notebooks, command arguments, or logs.
- Treat bundled versioned catalogues and generated manifests as generated evidence. Update their source inputs and builder, then regenerate; do not hand-edit outputs.

## Testing & QA

Pytest discovers `tests/test_*.py` and `test_*` functions. Tests are behavior-oriented and usually exercise the public API against deterministic synthetic SEND data.

- Reuse `tests/conftest.py` fixtures: `make_connection` for in-memory DuckDB data and `basic_subjects` for the standard multi-study cohort.
- Use `tmp_path` for ZIP, CSV, cache, API, and other file-backed cases. Mock network calls; tests must not require live Grit42 access.
- Add regression coverage for contract, scientific-semantic, provenance, cache, unit, or security defects. Assert DataFrame schemas, receipts, errors, hashes, and observable membership rather than implementation text.
- Keep bundled data checks in `test_config.py`, `test_schema_composition.py`, `test_grit42_parity.py`, and `test_unit_catalogue.py` consistent with data changes.
- `tests/test_notebooks.py` executes both notebooks headlessly against synthetic SEND data. Notebook changes must keep this package-specific contract working.
- There are no custom pytest markers and no configured coverage target. Hypothesis is installed but is not an established test pattern here.

Run the smallest affected test first, then the full pytest and Ruff commands before delivery. Build with `uv build --out-dir dist` and audit the resulting archives when package data, notebooks, guides, or `pyproject.toml` changes.
