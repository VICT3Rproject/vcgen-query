"""The stored Python-derived tables follow the active endpoint catalogue.

Failure mode: the identity of the stored subject context and pre-study tables is
computed once per process, so a catalogue named by ``VCGEN_ENDPOINT_CATALOGUE``
after the first call leaves stale tables accepted as current.
"""

from vcgen_query import duckdb as query_duckdb


def test_derived_identity_changes_with_the_catalogue_identity(monkeypatch) -> None:
    first = query_duckdb._python_derived_identity()
    assert query_duckdb._python_derived_identity() == first
    monkeypatch.setattr(
        "vcgen_query.endpoint_catalogue.endpoint_catalogue_identity", lambda: {"sha256": "another catalogue"}
    )
    assert query_duckdb._python_derived_identity() != first
