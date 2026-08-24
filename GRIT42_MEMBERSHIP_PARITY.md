# Grit42 subject-membership parity

The live Grit42 OpenAPI document exposes saved-query reads and per-domain
record filters. It does not expose an operation that creates an arbitrary
saved subject query. Create each narrow bank query in the Advanced Query
UI, save them, and record their IDs in a local bindings file.

New Advanced queries select `DM.SPECIES`, `DM.STRAIN`, `EX.EXTRTV`,
`EX.EXDOSFRQ`, and `EX.EXROUTE` by default. The **Columns** tab lets the user
select the wider SEND inventory before those fields are used on the
**Conditions** tab.

The raw Columns-page capture is
`src/vcgen_query/data/grit42_columns.2026-08-22.v1.json`. The derived catalogue
is `src/vcgen_query/data/grit42_endpoint_catalogue.2026-08-22.v1.json`. It
records 36 domain groups, 830 domain-endpoint occurrences, 602 unique endpoint
codes, and each column's grit42 UI type. The domain grouping and types come
from the live Columns page on 2026-08-22. Endpoint descriptions come from
`docs/send_column_lookup.csv`, because grit42 mounts a description only while
its endpoint is hovered. The catalogue records this source difference and the
observed `EG.FACTOR_CURATED` domain addition. It also records the live-probed
saved-query source representation for `DM.RFSTDTC`, with the evidence query
IDs and capture date. Rebuild it with:

```sh
uv run python packages/vcgen-query/scripts/build_grit42_endpoint_catalogue.py
```

Three banks are available:

- `grit42_membership_bank.v1.yaml` is the completed default-column smoke bank.
  Its ten saved queries matched grit42 exactly.
- `grit42_membership_bank.expanded.v1.yaml` contains the 37 distinct cases for
  wider columns, operators, string patterns, empty values, numeric boundaries,
  timestamp normalization, Boolean groups, and repeated-domain row scope.
- `grit42_membership_bank.study_domains.v1.yaml` uses the 20-subject RAT1 arm
  anchor. Its five cases prove propagation from TA, TE, and TX, including TX
  same-row and separate-group behavior. All five cases matched.

The default and expanded banks use study `481152` and keep each reference
bundle small. The expanded bank uses `DM.STUDYID == "481152"` and
`EX.EXDOSE == 0.0` as its ten-subject anchor. The study-domain bank uses
`DM.STUDYID == RAT1` and `DM.ARMCD == 1S1` as its 20-subject anchor.

`SendColumnField` parsing uses this dated catalogue at runtime. A rule now
fails closed when its `(domain, column)` pair is absent or its declared
`value_type` does not match the captured grit42 type. The catalogue maps
`string` and `entity` to query type `string`, `numeric` to `number`, and
`timestamp` to `datetime`. We do not duplicate all 830 pairs in Python
`Literal` definitions. That would require a code regeneration step and could
drift from the packaged catalogue.

## Authentication

Use the **API token** tab in the Grit42 **User settings** dialog. Do not put the
token in source code, a command argument, or a tracked file. Supply it through
`GRIT42_API_KEY`, or write it to the repository-root `.grit42-api-token` file
and set that file to owner-read/write only:

```sh
chmod 600 .grit42-api-token
```

The filename is ignored by Git. The harness never prints the token.

## Workflow

Validate the bank and create an ignored binding template:

```sh
uv run python -m vcgen_query.grit42_parity validate-bank
uv run python -m vcgen_query.grit42_parity init-bindings \
  --output tmp/grit42-parity/query-ids.yaml
```

Before creating saved queries, probe the current live study slice. This uses
only per-domain API filters and confirms that the small scout sets still have
useful overlap:

```sh
uv run python -m vcgen_query.grit42_parity probe \
  --output-dir tmp/grit42-parity/probe
```

Create each saved query with the exact root-group shape defined by
`grit42_membership_bank.v1.yaml`. Add the exact `EX.EXTRTV` base rule to every
query, preserve each nested group and group-level NOT, then put the saved
numeric ID in the bindings file. Give each query the
`VCGEN_PARITY_supported_` prefix and include its case ID so it is easy to audit
in the UI. Do not
copy a hand-written `condition_tree` to the server; the saved Grit42 record is
the source definition. Keep the saved queries until collaborators review the
comparison. Delete them only as a separate, explicit cleanup action.

For the expanded bank, first select all fields listed in its header comment on
the Columns tab. Preserve each nested group shape, then use one ignored
bindings file:

```sh
uv run python -m vcgen_query.grit42_parity \
  --bank packages/vcgen-query/src/vcgen_query/data/grit42_membership_bank.expanded.v1.yaml \
  validate-bank
uv run python -m vcgen_query.grit42_parity \
  --bank packages/vcgen-query/src/vcgen_query/data/grit42_membership_bank.expanded.v1.yaml \
  init-bindings --output tmp/grit42-parity/query-ids-expanded.yaml
uv run python -m vcgen_query.grit42_parity \
  --bank packages/vcgen-query/src/vcgen_query/data/grit42_membership_bank.expanded.v1.yaml \
  probe --output-dir tmp/grit42-parity/probe-expanded
uv run python -m vcgen_query.grit42_parity \
  --bank packages/vcgen-query/src/vcgen_query/data/grit42_membership_bank.expanded.v1.yaml \
  run --bindings tmp/grit42-parity/query-ids-expanded.yaml \
  --output-dir tmp/grit42-parity/run-expanded
```

Run the comparison:

```sh
uv run python -m vcgen_query.grit42_parity run \
  --bindings tmp/grit42-parity/query-ids.yaml \
  --output-dir tmp/grit42-parity/run
```

The runner performs these read-only operations:

1. Fetch the complete DM, EX, TS, and LB rows for the seed study through the
   single-domain query endpoints. The common anchor uses DM and EX; individual
   cases can also use TS and LB.
2. Retrieve each saved query and verify that its imported predicate fingerprint
   matches the versioned bank predicate.
3. Execute that imported predicate locally against the same live study slice.
4. Fetch only the matching DM records for the saved query and compare normalized
   `(STUDYID, USUBJID)` sets.

`summary.json` contains counts and hashes only. Raw live rows, saved-query
payloads, local explanation keys, and exact missing/extra subject keys remain
under ignored `tmp/` case directories.

The harness reports gaps but returns success when the run completes. This is an
investigation tool, not a CI parity gate. Review predicate mismatches before
interpreting membership differences.

## Observed expanded-bank result and implementation status

Before the row-scope implementation, the 2026-08-22 run matched 9 of 11 cases.
Nine cases had exact predicate and subject-membership parity. Two
repeated-LB-domain conjunctions differed:

- `ALT AND AST`: local subject-set intersection returned 2 subjects; grit42
  returned 0.
- `AST AND LBSTRESN > 1.8`: local independent subject-set intersection returned
  5 subjects; grit42 returned 4, which matches the bank's same-row diagnostic
  count.

A direct grouping probe then put `LBTESTCD == AST` and `LBSTRESN > 1.8` in two
separate nested groups under the root `AND`. Grit42 returned 5 subjects, and
the local executor returned the same 5. This confirms that group boundaries
are semantic in grit42:

- rules in one repeated-domain group form a same-row conjunction;
- separate groups form independent subject sets that the parent group combines.

The local executor now implements this distinction. It combines direct SEND
rules from one group and domain into one row predicate before it lifts the
result to subject membership. Nested groups, different domains, derived rules,
and pre-study endpoint rules lift independently. The expanded bank now records
the confirmed grit42 counts: `ALT AND AST` is 0 and same-group
`AST AND LBSTRESN > 1.8` is 4; the separate-group control remains 5.

The saved pre-fix output is under the ignored
`tmp/grit42-parity/run-expanded/` directory. The post-fix read-only run used the
same frozen source identity and produced 11 of 11 exact predicate and subject-
membership matches under the ignored
`tmp/grit42-parity/run-expanded-row-scope/` directory.

## Coverage ledger

The three live banks now give exact evidence for these behaviors:

| Area | Live evidence |
|---|---|
| Boolean structure | `AND`, `OR`, two nested levels, and group-level `NOT` |
| Group boundary | Same-group LB rules share one row; separate nested LB groups lift independently; row-group NOT runs before subject lift |
| Operators | String `eq`, `ne`, `in`, `not_in`, `contains`, `not_contains`, `starts_with`, `ends_with`, `is_empty`, `is_not_empty`; numeric and datetime `eq`, `gt`, `gte`, `lt`, `lte`; numeric `between`, `not_between` |
| Missing values | NULL-backed and true zero-length `is_empty`; negative string patterns exclude NULL |
| Granularity | Subject-grain DM, EX, and LB; study-grain TS, TA, TE, and TX propagation |
| Cross-domain sets | DM/EX intersections; DM/EX/TS/LB and DM/EX/TS/TA/TE/TX source slices |
| Types | Catalogue-backed entity/string, numeric, and timestamp rules |
| Import integrity | Exact imported and bank predicate fingerprints for every saved case |

The live runs exposed these import and execution shapes:
nested multiselect values, repeated range metadata, and minute-precision
timestamps; `like` and `not_like` are the wire names for Contains and Not
contains. The importer normalizes only these observed forms and still rejects
conflicting metadata. `DM.RFSTDTC` saved-query comparisons use calendar-date
midnight even though the domain API and source export expose an offset-aware
time. This source behavior is recorded on that catalogue column and applies
only to imported advanced grit42 queries.

The grit42 UI does not expose negative starts-with or ends-with operators. It
also cleared a value entered for `LB.LBDTC`. The selected live slices contain
NULL, non-empty, and zero-length strings but no whitespace-only string. These
unavailable states are not presented as live evidence; canonical local tests
retain coverage for them.
