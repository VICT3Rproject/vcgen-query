# Download and use a saved VICT3R query

This guide shows how to download a saved subject-query definition from the
VICT3R (Grit42) API, import it into `vcgen-query`, and run it against a local
SEND source.

Grit42 supports two saved-query formats:

- A form query uses the standard Grit42 subject-query form.
- An advanced query uses nested filter groups and SEND columns.

`import_grit42_subject_query()` accepts both formats.

## Before you start

You need:

- access to the saved Grit42 query;
- a Grit42 API token that can read it;
- the complete saved-query JSON record; and
- a SEND ZIP archive or extracted CSV directory for local execution.

## 1. Save the query definition

Save the form or advanced query in the Grit42 user interface. A URL such as
this contains the saved-query ID:

```text
https://vict3r.upf.edu/vict3rsubjectqueries/10321464
```

The `/dataset` URL is a data export:

```text
https://vict3r.upf.edu/vict3rsubjectqueries/10321464/dataset
```

Do not use the dataset export as the query definition. It contains selected
SEND records, but it may not contain all filter rules, selected columns, or
nested groups.

## 2. Download the saved-query JSON

Read the query through the authenticated API:

```text
GET https://vict3r.upf.edu/api/v1/subject_queries/{id}
```

Users manage API tokens in the **API token** tab of the Grit42 **User
settings** dialog. The API sends the token in the `X-API-KEY` header.

Keep the token out of source code, command arguments, notebooks, and chat. The
credential rule is in [AGENTS.md](AGENTS.md#code-conventions--common-patterns).
Use an operating-system secret store or an approved secret manager. For local
maintainer work, `.env` and `.grit42-api-token` are excluded by the repository
`.gitignore`, but they must still remain machine-local.

This Python example reads the token from `GRIT42_API_KEY` and writes the
complete API response to a local JSON file:

```python
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

query_id = 10321464
raw_dir = Path("tmp/grit42-queries")
raw_dir.mkdir(parents=True, exist_ok=True)
raw_path = raw_dir / f"grit42-subject-query-{query_id}.json"
request = Request(
    f"https://vict3r.upf.edu/api/v1/subject_queries/{query_id}",
    headers={
        "Accept": "application/json",
        "X-API-KEY": os.environ["GRIT42_API_KEY"],
    },
)

with urlopen(request, timeout=30) as response:
    saved_query = json.load(response)

if not isinstance(saved_query, dict):
    raise TypeError("The saved-query API response must be a JSON object")

raw_path.write_text(
    json.dumps(saved_query, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"Saved query definition to {raw_path}")
```

This request downloads the query definition only. It does not download the
SEND records that match the query. Treat the JSON file as project data. Keep it
under an ignored local path, and do not commit or share it.

## 3. Understand the two source formats

An advanced query contains `columns` and `condition_tree`. The following
example selects rats and requires a non-empty body-weight study identifier:

```json
{
  "id": 10321464,
  "columns": {
    "DM": ["species"],
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

A form query contains `query_type: "form"` and `form_values`:

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

Do not rebuild either JSON format by hand. Keep the complete record returned by
Grit42. The importer validates the fields and stores a hash of that record so
the query origin can be checked later.

## 4. Import the downloaded query

Load the complete JSON file and import it:

```python
import json
from pathlib import Path

import vcgen_query as vq

raw_path = Path("tmp/grit42-queries/grit42-subject-query-10321464.json")
saved_query = json.loads(raw_path.read_text(encoding="utf-8"))

query = vq.import_grit42_subject_query(saved_query)

print(query.query_id)
print(query.import_provenance.source_contract)
print(query.import_provenance.source_payload_sha256)
```

To send the query to the VCG Service, serialize `query.to_dict()` as JSON or
YAML. The service does not accept the raw Grit42 record; see the
[request contract](../../docs/service/README.md).

Use `import_grit42_subject_query()` for the raw VICT3R response. Do not pass
that response to `load_subject_query()`, which reads the local
`vcgen.subject_query.v1` format, not a VICT3R API record.

The imported query selects subjects only. Its membership is the Grit42 parity
checkpoint. Endpoint selection is a later stage:
`vq.CohortQuery.compose(query, ["BW_BW", "LB_HCT_BLOOD"])`. Source selectors,
units, specimen rules, and display facts come from the locked endpoint
catalogue (see the
[data README](src/vcgen_query/data/README.md#not-in-this-package)). Predicate
endpoint references do not become outputs unless they are also in the requested set.

## 5. Run the query against local SEND data

Run the imported query with `evaluate_membership()` and `execute_query()`, as
the [README](README.md#getting-started) shows. The
[README](README.md#returned-data-structures) describes the membership and the
result tables.

Local execution reads the local SEND source. It does not contact the VICT3R
API. The local result can differ from the current VICT3R result when the local
source has a different database revision or contains only part of the source
data.

## Matching rules

The importer stops with an error on an unsupported field, operator, value
type, or malformed value. The matching rules of the imported query are in
[How subject filters work](README.md#how-subject-filters-work).
