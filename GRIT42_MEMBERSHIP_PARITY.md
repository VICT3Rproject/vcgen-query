# Grit42 membership parity for maintainers

This is a maintainer runbook, not a user guide. It explains how to compare the
subjects selected by `vcgen-query` with the subjects selected by a saved
Grit42 query.

The comparison uses subject keys: `(STUDYID, USUBJID)`. It does not compare
measurement values or model inputs.

## What the harness does

The parity harness performs read-only Grit42 API operations:

1. It retrieves a saved query and the required SEND rows for a small study
   slice.
2. It imports and executes the saved query locally.
3. It compares the local and Grit42 subject-key sets.
4. It writes counts, hashes, and missing or extra keys under an ignored `tmp/`
   directory.

Grit42 does not expose an API operation for creating arbitrary advanced
queries. A maintainer must create and save each query in the Grit42 user
interface, then record its numeric ID in a local bindings file.

## Versioned query banks

A query bank is a versioned set of small comparison cases.

| Bank | Cases | Purpose |
|---|---:|---|
| `grit42_membership_bank.v1.yaml` | 10 | Basic filters using the columns selected by default in a new advanced query. |
| `grit42_membership_bank.expanded.v1.yaml` | 37 | Wider columns, comparison operators, missing values, nested groups, and same-row matching. |
| `grit42_membership_bank.study_domains.v1.yaml` | 5 | Filters from study-level domains such as Trial Arms, Trial Elements, and Trial Sets. |

The files are under `src/vcgen_query/data/`. Their header comments list the
Grit42 columns that must be enabled before the saved queries are created.

The banks contain expected counts for small diagnostic study slices. They are
test definitions, not a permanent claim about the current live database.

## Authentication

Use the **API token** tab in the Grit42 **User settings** dialog. Do not put the
token in source code, notebooks, command arguments, or tracked files.

The harness reads the token from `GRIT42_API_KEY`. It can also read the
repository-root `.grit42-api-token` file. The repository `.gitignore` excludes
that file. If you use it, restrict access to your user account:

```bash
chmod 600 .grit42-api-token
```

## Run the default bank

Validate the bank and create a local bindings template:

```bash
uv run python -m vcgen_query.grit42_parity validate-bank
uv run python -m vcgen_query.grit42_parity init-bindings \
  --output tmp/grit42-parity/query-ids.yaml
```

Create the saved queries in the Grit42 user interface. Keep the group structure
defined by the bank, then add each saved-query ID to the bindings file.

Use `probe` to check whether the small source slice is still suitable:

```bash
uv run python -m vcgen_query.grit42_parity probe \
  --output-dir tmp/grit42-parity/probe
```

Run the comparison:

```bash
uv run python -m vcgen_query.grit42_parity run \
  --bindings tmp/grit42-parity/query-ids.yaml \
  --output-dir tmp/grit42-parity/run
```

## Run another bank

Pass `--bank` before the command. For example:

```bash
uv run python -m vcgen_query.grit42_parity \
  --bank src/vcgen_query/data/grit42_membership_bank.expanded.v1.yaml \
  validate-bank
```

Use the same `--bank` option with `init-bindings`, `probe`, and `run`.

Run the CLI help for the full option list:

```bash
uv run python -m vcgen_query.grit42_parity --help
```

## Review the output

`summary.json` contains aggregate counts and hashes. Case directories contain
the saved-query record, local explanation, and exact missing or extra subject
keys. All output paths are under ignored `tmp/` directories by default.

The `run` command returns success when the comparison process completes, even
when one or more cases differ. Always inspect each case status and the reported
match count. Treat a difference as evidence to investigate, not as an automatic
test failure.

Do not commit raw Grit42 rows, API responses, bindings, or subject keys.
