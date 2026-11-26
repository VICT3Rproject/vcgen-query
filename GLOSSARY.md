# Query

Domain terms of `vcgen-query`: the selection of animals from SEND data and the
measurements that a query returns. Use these names in code, docs, and reviews.
A caller of the package uses these terms unchanged.

## Source data

**SEND**: the Standard for Exchange of Nonclinical Data. SEND stores study data
in named tables, called **domains**: `DM` holds subject information, `BW` body
weight, `LB` laboratory results, `TS` study summaries.

**Source**: the SEND data of one or more studies that a query reads.
_Avoid_: dataset.

**Subject**: one animal in a study of a source.
_Avoid_: patient, individual.

**Grit42**: the VICT3R system that stores SEND data and keeps saved subject
queries.

**UCUM**: the Unified Code for Units of Measure. It identifies units and the
valid conversions between them.

## Queries

**Subject query**: the Grit42-compatible predicate document that decides
membership only.
_Avoid_: cohort for the selection.

**Predicate**: the filter rules of a subject query.

**Membership**: the subjects of a source that match a subject query.

**Cohort query**: the subject query plus the later row-level stages.

**Observation**: one recorded measurement of one subject.
_Avoid_: endpoint for one value.

## Endpoints

**Endpoint**: a canonical measurement, for example `LB_ALT`.
_Avoid_: endpoint for a SEND column such as `LB.LBSTRESN` (say "column").

**Endpoint catalogue**: the locked list of endpoints, with the source rows and
the canonical unit of each endpoint.
_Avoid_: bare "catalogue".

**Panel**: a specimen panel from the endpoint catalogue. Never written by hand.

**Catalogue lock**: the record that names the md5 of an endpoint catalogue and
of its data decisions. Only the files that it names are read.

**Data decision**: a reviewed per-study correction of the data of one version
of a source. A source without a reviewed set has no data decisions.
_Avoid_: fix, override.
