"""SQL identifier helpers shared by the source validation and the DuckDB adapter."""

from __future__ import annotations

from typing import Any


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def table_columns(con: Any, table: str) -> tuple[str, ...]:
    """Return the column names of a table or view, or ``()`` if absent.

    DuckDB raises a binder/catalog exception (subclass varies by version) when
    the relation is missing, so the existence probe is intentionally broad.
    """

    try:
        cursor = con.execute(f"SELECT * FROM {quote_ident(table)} LIMIT 0")
    except Exception:  # noqa: BLE001 - missing relation probe, see docstring
        return ()
    return tuple(item[0] for item in cursor.description)
