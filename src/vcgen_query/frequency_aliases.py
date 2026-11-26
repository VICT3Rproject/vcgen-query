"""Reviewed dose frequencies: source spellings to one canonical frequency.

The archive records one dosing frequency with several spellings ("QD",
"QD – Once daily", and the TS spellings "DAILY" and "Once daily"). The alias
table (``vcgen.dose_frequency_aliases.v1``) ships with this package and maps
each reviewed spelling to one canonical frequency. The ``dose_frequency``
derived field and the population frequency choices resolve through it. A
spelling that is not in the table maps to no frequency. The table hash is part
of the predicate fingerprint of each query that uses ``dose_frequency``.
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
from .send_columns import DOSE_FREQUENCY_PARAMETER_CODE, TS_PARAMETER_CODE, TS_VALUE

FREQUENCY_ALIASES_CONTRACT = "vcgen.dose_frequency_aliases.v1"
FREQUENCY_ALIASES_FILENAME = "dose_frequency_aliases.v1.yaml"


class FrequencyAliases(AliasTable[str]):
    """The reviewed frequency table of one query execution."""

    def canonical(self, value: Any) -> str | None:
        normalized = normalize_source_text(value)
        return None if normalized is None else self.aliases.get(normalized)

    def canonical_sql(self, expr: str) -> str:
        """A SQL expression that gives the canonical frequency of one source value, or NULL."""

        return canonical_case_sql(normalized_source_sql(expr), dict(sorted(self.aliases.items())))

    def subject_frequencies_sql(self, view: str, column: str, study: str, subject: str) -> str:
        """A relation of (study, subject, frequency): the canonical frequencies of each subject.

        ``column`` is a list of source spellings. Spellings without a frequency are left out.
        """

        return (
            "(WITH spellings AS ("
            f"SELECT DISTINCT rows.{study} AS study, rows.{subject} AS subject, "
            "cast(candidate.value AS VARCHAR) AS spelling "
            f"FROM {view} rows CROSS JOIN UNNEST(rows.{column}) AS candidate(value)), "
            f"lookup AS (SELECT spelling, {self.canonical_sql('spelling')} AS canonical "
            "FROM (SELECT DISTINCT spelling FROM spellings)) "
            "SELECT DISTINCT spellings.study, spellings.subject, lookup.canonical AS frequency "
            "FROM spellings JOIN lookup ON spellings.spelling IS NOT DISTINCT FROM lookup.spelling "
            "WHERE lookup.canonical IS NOT NULL)"
        )


def _spelling(raw: Mapping[str, Any], location: str) -> str:
    value = raw.get("value")
    if not isinstance(value, str) or normalize_source_text(value) != value:
        raise RuntimeError(f"{location}.value must be a normalized (upper-case) spelling")
    return value


def _frequency_aliases_from_mapping(payload: Mapping[str, Any], *, sha256: str) -> FrequencyAliases:
    return alias_table_from_mapping(
        FrequencyAliases, payload, sha256=sha256, contract=FREQUENCY_ALIASES_CONTRACT, label="dose frequency",
        group="frequencies", value_field="frequency", id_prefix="FREQ", source_fields=("value",),
        source_noun="spelling", key=_spelling,
    )


@lru_cache(maxsize=1)
def load_frequency_aliases() -> FrequencyAliases:
    return load_alias_table(FREQUENCY_ALIASES_FILENAME, _frequency_aliases_from_mapping)


def canonical_frequency(value: str) -> str:
    """The canonical frequency that a query value spells (case and spacing aside)."""

    return load_frequency_aliases().require_canonical(value)


def frequency_alias_inventory(con: Any) -> list[dict[str, Any]]:
    """Every normalized EXDOSFRQ and TS PDOSFRQ spelling with its canonical frequency and counts.

    ``status`` is ``mapped``, ``unmapped`` (reviewed, no frequency) or
    ``unknown`` (not in the table). Use it to review the table when the source
    changes. The rows contain counts only, no identifiers.
    """

    table = load_frequency_aliases()
    rows = con.execute(
        "SELECT spelling, count(DISTINCT (STUDYID, USUBJID)) FILTER (WHERE source = 'EX') AS ex_subjects, "
        "count(DISTINCT STUDYID) FILTER (WHERE source = 'EX') AS ex_studies, "
        "count(DISTINCT (STUDYID, USUBJID)) FILTER (WHERE source = 'TS') AS ts_subjects, "
        "count(DISTINCT STUDYID) FILTER (WHERE source = 'TS') AS ts_studies FROM ("
        f"SELECT 'EX' AS source, STUDYID, USUBJID, {normalized_source_sql('EXDOSFRQ')} AS spelling FROM ex "
        f"UNION ALL SELECT 'TS', ts.STUDYID, dm.USUBJID, {normalized_source_sql(TS_VALUE)} FROM ts "
        f"JOIN dm USING (STUDYID) WHERE upper({TS_PARAMETER_CODE}) = '{DOSE_FREQUENCY_PARAMETER_CODE}'"
        ") WHERE spelling IS NOT NULL GROUP BY spelling ORDER BY ex_subjects + ts_subjects DESC, spelling"
    ).fetchall()
    inventory = []
    for spelling, ex_subjects, ex_studies, ts_subjects, ts_studies in rows:
        canonical = table.canonical(spelling)
        status = "mapped" if canonical is not None else "unmapped" if spelling in table.unmapped else "unknown"
        inventory.append({
            "value": spelling, "frequency": canonical, "status": status,
            "ex_subjects": int(ex_subjects), "ex_studies": int(ex_studies),
            "ts_subjects": int(ts_subjects), "ts_studies": int(ts_studies),
        })
    return inventory


__all__ = [
    "FrequencyAliases",
    "canonical_frequency",
    "frequency_alias_inventory",
    "load_frequency_aliases",
]
