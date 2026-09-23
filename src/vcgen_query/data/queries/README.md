# Cohort-query v5 profiles

Every YAML file in this directory is a `vcgen.cohort_query.v5` document. Load
these files with `vcgen_query.load_query()` for local SEND execution, or upload
them to a compatible VCGen service.

See [`../README.md`](../README.md) for the complete package-data inventory and
the intended use of every bundled query document.

The current staging service supports the 48 requested endpoints in
`gurjanov_28d_generation_core.yaml` and the two endpoints in
`hosted_debug_wistar_han_28d.yaml`. The broader benchmark and historical files
also use valid v5 syntax, but they request endpoints outside the current model
protocol and are local analysis profiles, not staging examples.

The debug client initializes its endpoint selector from `requested_endpoints`.
It sends the selected ordered list both inside the v5 query and in the parallel
service request field.
