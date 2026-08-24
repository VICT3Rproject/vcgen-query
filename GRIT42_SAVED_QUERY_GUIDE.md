# Import a saved Grit42 query

This guide shows how to retrieve a saved Grit42 subject query, import it into
`vcgen-query`, and run it against a local SEND source.

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

## 2. Retrieve the saved-query JSON

Read the query through the authenticated API:

```text
GET https://vict3r.upf.edu/api/v1/subject_queries/{id}
```

Users manage API tokens in the **API token** tab of the Grit42 **User
settings** dialog. The API sends the token in the `X-API-KEY` header.

Keep the token out of source code, command arguments, notebooks, and chat. Use
an operating-system secret store or an approved secret manager. For local
maintainer work, `.env` and `.grit42-api-token` are excluded by the repository
`.gitignore`, but they must still remain machine-local.

This Python example reads the token from `GRIT42_API_KEY`:

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

## 4. Import the query

Save the API response to a local JSON file, then import it:

```python
import json
from pathlib import Path

import vcgen_query as vq

raw_path = Path("grit42-subject-query-10321464.json")
saved_query = json.loads(raw_path.read_text(encoding="utf-8"))

query = vq.import_grit42_subject_query(saved_query)

print(query.query_id)
print(query.import_provenance.source_contract)
print(query.import_provenance.source_payload_sha256)
```

The imported filter selects subjects. It does not select the measurements used
by a model. Add an `observation_contract` when the local query must return
specific endpoints, units, specimens, or study days.

## 5. Execute and inspect the query

Use the same frozen SEND source when results need to be reproduced or compared:

```python
explanation = vq.explain_query(
    "/data/frozen-wider-source.zip",
    query,
    include_subject_keys=True,
)

print(explanation["matched_subject_count"])
print(explanation["membership_hash"])

result = vq.execute_query("/data/frozen-wider-source.zip", query)
print(result.subjects.head())
print(result.receipt)
```

`explain_query()` reports how many subjects match each rule or group.
`execute_query()` returns the selected subjects and any measurements requested
by the observation contract.

## Matching rules to remember

- Rules in the same group and SEND domain must match the same source row.
- Rules in separate nested groups may match different rows for the same
  subject.
- Rules from different domains are combined at subject level.
- A matching row in a study-level domain applies to all subjects in that study.
- Unsupported fields, operators, value types, or malformed values stop the
  import with an error.

See the [main README](README.md#how-subject-filters-work) for the plain-language
filter rules and unit behavior.
