"""Source vocabulary for the top-level population fields of a subject query.

Each choice is a value that the matching derived query field can select:
``species``, ``strain``, ``route``, ``dose_frequency``,
``planned_dose_duration_days``, ``study_start_year`` and
``baseline_body_weight_g``. Values come from the derived source views, in
descending order of subject count. Routes and dose frequencies are the
canonical values of the reviewed route and frequency aliases; a source value
without one is not offered. The result contains no counts or identifiers.
"""

from __future__ import annotations

import math
from typing import Any

from .duckdb import table_columns
from .frequency_aliases import load_frequency_aliases
from .route_aliases import load_route_aliases
from .set_execution import study_start_year_sql

# Rounding step of the suggested pre-dose body weight bounds.
_WEIGHT_STEP_G = 10
# The bounds cover the central 98% of subjects, so no single record sets them.
_WEIGHT_QUANTILES = (0.01, 0.99)


def _range(minimum: Any, maximum: Any) -> dict[str, int] | None:
    if minimum is None or maximum is None:
        return None
    return {"minimum": int(minimum), "maximum": int(maximum)}


def _values_by_subject_count(con: Any, relation: str, column: str) -> list[str]:
    """Distinct ``column`` values of ``relation``, by subject count, most common first."""

    return [value for (value,) in con.execute(
        f"SELECT {column} FROM {relation} GROUP BY {column} ORDER BY count(*) DESC, {column}"
    ).fetchall()]


def population_vocabulary(con: Any) -> dict[str, Any]:
    """Return the choices and ranges for the top-level population fields."""

    rows = con.execute(
        "SELECT SPECIES, STRAIN, count(*) AS subjects FROM subject_demographics "
        "WHERE SPECIES IS NOT NULL AND trim(SPECIES) <> '' "
        "GROUP BY SPECIES, STRAIN ORDER BY SPECIES, subjects DESC, STRAIN"
    ).fetchall()
    totals: dict[str, int] = {}
    strains: dict[str, list[str]] = {}
    for species, strain, subjects in rows:
        totals[species] = totals.get(species, 0) + subjects
        if strain is not None and strain.strip():
            strains.setdefault(species, []).append(strain)
    species = [
        {"value": name, "strains": strains.get(name, [])}
        for name in sorted(totals, key=lambda name: (-totals[name], name))
    ]

    # Whole-day planned durations only: the filter compares numbers, and the form offers days.
    durations = [int(value) for (value,) in con.execute(
        "SELECT planned_dose_duration_days FROM subject_exposure_summary "
        "WHERE planned_dose_duration_days IS NOT NULL AND isfinite(planned_dose_duration_days) "
        "AND planned_dose_duration_days > 0 AND planned_dose_duration_days = floor(planned_dose_duration_days) "
        "GROUP BY planned_dose_duration_days ORDER BY count(*) DESC, planned_dose_duration_days"
    ).fetchall()]

    years = None
    year_rows = study_start_year_sql(table_columns(con, "dm"))
    if year_rows is not None:
        years = _range(*con.execute(f"SELECT min(actual), max(actual) FROM ({year_rows})").fetchone())

    weight = None
    if table_columns(con, "subject_baseline_body_weight"):
        low, high = con.execute(
            "SELECT quantile_cont(baseline_body_weight_g, ?), quantile_cont(baseline_body_weight_g, ?) "
            "FROM subject_baseline_body_weight WHERE isfinite(baseline_body_weight_g)",
            list(_WEIGHT_QUANTILES),
        ).fetchone()
        bounds = _range(
            None if low is None else math.floor(low / _WEIGHT_STEP_G) * _WEIGHT_STEP_G,
            None if high is None else math.ceil(high / _WEIGHT_STEP_G) * _WEIGHT_STEP_G,
        )
        weight = None if bounds is None else {**bounds, "unit": "g"}

    return {
        "species": species,
        # Canonical values, as the route and dose frequency filters resolve them.
        "routes": _values_by_subject_count(
            con,
            load_route_aliases().subject_routes_sql(
                "subject_exposure_summary", "route_method_values", "STUDYID", "USUBJID"
            ),
            "route",
        ),
        "dose_frequencies": _values_by_subject_count(
            con,
            load_frequency_aliases().subject_frequencies_sql(
                "subject_exposure_summary", "dose_frequency_values", "STUDYID", "USUBJID"
            ),
            "frequency",
        ),
        "planned_dose_duration_days": durations,
        "study_start_year": years,
        "baseline_body_weight_g": weight,
    }


__all__ = ["population_vocabulary"]
