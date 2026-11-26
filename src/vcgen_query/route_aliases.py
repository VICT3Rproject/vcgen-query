"""Reviewed delivery routes: EXROUTE and EXMETHOD pairs to one canonical route.

The archive records one delivery mechanism with many spellings ("ORAL GAVAGE",
"Oral Gavage", and "ORAL" with EXMETHOD "GAVAGE"). The alias table
(``vcgen.route_aliases.v1``) ships with this package and maps each reviewed
source pair to one canonical route. The ``route`` derived field and the
population route choices resolve through it. A pair that is not in the table
maps to no route. The table hash is part of the predicate fingerprint of each
query that uses ``route``.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from .alias_table import (
    AliasTable,
    alias_table_from_mapping,
    canonical_case_sql,
    load_alias_table,
    normalize_source_text,
    normalized_source_sql,
)
from .send_columns import ROUTE_PARAMETER_CODE, TS_PARAMETER_CODE, TS_VALUE

ROUTE_ALIASES_CONTRACT = "vcgen.route_aliases.v1"
ROUTE_ALIASES_FILENAME = "route_aliases.ex.v1.yaml"
# Separates the normalized route and method in the SQL lookup key. It contains no quote.
_KEY_SEPARATOR = "\x1f"

RouteKey = tuple[str, str | None]


class RouteAliases(AliasTable[RouteKey]):
    """The reviewed route table of one query execution."""

    def canonical(self, route: Any, method: Any) -> str | None:
        normalized = normalize_source_text(route)
        if normalized is None:
            return None
        return self.aliases.get((normalized, normalize_source_text(method)))

    def canonical_sql(self, route_expr: str, method_expr: str) -> str:
        """A SQL expression that gives the canonical route of one EX row, or NULL."""

        key = (
            f"({normalized_source_sql(route_expr)} || '{_KEY_SEPARATOR}' "
            f"|| coalesce({normalized_source_sql(method_expr)}, ''))"
        )
        cases = {
            route + _KEY_SEPARATOR + (method or ""): canonical
            for (route, method), canonical in sorted(self.aliases.items(), key=lambda item: (item[0][0], item[0][1] or ""))
        }
        return canonical_case_sql(key, cases)

    def subject_routes_sql(self, view: str, column: str, study: str, subject: str) -> str:
        """A relation of (study, subject, route): the canonical routes of each subject.

        ``column`` is a list of ``{route, method}`` structs. The lookup runs once per
        distinct source pair, not once per EX row, and joins back to the subjects.
        Pairs without a route are left out.
        """

        return (
            "(WITH pairs AS ("
            f"SELECT DISTINCT rows.{study} AS study, rows.{subject} AS subject, "
            "candidate.value.route AS route, candidate.value.method AS method "
            f"FROM {view} rows CROSS JOIN UNNEST(rows.{column}) AS candidate(value)), "
            f"lookup AS (SELECT route, method, {self.canonical_sql('route', 'method')} AS canonical "
            "FROM (SELECT DISTINCT route, method FROM pairs)) "
            "SELECT DISTINCT pairs.study, pairs.subject, lookup.canonical AS route FROM pairs JOIN lookup "
            "ON pairs.route IS NOT DISTINCT FROM lookup.route AND pairs.method IS NOT DISTINCT FROM lookup.method "
            "WHERE lookup.canonical IS NOT NULL)"
        )


def _pair(raw: Mapping[str, Any], location: str) -> RouteKey:
    route = raw.get("exroute")
    method = raw.get("exmethod")
    if not isinstance(route, str) or (method is not None and not isinstance(method, str)):
        raise RuntimeError(f"{location} needs a string exroute and a string or null exmethod")
    if normalize_source_text(route) != route or (method is not None and normalize_source_text(method) != method):
        raise RuntimeError(f"{location} must give exroute and exmethod in normalized (upper-case) form")
    return route, method


def route_aliases_from_mapping(payload: Mapping[str, Any], *, sha256: str) -> RouteAliases:
    """Validate an alias table: each source pair maps to one route or is unmapped."""

    return alias_table_from_mapping(
        RouteAliases, payload, sha256=sha256, contract=ROUTE_ALIASES_CONTRACT, label="route", group="routes",
        value_field="route", id_prefix="ROUTE", source_fields=("exroute", "exmethod"), source_noun="pair", key=_pair,
    )


@lru_cache(maxsize=1)
def load_route_aliases() -> RouteAliases:
    return load_alias_table(ROUTE_ALIASES_FILENAME, route_aliases_from_mapping)


def canonical_route(value: str) -> str:
    """The canonical route that a query value spells (case and spacing aside)."""

    return load_route_aliases().require_canonical(value)


def route_alias_inventory(con: Any, aliases: RouteAliases | None = None) -> list[dict[str, Any]]:
    """Every normalized route and method pair of EX and every TS ROUTE value with its canonical route and counts.

    A TS ROUTE value is a pair without a method. The counts of EX rows and of TS
    values are separate: ``ex_subjects`` and ``ex_studies`` count the EX rows of
    the pair; ``ts_subjects`` and ``ts_studies`` count the DM subjects and the
    studies of the TS value. ``status`` is ``mapped``, ``unmapped`` (reviewed, no
    route), ``unknown`` (not in the table) or ``missing_route``. Use it to review
    the table when the source changes. The rows contain counts only, no
    identifiers.
    """

    table = load_route_aliases() if aliases is None else aliases
    rows = con.execute(
        "SELECT route, method, "
        "count(DISTINCT (STUDYID, USUBJID)) FILTER (WHERE source = 'EX') AS ex_subjects, "
        "count(DISTINCT STUDYID) FILTER (WHERE source = 'EX') AS ex_studies, "
        "count(DISTINCT (STUDYID, USUBJID)) FILTER (WHERE source = 'TS') AS ts_subjects, "
        "count(DISTINCT STUDYID) FILTER (WHERE source = 'TS') AS ts_studies, "
        "list(DISTINCT spelling ORDER BY spelling) AS spellings FROM ("
        f"SELECT 'EX' AS source, STUDYID, USUBJID, {normalized_source_sql('EXROUTE')} AS route, "
        f"{normalized_source_sql('EXMETHOD')} AS method, "
        "coalesce(cast(EXROUTE AS VARCHAR), '-') || ' | ' || coalesce(nullif(trim(cast(EXMETHOD AS VARCHAR)), ''), '-') "
        "AS spelling FROM ex "
        f"UNION ALL SELECT 'TS', ts.STUDYID, dm.USUBJID, {normalized_source_sql(TS_VALUE)}, CAST(NULL AS VARCHAR), "
        f"cast({TS_VALUE} AS VARCHAR) || ' | -' FROM ts JOIN dm USING (STUDYID) "
        f"WHERE upper({TS_PARAMETER_CODE}) = '{ROUTE_PARAMETER_CODE}' AND {normalized_source_sql(TS_VALUE)} IS NOT NULL"
        ") GROUP BY route, method ORDER BY ex_subjects + ts_subjects DESC, route NULLS LAST, method NULLS FIRST"
    ).fetchall()
    inventory = []
    for route, method, ex_subjects, ex_studies, ts_subjects, ts_studies, spellings in rows:
        canonical = table.canonical(route, method)
        if route is None:
            status = "missing_route"
        elif canonical is not None:
            status = "mapped"
        elif (route, method) in table.unmapped:
            status = "unmapped"
        else:
            status = "unknown"
        inventory.append({
            "exroute": route, "exmethod": method, "route": canonical, "status": status,
            "ex_subjects": int(ex_subjects), "ex_studies": int(ex_studies),
            "ts_subjects": int(ts_subjects), "ts_studies": int(ts_studies), "spellings": list(spellings),
        })
    return inventory


__all__ = [
    "ROUTE_ALIASES_CONTRACT",
    "ROUTE_ALIASES_FILENAME",
    "RouteAliases",
    "canonical_route",
    "load_route_aliases",
    "route_alias_inventory",
    "route_aliases_from_mapping",
]
