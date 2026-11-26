"""Long-format SEND observation extraction for a selected cohort.

Given a :class:`~vcgen_query.cohort.CohortSelection` and an
:class:`~vcgen_query.contract.ObservationContract`, :func:`extract_observations`
returns one row per parsed SEND measurement that matches a configured
endpoint, scoped to the selected subjects. This is the *query* output: parsed,
normalized observations in long form, as a DataFrame with ``OBSERVATION_COLUMNS``.

What is deliberately absent (it lives in the modelling layer, not the query
contract):

* single-table shaping (wide feature columns, one-cell-per-sample/day
  deduplication, duplicate-cell errors);
* temporal/terminal slice selection and coverage (presence) filtering;
* baseline attachment and endpoint-role validation;
* study splitting and outer-evaluation subject selection.

Each endpoint is matched by domain, test code (and optionally test name, source
unit, specimen, and subject sex). SEND parsing
(:mod:`vcgen_query.send_semantics`) classifies every matched cell into a result
state (``numeric_observed``, ``censored_observed``, ``qualitative_observed``,
``explicitly_failed``, ``silently_absent``) so downstream code can decide what
is numerically usable without re-parsing.

The extraction is columnar. The source rows arrive as Arrow batches. Each
semantic step (result parsing, endpoint matching and description, study day,
pool, baseline flag, source order, subject context) depends on a few columns
only, so the batch is grouped by the values of those columns, the SEND
semantics run once per distinct value tuple, and the observation table is
assembled with array takes. Cost grows with the number of distinct values, not
with the number of rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from .table_schema import OBSERVATIONS_TABLE
from .cohort import CohortSelection
from .contract import EndpointSpec
from .data_decisions import DataDecisions, load_data_decisions
from .endpoint_rows import (
    endpoint_identity_matches,
    endpoint_matches_row,
    endpoint_projection_columns,
    endpoint_specimen_matches,
    endpoints_by_test_code,
    row_category,
    identity_columns,
    row_pool_id,
    row_specimen_class,
    row_test_code,
)
from .duckdb import q, quote_ident, table_columns
from .query import StageWarning, TraceItem
from .query import trace_items as sorted_trace_items
from .send_columns import STUDY_ID, SUBJECT_ID
from .send_semantics import (
    baseline_flag_columns,
    day_columns,
    endpoint_key,
    first_numeric_day,
    is_explicit_baseline,
    measurement_context,
    measurement_identity,
    ParsedResult,
    parse_result,
    qualifier_columns,
    result_columns,
    source_order_key,
)


OBSERVATION_COLUMNS = tuple(column.name for column in OBSERVATIONS_TABLE.columns)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series([], dtype=object) for name in OBSERVATION_COLUMNS})


@dataclass(frozen=True)
class ObservationResult:
    """Long-format observations for a cohort plus a receipt.

    ``frame`` has one row per parsed SEND measurement matching a configured endpoint
    (``OBSERVATION_COLUMNS``). ``unit_inferred`` is true when the endpoint's implicit
    unit replaced a missing source unit; ``source_order`` is the source-order key
    of :func:`~vcgen_query.send_semantics.source_order_key`.
    """

    frame: pd.DataFrame = field(default_factory=_empty_frame)
    endpoint_names: tuple[str, ...] = ()
    warnings: tuple[StageWarning, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    # Rows that a data decision removed, by finding and study.
    decision_removals: dict[str, dict[str, int]] = field(default_factory=dict)
    # The removed rows themselves, by finding. Empty unless the caller asked for trace items.
    decision_removal_items: dict[str, tuple[TraceItem, ...]] = field(default_factory=dict)
    data_decisions: dict[str, Any] = field(default_factory=dict)
    # The (study, subject) keys that have at least one observation.
    observed_subjects: frozenset[tuple[str, str]] = frozenset()

    @property
    def record_count(self) -> int:
        return len(self.frame)

    def to_receipt(self) -> dict[str, Any]:
        frame = self.frame
        by_endpoint = frame["endpoint_name"].value_counts()
        by_state = frame["result_state"].value_counts()
        numeric = int((frame["numeric_value"].notna() & ~frame["excluded"].astype(bool)).sum())
        return {
            "record_count": self.record_count,
            "endpoint_names": list(self.endpoint_names),
            "records_by_endpoint": {str(key): int(by_endpoint[key]) for key in sorted(by_endpoint.index)},
            "records_by_result_state": {str(key): int(by_state[key]) for key in sorted(by_state.index)},
            "numeric_usable_count": numeric,
            "warnings": [warning.message for warning in self.warnings],
            "counts": dict(self.counts),
            "data_decisions": dict(self.data_decisions),
        }


def extract_observations(
    con: Any,
    selection: CohortSelection,
    endpoints: Sequence[EndpointSpec],
    *,
    data_decisions: DataDecisions | None = None,
    trace_items: bool = False,
) -> ObservationResult:
    """Extract long-format SEND observations for a selected cohort.

    Rows of an endpoint in a specimen panel pass through the data decisions
    first (the locked decisions unless ``data_decisions`` is given). A
    decision can remove the row or impute its specimen class; the endpoint then
    matches the resolved class. Endpoint decisions apply to every endpoint:
    ``exclude_endpoint`` and ``drop_method_rows`` remove the row, and
    ``reinterpret_unit`` gives the record the reading as its unit, so the
    daily projection converts it with the compiled rule of the reading.
    """

    decisions = load_data_decisions() if data_decisions is None else data_decisions
    endpoints = tuple(endpoints)
    if not endpoints or not selection.subjects:
        warnings: list[StageWarning] = []
        if not endpoints:
            warnings.append(
                StageWarning("no_endpoints_requested", "No endpoints were requested, so no observations were extracted.")
            )
        elif not selection.subjects:
            warnings.append(
                StageWarning("empty_cohort", "The cohort has no subjects, so no observations were extracted.")
            )
        return ObservationResult(
            endpoint_names=tuple(e.name for e in endpoints),
            warnings=tuple(warnings),
            counts={"record_count": 0},
        )

    contexts_by_key = {(c.study_id, c.subject_id): c for c in selection.subjects}
    selected_studies = {c.study_id for c in selection.subjects}
    endpoints_by_domain: dict[str, list[EndpointSpec]] = defaultdict(list)
    for endpoint in endpoints:
        endpoints_by_domain[endpoint.domain].append(endpoint)

    warnings = []
    skipped_not_selected = 0
    # Source-row accounting: every row read ends in exactly one of these outcomes.
    rows_read = 0
    tally = _Tally()
    chunks: dict[str, list[Any]] = {name: [] for name in OBSERVATION_COLUMNS}
    observed: set[tuple[str, str]] = set()
    record_count = 0

    for domain, domain_endpoints in sorted(endpoints_by_domain.items()):
        columns = table_columns(con, domain.lower())
        if not columns:
            warnings.append(
                StageWarning(
                    "endpoint_domain_unavailable",
                    f"The source data has no {domain} domain, which the requested endpoints need.",
                )
            )
            continue
        days = day_columns(domain, columns)
        # Every endpoint requires its test code, so a row is checked only against
        # the endpoints that name the row's code (in configuration order).
        by_code = endpoints_by_test_code(domain_endpoints)
        cursor, prefilter = _endpoint_filtered_cursor(
            con, domain, columns, selected_studies, set(contexts_by_key), set(by_code)
        )
        # The last column is the row position in the complete study-filtered order.
        domain_columns = tuple(item[0] for item in cursor.description)[:-1]
        rows_read += prefilter.rows_read
        skipped_not_selected += prefilter.not_selected
        tally.rows_lost["no_endpoint_match"] += prefilter.no_endpoint_code
        extractor = _DomainExtractor(
            domain=domain,
            table_columns=columns,
            domain_columns=domain_columns,
            days=days,
            qualifiers=qualifier_columns(domain, columns),
            baseline_flags=baseline_flag_columns(domain, columns),
            by_code=by_code,
            decisions=decisions,
            contexts_by_key=contexts_by_key,
            trace_items=trace_items,
            tally=tally,
        )
        domain_seen = False
        for batch in cursor.to_arrow_reader(_BATCH_ROWS):
            emitted = extractor.process(batch, chunks, observed)
            record_count += emitted
            domain_seen = domain_seen or emitted > 0
        if not domain_seen:
            warnings.append(
                StageWarning(
                    "domain_without_observations",
                    f"No {domain} observations were extracted for the cohort.",
                )
            )

    counts = {
        "record_count": record_count,
        "endpoint_count": len(endpoints),
        "domain_count": len(endpoints_by_domain),
        "skipped_not_selected": skipped_not_selected,
        "source_rows_read": rows_read,
        "source_rows_matched": tally.rows_matched,
        "source_rows_no_endpoint_match": tally.rows_lost["no_endpoint_match"],
        "source_rows_sex_not_applicable": tally.rows_lost["sex_not_applicable"],
        "source_rows_data_decision": tally.rows_lost["data_decision"],
        # Records keep their source unit; no conversion is applied at this stage.
        "records_unit_canonical": tally.unit_status["canonical"],
        "records_unit_not_canonical": tally.unit_status["not_canonical"],
        "records_unit_missing": tally.unit_status["missing"],
        "records_without_canonical_unit": tally.unit_status["no_canonical_unit"],
    }
    applied = {
        finding: {"action": action, "rows": sum(by_study.values()), "studies": len(by_study)}
        for source in (tally.removed, tally.imputed, tally.read)
        for (finding, action), by_study in sorted(source.items())
    }
    return ObservationResult(
        frame=_frame(chunks, record_count),
        endpoint_names=tuple(endpoint.name for endpoint in endpoints),
        warnings=tuple(warnings),
        counts=counts,
        decision_removals={finding: dict(by_study) for (finding, _), by_study in sorted(tally.removed.items())},
        decision_removal_items={finding: sorted_trace_items(items) for finding, items in tally.removed_items.items()},
        data_decisions={**decisions.identity(), "applied": applied},
        observed_subjects=frozenset(observed),
    )


# --- columnar assembly -----------------------------------------------------

# Source rows per Arrow batch; the memoized semantics carry across batches.
_BATCH_ROWS = 200_000


def _frame(chunks: dict[str, list[Any]], total: int) -> pd.DataFrame:
    """Build the observation table; each column gets the dtype that its Python values infer."""

    if not total:
        return _empty_frame()
    data: dict[str, pd.Series] = {}
    for name in OBSERVATION_COLUMNS:
        parts = chunks[name]
        if isinstance(parts[0], pa.Array):
            text = pa.chunked_array(parts)
            # Text with a value is a string column; text without any is all None.
            data[name] = (
                pd.Series(np.full(total, None, dtype=object)) if text.null_count == total
                else text.to_pandas(types_mapper={pa.string(): pd.StringDtype(na_value=np.nan)}.get)
            )
            continue
        values = parts[0] if len(parts) == 1 else np.concatenate(parts)
        if values.dtype == np.float64:
            # A float column holds missing values as NaN; without any value it is all None.
            data[name] = (
                pd.Series(values) if not np.isnan(values).all() else pd.Series(np.full(len(values), None, dtype=object))
            )
        elif values.dtype == object:
            data[name] = pd.Series(values).infer_objects()
        else:
            data[name] = pd.Series(values)
    return pd.DataFrame(data, columns=list(OBSERVATION_COLUMNS))


@dataclass(slots=True)
class _Tally:
    """Row accounting that the domain extractors add to."""

    rows_matched: int = 0
    rows_lost: Counter[str] = field(default_factory=Counter)
    unit_status: Counter[str] = field(default_factory=Counter)
    removed: dict[tuple[str, str], Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    imputed: dict[tuple[str, str], Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    read: dict[tuple[str, str], Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    removed_items: dict[str, list[TraceItem]] = field(default_factory=lambda: defaultdict(list))


@dataclass(slots=True)
class _Groups:
    """The distinct value tuples of some columns in a batch: a group per row, and each group's first row."""

    ids: np.ndarray
    first: np.ndarray
    encoded: list[tuple[np.ndarray, pa.Array]]

    def tuples(self) -> list[tuple[Any, ...]]:
        """The Python value tuple of each group (an empty tuple for no columns)."""

        if not self.encoded:
            return [()] * len(self.first)
        values = [
            dictionary.take(pa.array(codes[self.first])).to_pylist() for codes, dictionary in self.encoded
        ]
        return list(zip(*values))


def _combine(parts: Sequence[tuple[np.ndarray, int]], rows: int) -> tuple[np.ndarray, np.ndarray]:
    """Group rows by equal code tuples. Returns each row's group and each group's first row.

    ``parts`` are (codes, upper bound of the codes + 1) pairs.
    """

    ids = np.zeros(rows, dtype=np.int64)
    count = 1
    for codes, cardinality in parts:
        if cardinality == 1:
            continue
        ids, uniques = pd.factorize(ids * cardinality + codes)
        count = len(uniques)
    first = np.empty(count, dtype=np.int64)
    first[ids[::-1]] = np.arange(rows - 1, -1, -1)
    return ids, first


class _Batch:
    """One Arrow batch with the dictionary encoding of its columns, made on demand."""

    def __init__(self, table: pa.RecordBatch) -> None:
        self.table = table
        self.rows = table.num_rows
        self._encoded: dict[str, tuple[np.ndarray, pa.Array]] = {}

    def encoded(self, column: str) -> tuple[np.ndarray, pa.Array]:
        hit = self._encoded.get(column)
        if hit is None:
            array = pc.dictionary_encode(self.table.column(column), null_encoding="encode")
            hit = self._encoded[column] = (
                array.indices.to_numpy(zero_copy_only=False).astype(np.int64, copy=False),
                array.dictionary,
            )
        return hit

    def group(self, columns: Sequence[str]) -> _Groups:
        encoded = [self.encoded(column) for column in columns]
        ids, first = _combine([(codes, len(dictionary)) for codes, dictionary in encoded], self.rows)
        return _Groups(ids, first, encoded)


class _Registry:
    """Append-only columns of Python values that batches read as arrays."""

    def __init__(self, names: Sequence[str]) -> None:
        self._lists: dict[str, list[Any]] = {name: [] for name in names}
        self._arrays: dict[str, np.ndarray] = {}
        self._strings: dict[str, pa.Array] = {}

    def __len__(self) -> int:
        return len(next(iter(self._lists.values())))

    def append(self, **values: Any) -> int:
        position = len(self)
        for name, value in values.items():
            self._lists[name].append(value)
        return position

    def array(self, name: str, dtype: Any) -> np.ndarray:
        """A numeric column, rebuilt when it has grown."""

        values = self._lists[name]
        cached = self._arrays.get(name)
        if cached is None or len(cached) != len(values):
            cached = self._arrays[name] = np.array(values, dtype=dtype)
        return cached

    def strings(self, name: str) -> pa.Array:
        """A text column (None for missing), extended by the values appended since the last call."""

        values = self._lists[name]
        cached = self._strings.get(name)
        have = 0 if cached is None else len(cached)
        if have != len(values):
            tail = pa.array(values[have:], type=pa.string())
            cached = self._strings[name] = tail if cached is None else pa.concat_arrays([cached, tail])
        return cached

    def list(self, name: str) -> list[Any]:
        return self._lists[name]


def _strings(values: pa.Array | Sequence[str | None], codes: np.ndarray) -> pa.Array:
    """The text of each row: ``values`` at the row's code."""

    if not isinstance(values, pa.Array):
        values = pa.array(values, type=pa.string())
    return values.take(pa.array(codes))


_PARSE_FIELDS = (
    "result_state", "numeric_value", "raw_value", "value_column", "unit_column", "excluded",
    "exclusion_reason", "failure_reason", "parsed", "kind",
)
# What an endpoint match of one row adds to each of its records.
_DESCRIPTION_FIELDS = (
    "endpoint_name", "domain", "test_code", "test_name", "measurement_identity", "endpoint_key", "specimen",
    "unit", "method", "laterality", "portion_totality", "fasting_status", "laboratory_name", "unit_inferred",
    "status",
)


class _DomainExtractor:
    """Extracts the observations of one domain batch by batch, with its memos kept across batches."""

    def __init__(
        self,
        *,
        domain: str,
        table_columns: tuple[str, ...],
        domain_columns: tuple[str, ...],
        days: tuple[str, ...],
        qualifiers: tuple[str, ...],
        baseline_flags: tuple[str, ...],
        by_code: Mapping[str, Sequence[EndpointSpec]],
        decisions: DataDecisions,
        contexts_by_key: Mapping[tuple[str, str], Any],
        trace_items: bool,
        tally: _Tally,
    ) -> None:
        self.domain = domain
        self.table_columns = table_columns
        self.domain_columns = domain_columns
        self.days = days
        self.qualifiers = qualifiers
        self.baseline_flags = baseline_flags
        self.by_code = by_code
        self.decisions = decisions
        self.contexts_by_key = contexts_by_key
        self.trace_items = trace_items
        self.tally = tally
        # Many rows share their identity and result values, so each distinct
        # value tuple is parsed and matched once (see identity_columns).
        self.result_columns = result_columns(domain, domain_columns)
        self.identity_columns = identity_columns(domain, domain_columns, days)
        self.pool_columns = tuple(
            column for column in (f"{domain.upper()}POOLID", "POOLID") if column in domain_columns
        )
        self.time_columns = tuple(
            column
            for column in (f"{domain.upper()}DTC", f"{domain.upper()}STDTC", f"{domain.upper()}ENDTC", "DTC")
            if column in domain_columns
        )
        self.sequence_columns = tuple(
            column for column in (f"{domain.upper()}SEQ", "SEQ") if column in domain_columns
        )
        self.testcd_column = f"{domain}TESTCD"
        self.times: dict[tuple[Any, ...], str] = {}
        self.sequences: dict[tuple[Any, ...], tuple[float, str]] = {}
        self.parse_ids: dict[tuple[Any, ...], int] = {}
        self.parses = _Registry(_PARSE_FIELDS)
        self.parse_kinds: dict[tuple[Any, ...], int] = {}
        self.matches: dict[tuple[Any, ...], int] = {}
        self.match_list: list[_RowMatch] = []
        self.descriptions = _Registry(_DESCRIPTION_FIELDS)
        self.sex_codes: dict[str | None, int] = {}

    # --- distinct-value semantics ---------------------------------------

    def _parse_id(self, values: tuple[Any, ...]) -> int:
        parsed = parse_result(self.domain, dict(zip(self.result_columns, values)), self.domain_columns)
        kind_key = (parsed.state, parsed.unit, parsed.value_column is None)
        kind = self.parse_kinds.setdefault(kind_key, len(self.parse_kinds))
        pid = self.parses.append(
            result_state=parsed.state,
            numeric_value=float("nan") if parsed.numeric_value is None else parsed.numeric_value,
            raw_value=parsed.raw_value,
            value_column=parsed.value_column,
            unit_column=parsed.unit_column,
            excluded=parsed.excluded,
            exclusion_reason=parsed.exclusion_reason,
            failure_reason=parsed.failure_reason,
            parsed=parsed,
            kind=kind,
        )
        self.parse_ids[values] = pid
        return pid

    def _new_match(self, row: dict[str, Any], parsed: ParsedResult, study_id: str) -> int:
        domain = self.domain
        match = _match_row(domain, row, self.table_columns, parsed, self.by_code, self.decisions, study_id)
        match.study_id = study_id
        if match.matched:
            measurement = measurement_context(domain, row, self.domain_columns, parsed_result=parsed)
            key = endpoint_key(domain, row, self.domain_columns, self.qualifiers)
            for endpoint in match.matched:
                # The record marks an implicit unit, so the inference stays visible.
                unit = measurement["unit"]
                if unit is None and parsed.state == "numeric_observed":
                    unit = endpoint.implicit_unit
                reading = self.decisions.reading(study_id, endpoint.name, unit)
                if reading is not None:
                    unit = reading.reading
                identity = measurement_identity(
                    domain,
                    row,
                    self.domain_columns,
                    parsed_result=parsed,
                    canonical_test_code=endpoint.name,
                    specimen=match.resolved.get(endpoint.name),
                    context=measurement,
                )
                match.description_ids.append(
                    self.descriptions.append(
                        endpoint_name=endpoint.name,
                        domain=domain,
                        test_code=measurement["test_code"],
                        test_name=measurement["test_name"],
                        measurement_identity=identity,
                        endpoint_key=key,
                        specimen=match.resolved.get(endpoint.name, measurement["specimen"]),
                        unit=unit,
                        method=measurement["method"],
                        laterality=measurement["laterality"],
                        portion_totality=measurement["portion_totality"],
                        fasting_status=measurement["fasting_status"],
                        laboratory_name=measurement["laboratory_name"],
                        unit_inferred=measurement["unit"] is None and unit is not None,
                        status=_unit_status(endpoint, unit),
                    )
                )
                match.read_keys.append(None if reading is None else (reading.finding, reading.action))
        self.match_list.append(match)
        return len(self.match_list) - 1

    # --- one batch -------------------------------------------------------

    def process(
        self, table: pa.RecordBatch, chunks: dict[str, list[Any]], observed: set[tuple[str, str]]
    ) -> int:
        """Account for the rows of one batch and add its records to ``chunks``. Returns the record count."""

        rows = table.num_rows
        if not rows:
            return 0
        domain = self.domain
        batch = _Batch(table)
        source_index = table.column(len(self.domain_columns)).to_numpy(zero_copy_only=False).astype(np.int64)

        # Subjects: the selected context of each distinct (study, subject).
        pairs = batch.group((STUDY_ID, SUBJECT_ID))
        pair_study: list[str] = []
        pair_subject: list[str] = []
        pair_sample: list[str] = []
        pair_sex: list[str | None] = []
        for study, subject in pairs.tuples():
            study_id = str(study or "")
            subject_id = str(subject or "").strip()
            context = self.contexts_by_key.get((study_id, subject_id))
            if context is None:
                raise RuntimeError("the SQL subject prefilter streamed a subject that is not selected")
            pair_study.append(study_id)
            pair_subject.append(subject_id)
            pair_sample.append(context.sample_id)
            pair_sex.append(context.sex)
        sex_code = np.array([self.sex_codes.setdefault(sex, len(self.sex_codes)) for sex in pair_sex], dtype=np.int64)

        # Study day.
        day_groups = batch.group(self.days)
        day_values = [first_numeric_day(dict(zip(self.days, values)), self.days) for values in day_groups.tuples()]

        # Parsed result: once per distinct value tuple of the result columns.
        parse_groups = batch.group(self.result_columns)
        parse_of_group = np.empty(len(parse_groups.first), dtype=np.int64)
        for position, values in enumerate(parse_groups.tuples()):
            parse_id = self.parse_ids.get(values)
            parse_of_group[position] = self._parse_id(values) if parse_id is None else parse_id
        parse_row = parse_of_group[parse_groups.ids]
        kind_row = self.parses.array("kind", np.int64)[parse_row]

        # Endpoint match: once per distinct identity and parsed kind.
        identity_groups = batch.group(self.identity_columns)
        match_ids, match_first = _combine(
            [(identity_groups.ids, len(identity_groups.first)), (kind_row, len(self.parse_kinds))], rows
        )
        identity_values = identity_groups.tuples()
        kind_keys = list(self.parse_kinds)
        first_rows = match_first.tolist()
        identity_of_first = identity_groups.ids[match_first].tolist()
        kind_of_first = kind_row[match_first].tolist()
        match_of_group = np.empty(len(first_rows), dtype=np.int64)
        missing: list[tuple[int, int, tuple[Any, ...]]] = []
        for position, (index, identity, kind) in enumerate(zip(first_rows, identity_of_first, kind_of_first)):
            key = (identity_values[identity], kind_keys[kind])
            match = self.matches.get(key)
            if match is None:
                missing.append((position, index, key))
            else:
                match_of_group[position] = match
        if missing:
            representatives = table.select(list(self.domain_columns)).take(
                pa.array([index for _, index, _ in missing])
            ).to_pylist()
            parsed_list = self.parses.list("parsed")
            for (position, index, key), row in zip(missing, representatives):
                match = self.matches[key] = self._new_match(
                    row, parsed_list[parse_row[index]], pair_study[pairs.ids[index]]
                )
                match_of_group[position] = match
        match_row = match_of_group[match_ids]

        # Rows with the same match, subject sex and study day keep the same endpoints.
        triple_ids, triple_first = _combine(
            [
                (match_row, len(self.match_list)),
                (sex_code[pairs.ids], len(self.sex_codes)),
                (day_groups.ids, len(day_values)),
            ],
            rows,
        )
        triple_rows = np.bincount(triple_ids, minlength=len(triple_first))
        triple_size = np.zeros(len(triple_first), dtype=np.int64)
        triple_descriptions: list[list[int]] = []
        triple_removing = np.zeros(len(triple_first), dtype=bool)
        tally = self.tally
        for position, index in enumerate(triple_first.tolist()):
            match = self.match_list[match_row[index]]
            sex = pair_sex[pairs.ids[index]]
            applicable = [k for k, endpoint in enumerate(match.matched) if endpoint.applies_to_sex(sex)]
            matching = applicable
            count = int(triple_rows[position])
            study_id = match.study_id
            for key in match.removed:
                tally.removed[key][study_id] += count
            if match.removed:
                triple_removing[position] = True
            for key in match.imputed:
                tally.imputed[key][study_id] += count
            if not matching:
                tally.rows_lost[
                    "data_decision" if not match.matched and match.removed
                    else "no_endpoint_match" if not match.matched
                    else "sex_not_applicable"
                ] += count
                triple_descriptions.append([])
                continue
            tally.rows_matched += count
            for key in {match.read_keys[k] for k in matching if match.read_keys[k] is not None}:
                tally.read[key][study_id] += count
            triple_size[position] = len(matching)
            triple_descriptions.append([match.description_ids[k] for k in matching])

        if self.trace_items and triple_removing.any():
            self._trace_removed(
                table, np.flatnonzero(triple_removing[triple_ids]), match_row, pairs.ids, pair_subject,
                day_groups.ids, day_values, parse_row,
            )

        # Expand each kept row to one record per matching endpoint, in endpoint order.
        per_row = triple_size[triple_ids]
        total = int(per_row.sum())
        if not total:
            return 0
        out_row = np.repeat(np.arange(rows), per_row)
        within = np.arange(total) - np.repeat(np.cumsum(per_row) - per_row, per_row)
        offsets = np.cumsum(triple_size) - triple_size
        flat = np.array([item for items in triple_descriptions for item in items], dtype=np.int64)
        description = flat[offsets[triple_ids[out_row]] + within]
        parse_out = parse_row[out_row]
        pair_out = pairs.ids[out_row]
        observed.update(
            (pair_study[position], pair_subject[position]) for position in np.unique(pair_out).tolist()
        )
        used = np.bincount(description, minlength=len(self.descriptions))
        statuses = self.descriptions.list("status")
        for position in np.flatnonzero(used).tolist():
            tally.unit_status[statuses[position]] += int(used[position])

        def take(values: Sequence[Any], codes: np.ndarray) -> np.ndarray:
            array = np.empty(len(values), dtype=object)
            array[:] = values
            return array[codes]

        add = {
            "study_id": _strings(pair_study, pair_out),
            "subject_id": _strings(pair_subject, pair_out),
            "sample_id": _strings(pair_sample, pair_out),
            "sex": _strings(pair_sex, pair_out),
            "study_day": take(day_values, day_groups.ids[out_row]),
        }
        pool_groups = batch.group(self.pool_columns)
        pool_values = [row_pool_id(domain, dict(zip(self.pool_columns, values))) for values in pool_groups.tuples()]
        add["pool_id"] = _strings(pool_values, pool_groups.ids[out_row])
        baseline_groups = batch.group(self.baseline_flags)
        baseline_values = np.array(
            [is_explicit_baseline(dict(zip(self.baseline_flags, values)), self.baseline_flags)
             for values in baseline_groups.tuples()],
            dtype=bool,
        )
        add["explicit_baseline"] = baseline_values[baseline_groups.ids[out_row]]
        # The time and the sequence number repeat independently, so each is keyed on its own columns.
        time_groups = batch.group(self.time_columns)
        times = []
        for values in time_groups.tuples():
            part = self.times.get(values)
            if part is None:
                part = self.times[values] = source_order_key(
                    domain, dict(zip(self.time_columns, values)), stable_index=0
                )[0]
            times.append(part)
        sequence_groups = batch.group(self.sequence_columns)
        sequences = []
        for values in sequence_groups.tuples():
            part = self.sequences.get(values)
            if part is None:
                part = self.sequences[values] = source_order_key(
                    domain, dict(zip(self.sequence_columns, values)), stable_index=0
                )[1:3]
            sequences.append(part)
        time_out = time_groups.ids[out_row]
        sequence_out = sequence_groups.ids[out_row]
        add["source_order"] = np.fromiter(
            zip(
                take(times, time_out).tolist(),
                np.array([part[0] for part in sequences], dtype=np.float64)[sequence_out].tolist(),
                take([part[1] for part in sequences], sequence_out).tolist(),
                source_index[out_row].tolist(),
            ),
            dtype=object,
            count=total,
        )
        for name in (
            "endpoint_name", "domain", "test_code", "test_name", "measurement_identity", "endpoint_key",
            "specimen", "unit", "method", "laterality", "portion_totality", "fasting_status", "laboratory_name",
        ):
            add[name] = _strings(self.descriptions.strings(name), description)
        add["unit_inferred"] = self.descriptions.array("unit_inferred", bool)[description]
        for name in ("result_state", "raw_value", "value_column", "unit_column", "exclusion_reason", "failure_reason"):
            add[name] = _strings(self.parses.strings(name), parse_out)
        add["numeric_value"] = self.parses.array("numeric_value", np.float64)[parse_out]
        add["excluded"] = self.parses.array("excluded", bool)[parse_out]
        for name, values in add.items():
            chunks[name].append(values)
        return total

    def _trace_removed(
        self,
        table: pa.RecordBatch,
        rows: np.ndarray,
        match_row: np.ndarray,
        pair_row: np.ndarray,
        pair_subject: list[str],
        day_row: np.ndarray,
        day_values: list[int | None],
        parse_row: np.ndarray,
    ) -> None:
        """Record the removed rows themselves, in source order."""

        endpoints = (
            table.column(self.testcd_column).take(pa.array(rows)).to_pylist()
            if self.testcd_column in self.domain_columns
            else [None] * len(rows)
        )
        parsed_list = self.parses.list("parsed")
        for row, endpoint in zip(rows.tolist(), endpoints):
            match = self.match_list[match_row[row]]
            parsed = parsed_list[parse_row[row]]
            for key in match.removed:
                self.tally.removed_items[key[0]].append({
                    "study": match.study_id, "subject": pair_subject[pair_row[row]], "endpoint": endpoint,
                    "day": day_values[day_row[row]],
                    "value": parsed.numeric_value if parsed.numeric_value is not None else parsed.raw_value,
                })


# --- endpoint matching helpers (ported from the host extraction layer) -----


def _unit_status(endpoint: EndpointSpec, unit: str | None) -> str:
    """Classify a record unit against the endpoint's canonical unit (no conversion)."""

    if endpoint.target_unit is None:
        return "no_canonical_unit"
    if unit is None:
        return "missing"
    if unit == endpoint.target_unit or any(
        rule.source_unit == unit and rule.scale == 1 and rule.offset == 0
        for rule in endpoint.compiled_unit_rules
    ):
        return "canonical"
    return "not_canonical"


@dataclass(slots=True)
class _RowMatch:
    """What one distinct row identity matched, and how a row of it is described."""

    matched: tuple[EndpointSpec, ...]
    resolved: dict[str, str]
    removed: frozenset[tuple[str, str]]
    imputed: frozenset[tuple[str, str]]
    study_id: str = ""
    # Per matched endpoint: its description in the extractor's registry, and its unit reading decision.
    description_ids: list[int] = field(default_factory=list)
    read_keys: list[tuple[str, str] | None] = field(default_factory=list)


def _match_row(
    domain: str,
    row: dict[str, Any],
    columns: tuple[str, ...],
    parsed: ParsedResult,
    by_code: Mapping[str, Sequence[EndpointSpec]],
    decisions: DataDecisions,
    study_id: str,
) -> _RowMatch:
    """Match one row against the endpoints that name its test code, in configuration order."""

    matched: list[EndpointSpec] = []
    resolved: dict[str, str] = {}
    removed: set[tuple[str, str]] = set()
    imputed: set[tuple[str, str]] = set()
    for endpoint in by_code.get(row_test_code(domain, row), ()):
        if endpoint.specimen_panel is None:
            if not endpoint_matches_row(endpoint, row, columns, parsed=parsed):
                continue
            removal = decisions.removal(study_id, endpoint.name, row.get(f"{domain}METHOD"))
            if removal is not None:
                removed.add((removal.finding, removal.action))
                continue
            matched.append(endpoint)
            continue
        if not endpoint_identity_matches(endpoint, row, columns, parsed=parsed):
            continue
        resolution = decisions.resolve(
            study_id, endpoint.specimen_panel, row_specimen_class(domain, row), endpoint.specimens,
            endpoint=endpoint.name, method=row.get(f"{domain}METHOD"),
            category=row_category(domain, row),
        )
        if resolution.outcome == "removed":
            removed.add((resolution.finding, resolution.action))
            continue
        if not endpoint_specimen_matches(endpoint, row, specimen_class=resolution.specimen_class):
            continue
        if resolution.outcome == "imputed":
            imputed.add((resolution.finding, resolution.action))
        resolved[endpoint.name] = resolution.specimen_class
        matched.append(endpoint)
    return _RowMatch(tuple(matched), resolved, frozenset(removed), frozenset(imputed))


@dataclass(frozen=True)
class _PrefilterCounts:
    """Rows of the selected studies, and the rows that the SQL prefilter did not stream."""

    rows_read: int
    not_selected: int
    no_endpoint_code: int


def _endpoint_filtered_cursor(
    con: Any,
    domain: str,
    columns: tuple[str, ...],
    selected_studies: set[str],
    selected_keys: set[tuple[str, str]],
    codes: set[str],
) -> tuple[Any, _PrefilterCounts]:
    """Stream the rows of selected subjects whose test code an endpoint names.

    The row loop normalizes subject IDs with ``str.strip`` and test codes with
    the canonical SEND normalizer (NFKC), which SQL cannot reproduce. So SQL
    reads only the distinct raw subject and test-code values of the selected
    studies, :func:`row_test_code` and ``str.strip`` classify them, and SQL
    streams only the rows with a kept raw value. Each streamed row ends with its
    1-based position in the complete study-filtered order, which is the order
    the loop used when it read every row. The counts give the rows that were
    not streamed, so the receipt accounts for every row of the selected studies.
    """

    table = quote_ident(domain.lower())
    selected_columns = endpoint_projection_columns(domain, columns)
    projection = ", ".join(q(column) for column in selected_columns)
    study_ids = sorted(study_id for study_id in selected_studies if study_id)
    where = (
        f"WHERE cast({q(STUDY_ID)} AS VARCHAR) IN ({', '.join('?' for _ in study_ids)})"
        if study_ids else ""
    )
    # Each distinct value is read raw, for the Python classification, and as
    # VARCHAR, for the SQL comparison, so typed source columns also match exactly.
    subject_pairs = [
        (study_text, subject_text)
        for study, subject, study_text, subject_text in con.execute(
            f"SELECT DISTINCT {q(STUDY_ID)}, {q(SUBJECT_ID)}, "
            f"cast({q(STUDY_ID)} AS VARCHAR), cast({q(SUBJECT_ID)} AS VARCHAR) FROM {table} {where}",
            study_ids,
        ).fetchall()
        if (str(study or ""), str(subject or "").strip()) in selected_keys
    ]
    code_columns = tuple(
        column
        for column in (f"{domain}TESTCD", f"{domain}TEST", "TESTCD", "TEST")
        if column in selected_columns
    )
    code_values = [
        values[len(code_columns):]
        for values in con.execute(
            f"SELECT DISTINCT {', '.join(q(column) for column in code_columns)}, "
            f"{', '.join(f'cast({q(column)} AS VARCHAR)' for column in code_columns)} "
            f"FROM {table} {where}",
            study_ids,
        ).fetchall()
        if row_test_code(domain, dict(zip(code_columns, values))) in codes
    ] if code_columns else []
    subject_ok = (
        "EXISTS (SELECT 1 FROM selected_subjects AS keys "
        f"WHERE cast(rows.{q(STUDY_ID)} AS VARCHAR) IS NOT DISTINCT FROM keys.study "
        f"AND cast(rows.{q(SUBJECT_ID)} AS VARCHAR) IS NOT DISTINCT FROM keys.subject)"
    )
    code_ok = (
        "EXISTS (SELECT 1 FROM selected_codes AS codes WHERE "
        + " AND ".join(
            f"cast(rows.{q(column)} AS VARCHAR) IS NOT DISTINCT FROM codes.code_{index}"
            for index, column in enumerate(code_columns)
        )
        + ")"
        if code_columns
        else f"{'TRUE' if row_test_code(domain, {}) in codes else 'FALSE'}"
    )
    selected_codes = ", ".join(
        f"unnest(?::VARCHAR[]) AS code_{index}" for index in range(len(code_columns))
    ) or "NULL AS code_0"
    statement = f"""
        WITH selected_subjects AS (
            SELECT unnest(?::VARCHAR[]) AS study, unnest(?::VARCHAR[]) AS subject
        ),
        selected_codes AS (SELECT {selected_codes}),
        rows AS (SELECT {projection} FROM {table} {where}),
        flagged AS (
            SELECT rows.*, {subject_ok} AS subject_ok, {code_ok} AS code_ok FROM rows
        )
    """
    params = [
        [study for study, _ in subject_pairs],
        [subject for _, subject in subject_pairs],
        *([[values[index] for values in code_values] for index in range(len(code_columns))]),
        *study_ids,
    ]
    # Only the stream sorts the rows; the counts read them unsorted.
    rows_read, not_selected, no_endpoint_code = con.execute(
        f"""{statement}
        SELECT count(*),
            count(*) FILTER (WHERE NOT subject_ok),
            count(*) FILTER (WHERE subject_ok AND NOT code_ok)
        FROM flagged""",
        params,
    ).fetchone()
    cursor = con.execute(
        f"""{statement}
        SELECT {projection}, __vcgen_source_index FROM (
            SELECT flagged.*, row_number() OVER (ORDER BY {projection}) AS __vcgen_source_index
            FROM flagged
        )
        WHERE subject_ok AND code_ok ORDER BY __vcgen_source_index""",
        params,
    )
    return cursor, _PrefilterCounts(int(rows_read), int(not_selected), int(no_endpoint_code))


__all__ = [
    "OBSERVATION_COLUMNS",
    "ObservationResult",
    "extract_observations",
]
