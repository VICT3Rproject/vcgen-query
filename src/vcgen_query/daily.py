"""Deterministic daily endpoint projection for query results."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import pandas as pd

from .unit_catalogue import catalogue_identity

if TYPE_CHECKING:
    from .result import QueryResult


class DailyPolicy(TypedDict):
    on_conflict: Literal["raise", "report"]
    unit_catalogue: dict[str, str]
    endpoint_conversions: dict[str, dict[str, Any] | None]


class DailyReceipt(TypedDict, total=False):
    query_id: str
    source_identity: str | None
    observation_fingerprint: str
    policy: DailyPolicy
    daily_projection_fingerprint: str
    conversion_count: int
    unsupported_unit_count: int
    duplicate_count: int
    conflict_count: int
    endpoint_conversions: dict[str, dict[str, Any] | None]


class DailyProjectionConflictError(ValueError):
    """Raised when a daily endpoint cell has non-equivalent observations."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(f"conflicting observations in {len(conflicts)} daily endpoint cell(s)")


class DailyProjectionUnitError(ValueError):
    """Raised when a finite source value produces a non-finite canonical value."""

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = tuple(failures)
        super().__init__(
            f"unit conversion produced a non-finite value in {len(failures)} observation(s)"
        )


@dataclass(frozen=True)
class DailyFrame:
    """Three pandas DataFrame views of the same subject-day observations.

    ``values`` is the primary wide numeric table. Its keys are ``study_id``,
    ``subject_id``, and ``study_day``. An endpoint cell contains the one numeric
    value selected after unit conversion and duplicate resolution. ``NaN`` in
    that cell means that no numeric value was selected for that endpoint on
    that day. The table is sparse because SEND endpoints use different
    collection schedules. Every row in ``values`` has at least one selected
    numeric endpoint, although pandas can hide that endpoint behind ``...`` in
    a wide display.

    ``states`` has the same keys and endpoint columns, but its endpoint cells
    contain source-record status labels instead of measurements. For example,
    ``numeric_observed`` means that a matching source record had a numeric
    result. ``qualitative_observed``, ``censored_observed``,
    ``explicitly_failed``, and ``silently_absent`` describe matching records
    that did not supply a usable number. Multiple comma-separated labels mean
    that multiple source records mapped to the same daily cell. ``NaN`` means
    that no matching source record mapped to that cell.

    A populated ``states`` cell can therefore correspond to ``NaN`` in
    ``values``. Use ``cell_audit`` to see source values, units, conversion, and
    duplicate or conflict resolution for that cell. A state-only subject-day
    can occur in ``states`` without a row in ``values``.
    """

    values: pd.DataFrame
    states: pd.DataFrame
    cell_audit: pd.DataFrame
    receipt: DailyReceipt
    warnings: tuple[str, ...] = ()



_KEY_COLUMNS = ("study_id", "subject_id", "study_day")
_AUDIT_COLUMNS = (
    *_KEY_COLUMNS,
    "endpoint_name",
    "source_value",
    "source_unit",
    "canonical_value",
    "canonical_unit",
    "source_values",
    "source_units",
    "canonical_values",
    "conversion_status",
    "conversion_rule",
    "selected",
    "resolution",
)


def to_daily_frame(
    result: QueryResult,
    *,
    on_conflict: Literal["raise", "report"] = "raise",
) -> DailyFrame:
    """Project a long observation DataFrame into deterministic daily DataFrames."""
    if on_conflict not in {"raise", "report"}:
        raise ValueError("on_conflict must be 'raise' or 'report'")

    observations = result.observations
    endpoints = list(result.endpoint_names)
    if not endpoints and not observations.empty and "endpoint_name" in observations:
        endpoints = observations["endpoint_name"].dropna().astype(str).drop_duplicates().tolist()
    reserved_names = sorted(set(endpoints) & set(_KEY_COLUMNS))
    if reserved_names:
        raise ValueError(f"endpoint names cannot use daily key columns: {reserved_names}.")

    facts = {fact.endpoint_name: fact for fact in result.endpoint_facts}
    if observations.empty:
        frame = observations.copy()
    else:
        frame = observations.loc[
            observations["study_day"].notna() & observations["endpoint_name"].notna()
        ].copy()
        frame["endpoint_name"] = frame["endpoint_name"].astype(str)
        frame = _apply_conversions(frame, facts)

    selected, conflicts, duplicate_count = _resolve_cells(frame)
    if conflicts and on_conflict == "raise":
        raise DailyProjectionConflictError(conflicts)

    values = _pivot_daily(selected, "canonical_value", endpoints)
    if frame.empty:
        states = _empty_wide(endpoints)
    else:
        state_rows = (
            frame.assign(_result_state=frame["result_state"].astype(str))
            .groupby([*_KEY_COLUMNS, "endpoint_name"], sort=False, dropna=False)["_result_state"]
            .agg(",".join)
            .reset_index()
        )
        states = _pivot_daily(state_rows, "_result_state", endpoints)
    audit = _build_audit(frame, selected, conflicts)

    policy: DailyPolicy = {
        "on_conflict": on_conflict,
        "unit_catalogue": catalogue_identity(),
        "endpoint_conversions": {name: _conversion_dict(facts.get(name)) for name in endpoints},
    }
    policy_json = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    receipt: DailyReceipt = dict(result.receipt)
    for key in ("query_id", "source_identity", "observation_fingerprint"):
        if key not in receipt and key in result.metadata:
            receipt[key] = result.metadata[key]
    receipt.update(
        {
            "policy": policy,
            "daily_projection_fingerprint": hashlib.sha256(policy_json.encode("utf-8")).hexdigest(),
            "conversion_count": (
                0 if frame.empty else int(frame["conversion_status"].eq("converted").sum())
            ),
            "unsupported_unit_count": (
                0
                if frame.empty
                else int(frame["conversion_status"].eq("unsupported_source_unit").sum())
            ),
            "duplicate_count": duplicate_count,
            "conflict_count": len(conflicts),
            "endpoint_conversions": policy["endpoint_conversions"],
        }
    )
    return DailyFrame(
        values=values,
        states=states,
        cell_audit=audit,
        receipt=receipt,
        warnings=result.warnings,
    )


_CELL_COLUMNS = (*_KEY_COLUMNS, "endpoint_name")


def _empty_wide(endpoints: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=[*_KEY_COLUMNS, *endpoints])


def _pivot_daily(
    rows: pd.DataFrame,
    value_column: str,
    endpoints: list[str],
) -> pd.DataFrame:
    if rows.empty:
        return _empty_wide(endpoints)
    wide = (
        rows.pivot(
            index=list(_KEY_COLUMNS),
            columns="endpoint_name",
            values=value_column,
        )
        .reindex(columns=endpoints)
        .reset_index()
        .sort_values(list(_KEY_COLUMNS), kind="stable")
        .reset_index(drop=True)
    )
    wide.columns.name = None
    return wide


def _apply_conversions(
    observations: pd.DataFrame,
    facts: dict[str, Any],
) -> pd.DataFrame:
    """Apply endpoint conversions with vectorized pandas column operations."""
    frame = observations.copy()
    numeric = pd.to_numeric(frame["numeric_value"], errors="coerce")
    numeric = numeric.mask(numeric.isin((float("-inf"), float("inf"))))

    frame["_source_unit"] = frame["unit"]
    frame["canonical_unit"] = frame["endpoint_name"].map(
        {name: _canonical_unit(fact) for name, fact in facts.items()}
    )

    rule_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    scale_lookup: dict[tuple[str, str], float] = {}
    offset_lookup: dict[tuple[str, str], float] = {}
    for endpoint, fact in facts.items():
        target = _canonical_unit(fact)
        for rule in getattr(fact, "compiled_unit_rules", ()):
            source = str(_rule_get(rule, "source_unit"))
            key = (endpoint, source)
            scale = float(_rule_get(rule, "scale"))
            offset = float(_rule_get(rule, "offset", 0))
            rule_metadata[key] = {
                "source_unit": source,
                "source_ucum": str(_rule_get(rule, "source_ucum")),
                "target_unit": target,
                "scale": scale,
                "offset": offset,
                "catalogue_id": str(_rule_get(rule, "catalogue_id")),
                "catalogue_sha256": str(_rule_get(rule, "catalogue_sha256")),
            }
            scale_lookup[key] = scale
            offset_lookup[key] = offset

    rule_keys = pd.MultiIndex.from_arrays([frame["endpoint_name"], frame["_source_unit"]])
    scales = pd.Series(rule_keys.map(scale_lookup), index=frame.index, dtype="float64")
    offsets = pd.Series(rule_keys.map(offset_lookup), index=frame.index, dtype="float64")
    rules = pd.Series(rule_keys.map(rule_metadata), index=frame.index, dtype="object")

    finite = numeric.notna()
    unconverted = finite & frame["canonical_unit"].isna()
    configured_identity = finite & scales.eq(1.0) & offsets.eq(0.0)
    identity = unconverted | configured_identity
    converted = finite & frame["canonical_unit"].notna() & scales.notna() & ~configured_identity
    unsupported = finite & frame["canonical_unit"].notna() & scales.isna()

    canonical = pd.Series(float("nan"), index=frame.index, dtype="float64")
    canonical.loc[identity] = numeric.loc[identity]
    canonical.loc[converted] = (
        numeric.loc[converted] * scales.loc[converted] + offsets.loc[converted]
    )
    non_finite = converted & canonical.isin((float("-inf"), float("inf")))
    if non_finite.any():
        failures = [
            {
                "endpoint_name": str(row.endpoint_name),
                "source_unit": row.unit,
                "source_value": float(row.numeric_value),
                "target_unit": row.canonical_unit,
            }
            for row in frame.loc[
                non_finite,
                ["endpoint_name", "unit", "numeric_value", "canonical_unit"],
            ].itertuples(index=False)
        ]
        raise DailyProjectionUnitError(failures)
    status = pd.Series("not_numeric", index=frame.index, dtype="object")
    status.loc[identity] = "identity"
    status.loc[converted] = "converted"
    status.loc[unsupported] = "unsupported_source_unit"

    frame["canonical_value"] = canonical
    frame["conversion_status"] = status
    frame["conversion_rule"] = rules.where(finite & scales.notna(), None)
    frame["_conversion_scale"] = 1.0
    frame.loc[converted, "_conversion_scale"] = scales.loc[converted]
    frame["_conversion_offset"] = 0.0
    frame.loc[converted, "_conversion_offset"] = offsets.loc[converted]
    frame["_effective_unit"] = frame["canonical_unit"].combine_first(frame["_source_unit"])
    return frame


def _resolve_cells(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, Any]], int]:
    """Select unique cells vectorially; inspect only duplicate cells in Python."""
    if frame.empty:
        return frame.copy(), [], 0
    usable = frame.loc[
        frame["canonical_value"].notna() & ~frame["excluded"].fillna(False).astype(bool)
    ].copy()
    if usable.empty:
        return usable, [], 0

    counts = usable.groupby(list(_CELL_COLUMNS), sort=False, dropna=False).size()
    cell_index = pd.MultiIndex.from_frame(usable[list(_CELL_COLUMNS)])
    single = usable.loc[cell_index.isin(counts.index[counts.eq(1)])]
    duplicate_rows = usable.loc[cell_index.isin(counts.index[counts.gt(1)])]
    selected_parts = [single]
    conflicts: list[dict[str, Any]] = []
    duplicate_count = 0

    duplicate_rows["_source_order_sort"] = duplicate_rows["source_order"].astype(str)
    for key, items in duplicate_rows.groupby(
        list(_CELL_COLUMNS),
        sort=False,
        dropna=False,
    ):
        signatures = {
            _decimal_signature(value, scale, offset, unit)
            for value, scale, offset, unit in items[
                [
                    "numeric_value",
                    "_conversion_scale",
                    "_conversion_offset",
                    "_effective_unit",
                ]
            ].itertuples(index=False, name=None)
        }
        if len(signatures) == 1:
            selected_parts.append(
                items.sort_values(
                    ["measurement_identity", "_source_order_sort"],
                    kind="stable",
                ).iloc[[0]]
            )
            duplicate_count += len(items) - 1
            continue
        key_values = key if isinstance(key, tuple) else (key,)
        conflict = dict(zip(_CELL_COLUMNS, key_values))
        conflict["observations"] = (
            items[["numeric_value", "unit", "canonical_value"]]
            .rename(
                columns={
                    "numeric_value": "source_value",
                    "unit": "source_unit",
                }
            )
            .to_dict(orient="records")
        )
        conflicts.append(conflict)

    selected = pd.concat(selected_parts, ignore_index=False)
    return (
        selected.sort_values(list(_CELL_COLUMNS), kind="stable"),
        conflicts,
        duplicate_count,
    )


def _decimal_signature(
    value: Any,
    scale: Any,
    offset: Any,
    unit: Any,
) -> tuple[Decimal, Any]:
    canonical = Decimal(str(value)) * Decimal(str(scale)) + Decimal(str(offset))
    return canonical, None if pd.isna(unit) else unit


def _build_audit(
    frame: pd.DataFrame,
    selected: pd.DataFrame,
    conflicts: list[dict[str, Any]],
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=_AUDIT_COLUMNS)

    audit = (
        frame.groupby(list(_CELL_COLUMNS), sort=False, dropna=False)
        .agg(
            source_values=("numeric_value", list),
            source_units=("unit", list),
            canonical_values=("canonical_value", list),
            conversion_status=("conversion_status", list),
            conversion_rule=("conversion_rule", list),
            canonical_unit=("canonical_unit", "first"),
        )
        .reset_index()
    )
    first = frame.drop_duplicates(list(_CELL_COLUMNS), keep="first")[
        [*_CELL_COLUMNS, "numeric_value", "unit"]
    ].rename(
        columns={
            "numeric_value": "_first_source_value",
            "unit": "_first_source_unit",
        }
    )
    chosen = selected[[*_CELL_COLUMNS, "numeric_value", "unit", "canonical_value"]].rename(
        columns={
            "numeric_value": "_selected_source_value",
            "unit": "_selected_source_unit",
            "canonical_value": "_selected_canonical_value",
        }
    )
    chosen["_selected"] = True
    usable_counts = (
        frame.loc[frame["canonical_value"].notna() & ~frame["excluded"].fillna(False).astype(bool)]
        .groupby(list(_CELL_COLUMNS), sort=False, dropna=False)
        .size()
        .rename("_usable_count")
        .reset_index()
    )
    audit = audit.merge(first, on=list(_CELL_COLUMNS), how="left").merge(
        chosen,
        on=list(_CELL_COLUMNS),
        how="left",
    )
    audit = audit.merge(usable_counts, on=list(_CELL_COLUMNS), how="left")
    audit["_selected"] = audit["_selected"].fillna(False).astype(bool)
    audit["_usable_count"] = audit["_usable_count"].fillna(0).astype(int)
    audit["source_value"] = audit["_selected_source_value"].where(
        audit["_selected"],
        audit["_first_source_value"],
    )
    audit["source_unit"] = audit["_selected_source_unit"].where(
        audit["_selected"],
        audit["_first_source_unit"],
    )
    audit["canonical_value"] = audit["_selected_canonical_value"]
    audit["selected"] = audit["_selected"]
    audit["resolution"] = "no_numeric_value"
    audit.loc[audit["_selected"] & audit["_usable_count"].eq(1), "resolution"] = "selected"
    audit.loc[
        audit["_selected"] & audit["_usable_count"].gt(1),
        "resolution",
    ] = "equivalent_duplicate_collapsed"

    if conflicts:
        conflict_index = pd.MultiIndex.from_tuples(
            [tuple(item[column] for column in _CELL_COLUMNS) for item in conflicts],
            names=_CELL_COLUMNS,
        )
        audit_index = pd.MultiIndex.from_frame(audit[list(_CELL_COLUMNS)])
        audit.loc[audit_index.isin(conflict_index), "resolution"] = "conflict_reported"

    return (
        audit.sort_values(list(_CELL_COLUMNS), kind="stable")
        .reindex(columns=_AUDIT_COLUMNS)
        .reset_index(drop=True)
    )


def _canonical_unit(fact: Any) -> str | None:
    return getattr(fact, "target_unit", None) if fact else None


def _conversion_dict(fact: Any) -> Any:
    if not fact:
        return None
    target = _canonical_unit(fact)
    rules = getattr(fact, "compiled_unit_rules", ())
    if not target:
        return None
    return {
        "target_unit": target,
        "unit_catalogue": catalogue_identity(),
        "rules": [
            rule.to_canonical_dict()
            if hasattr(rule, "to_canonical_dict")
            else dict(rule)
            for rule in rules
        ],
    }


def _rule_get(rule: Any, key: str, default: Any = None) -> Any:
    return rule.get(key, default) if isinstance(rule, dict) else getattr(rule, key, default)


__all__ = [
    "DailyFrame",
    "DailyPolicy",
    "DailyProjectionConflictError",
    "DailyProjectionUnitError",
    "DailyReceipt",
    "to_daily_frame",
]
