# Grit42 saved-query export and VCGen import

This guide imports saved Grit42 **advanced** and **form** subject queries.
VCGen validates their source shapes as `grit42.advanced_subject_query.v1` or
`grit42.form_subject_query.v1`, records `grit42.subject_query_import.v1`
provenance, compiles `vcgen.subject_predicate.v1`, and emits a complete
`vcgen.cohort_query.v4` query.

## What to save and export

Save the advanced or form query in the Grit42 UI with the checkmark. A stable
URL such as:

```text
https://vict3r.upf.edu/vict3rsubjectqueries/10321464
```

means the query definition is already saved under subject-query ID `10321464`.

The compiled dataset endpoint is a data export, not the query definition:

```text
https://vict3r.upf.edu/vict3rsubjectqueries/10321464/dataset
```

It returns selected SEND records but may omit the predicate condition tree,
selected-column inventory, rule `valueType`, and original group structure. Use
the saved-query payload to reconstruct an importable AST.

## Retrieve the saved-query payload

Use the authenticated Grit42 saved-query endpoint:

```text
GET https://vict3r.upf.edu/api/v1/subject_queries/{id}
```

For query `10321464`, use:

```text
GET https://vict3r.upf.edu/api/v1/subject_queries/10321464
```

The OpenAPI specification is available from the Swagger UI:

```text
https://vict3r.upf.edu/docs/api/index.html
https://vict3r.upf.edu/docs/api/v1/swagger.yaml
```

The specification defines `ApiKeyAuth` and sends it in the `X-API-KEY` header.
It calls this credential an API token. Users manage it in the **API token** tab
of the Grit42 **User settings** dialog. The API has no username/password
authentication method.

An unauthenticated read of query `10321464` returns HTTP 401. The endpoint is
reachable. VCGen needs a token authorized for that query to retrieve its
saved-query JSON.

Do not paste a token into this guide, source code, shell history, or chat. Set
`GRIT42_API_KEY` from the macOS keychain, CI secret store, or an approved
secret manager. Do not use a repository `.env` file by default. This
repository does not ignore `.env`, which creates a commit risk. If the team
adopts a local environment-file policy, first add the exact filename to
`.gitignore` and keep the file machine-local.

With `GRIT42_API_KEY` already set in the calling environment, a standard
library retrieval is:

```python
import json
import os
from urllib.request import Request, urlopen

query_id = 10321464
request = Request(
    f"https://vict3r.upf.edu/api/v1/subject_queries/{query_id}",
    headers={
        "Accept": "application/json",
        "X-API-KEY": os.environ["GRIT42_API_KEY"],
    },
)
with urlopen(request) as response:
    saved_query = json.load(response)
```

The saved-query endpoint serializes `columns` as domain-to-column lists.
Condition-tree rule fields use lower-case `send_domain_<domain>.<column>` names:

```json
{
  "id": 10321464,
  "columns": {
    "DM": ["species", "strain"],
    "TS": ["tsparmcd", "tsval"],
    "BW": ["studyid"]
  },
  "condition_tree": {
    "type": "group",
    "properties": {"conjunction": "AND", "not": false},
    "children1": [
      {
        "type": "rule",
        "properties": {
          "field": "send_domain_dm.species",
          "operator": "select_equals",
          "value": ["RAT"],
          "valueType": ["select"],
          "valueSrc": ["value"]
        }
      },
      {
        "type": "rule",
        "properties": {
          "field": "send_domain_bw.studyid",
          "operator": "is_not_empty",
          "value": [],
          "valueType": [],
          "valueSrc": []
        }
      }
    ]
  }
}
```

The importer normalizes these field IDs to uppercase `DOMAIN.COLUMN`
references and maps supported Grit42 operators and typed values to the
predicate-v1 model.

Grit42 owns the saved-query response schema. Do not rebuild this JSON by hand.
An advanced record requires `columns` and `condition_tree`. A
`query_type: "form"` record requires `form_values`.

## Simple form query records

The same API endpoint returns fixed-form queries such as:

```json
{
  "id": 10326436,
  "query_type": "form",
  "form_values": {
    "species": ["RAT"],
    "strain": ["WISTAR HAN"],
    "sstyp": ["REPEAT DOSE TOXICITY"],
    "route": ["ORAL GAVAGE"]
  }
}
```

`import_grit42_subject_query()` dispatches saved form records to
`import_grit42_form_subject_query()`. Both require the complete saved-record
envelope. The complete untouched record is hashed for provenance; there is no
public standalone-`form_values` bypass.

## Import locally

```python
import json
from pathlib import Path

import vcgen_query as vq

raw_path = Path("grit42-subject-query-10321464.json")
saved_query = json.loads(raw_path.read_text(encoding="utf-8"))

query = vq.import_grit42_subject_query(saved_query)

assert query.contract == "vcgen.cohort_query.v4"
assert query.import_provenance.contract == "grit42.subject_query_import.v1"
print("source contract:", query.import_provenance.source_contract)
print("source payload hash:", query.import_provenance.source_payload_sha256)
print("predicate fingerprint:", vq.fingerprint_predicate(query.predicate))
```

The importer uses `columns` as source inventory and provenance. It validates
each rule against that inventory, requires a supported `valueType`, and
normalizes operators and typed values before producing predicate v1 within a
cohort-query v4 document.

## Execute and inspect the imported query

Use the same frozen wider SEND source for local execution:

```python
explanation = vq.explain_query(
    "/data/frozen-wider-source.zip",
    query,
    include_subject_keys=True,
)
print(explanation["matched_subject_count"])
print(explanation["membership_hash"])

result = vq.execute_query("/data/frozen-wider-source.zip", query)
print(result.receipt)
```

`explain_query()` reports a count for each AST node. `execute_query()` returns
selected subject rows and a separate observation extraction result.

The imported predicate selects membership. `columns` are not model
observations. The local `observation_contract` defines endpoint identity,
units, specimens, sex applicability, and day windows. The host model protocol
defines model roles and generation policy.

Observation endpoint names are uppercase machine identifiers. Human category
selectors are normalized during parsing, and day windows use inclusive integer
bounds. Each `source_units` value is an exact source label. An optional
`target_unit` is a canonical, case-sensitive UCUM code. The package compiles
compatible conversions from its internal catalogue. Unknown observation-contract
keys and invalid unit pairs fail closed.

## Execution model

- Grit42 advanced-query JSON and saved form records use different source
  contracts but both compile through import v1 into predicate v1.
- Direct `send_column` rules in the same group and SEND domain form one row
  predicate. All of those rules must match one source record before the result
  is lifted to subject membership.
- Nested groups are independent subject sets. Put same-domain rules in separate
  nested groups when they may match different source rows for one subject.
- Negation on a group made only from direct rules in one SEND domain is part of
  that row predicate. It is applied before the result is lifted to subjects.
  Other group negation complements the lifted set against the DM universe.
- Different SEND domains, derived rules, and `pre_study_endpoint` rules also
  lift independently. Relations without `USUBJID` propagate matching studies
  to all DM subjects in those studies.
- `explain_query()` reports `source_row` domain nodes and `subject_set` parent
  nodes. Individual rule counts are diagnostic; the domain node controls
  membership when several direct rules share a domain.
- The importer fails closed on unsupported fields, operators, value sources,
  malformed values, and missing type information.
- Grit42 serializes Contains and Not contains as `like` and `not_like`.
  Wildcard-bearing values still fail closed. Imported `DM.RFSTDTC` comparisons
  use its dated catalogue source semantics, which the live probes identify as
  calendar-date midnight.
