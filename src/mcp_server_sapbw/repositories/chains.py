"""Process-chain repository (B3).

Reads chain structure from RSPCCHAIN/RSPCCHAINATTR/RSPCCHAINT and runtime history from
RSPCLOGCHAIN/RSPCPROCESSLOG. Column usage and edge linkage were validated live in B3:

- Chain edges are event-parameter linked: ``successor.EVENTP_START == predecessor.EVENTP_GREEN``
  (green/success) or ``EVENTP_RED`` (error). The ``EVENT_*`` GUIDs are chain-wide constants, so they
  are NOT the link key.
- RSPCPROCESSLOG step timing is ``STARTTIMESTAMP``/``ENDTIMESTAMP`` decimals (YYYYMMDDHHMMSS.fff).
- Frequency is classified from observed run cadence in RSPCLOGCHAIN (data-driven, never from names).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil, floor
from typing import Any

from ..core.capabilities import MAX_RUNTIME_WINDOW_DAYS
from ..core.dialect import like_term
from ..models.chains import (
    Chain,
    ChainEdge,
    ChainProcess,
    ChainRuntimes,
    ChainSummary,
    DurationStats,
    FrequencyClass,
    ScheduleMatrixEntry,
    StepRuntime,
)
from ..models.provenance import UnsupportedResult
from .base import Repository

# Frequency thresholds (runs per day), from observed cadence.
_HOURLY_RPD = 18.0
_MULTI_DAILY_RPD = 1.5
_DAILY_RPD = 0.5
_WEEKLY_RPD = 0.08
_MONTHLY_RPD = 0.015
_MIN_RUNS_FOR_FREQ = 3
_P95 = 0.95
_MAX_RUNS_FOR_STEPS = 1000  # cap step-level analysis for very frequent chains
_TOP_BOTTLENECK = 5
_SUCCESS_STATUS = "G"  # RSPCLOGCHAIN ANALYZED_STATUS green (empirically confirmed)
_TS_DIGITS = 14  # YYYYMMDDHHMMSS
_DATE_DIGITS = 8  # YYYYMMDD

# Frequency buckets as (min runs-per-day, label), highest first; first match wins.
_FREQUENCY_BUCKETS: tuple[tuple[float, FrequencyClass], ...] = (
    (_HOURLY_RPD, "hourly"),
    (_MULTI_DAILY_RPD, "multiple_daily"),
    (_DAILY_RPD, "daily"),
    (_WEEKLY_RPD, "weekly"),
    (_MONTHLY_RPD, "monthly"),
)


def _parse_bw_timestamp(value: Any) -> datetime | None:
    """Parse an RSPCPROCESSLOG decimal timestamp (YYYYMMDDHHMMSS.fff) to a naive datetime."""
    if value is None:
        return None
    try:
        text = f"{Decimal(str(value)):.3f}"
    except (InvalidOperation, ValueError):
        return None
    intpart, _, frac = text.partition(".")
    if len(intpart) != _TS_DIGITS or not intpart.isdigit() or intpart.startswith("0"):
        return None
    try:
        parsed = datetime.strptime(intpart, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(microsecond=int((frac + "000")[:3]) * 1000)


def _parse_dats_tims(datum: Any, zeit: Any) -> datetime | None:
    """Parse RSPCLOGCHAIN DATUM (YYYYMMDD) + ZEIT (HHMMSS) to a naive datetime."""
    day = str(datum).strip()
    if len(day) != _DATE_DIGITS or not day.isdigit() or day == "00000000":
        return None
    try:
        seconds = f"{int(zeit):06d}" if zeit not in (None, "") else "000000"
    except (ValueError, TypeError):
        seconds = "000000"
    try:
        return datetime.strptime(day + seconds, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    low, high = floor(rank), ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def _classify_frequency(runs_per_day: float | None, run_count: int) -> FrequencyClass:
    """Bucket observed runs-per-day. Returns 'unknown' when there are too few runs to judge."""
    if runs_per_day is None or run_count < _MIN_RUNS_FOR_FREQ:
        return "unknown"
    for threshold, label in _FREQUENCY_BUCKETS:
        if runs_per_day >= threshold:
            return label
    return "irregular"


def _date_from_datum(datum: Any) -> date | None:
    day = str(datum).strip()
    if len(day) != _DATE_DIGITS or not day.isdigit():
        return None
    try:
        return datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None


class ChainsRepository(Repository):
    """Chain structure, runtime statistics, and schedule matrix."""

    # --- listing -------------------------------------------------------------------------

    def list_chains(
        self,
        *,
        name_pattern: str | None = None,
        active_only: bool = True,
        window_days: int = 90,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ChainSummary], int] | UnsupportedResult:
        unsupported = self.require("chain_attr")
        if unsupported is not None:
            return unsupported

        where = ["OBJVERS = 'A'"]
        params: list[Any] = []
        if active_only:
            where.append("OBJSTAT = ?")
            params.append("ACT")
        if name_pattern:
            like = like_term(name_pattern)
            where.append(like.clause("CHAIN_ID"))
            params.append(like.value)

        base = self.dialect.build_select(
            columns=["CHAIN_ID", "APPLNM", "OBJSTAT"],
            from_logical="chain_attr",
            where=where,
            params=params,
            order_by=["CHAIN_ID"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        chain_ids = [str(r[0]) for r in rows]
        texts = self._chain_texts(chain_ids)
        run_info = self._run_summary(chain_ids, window_days)

        summaries: list[ChainSummary] = []
        for row in rows:
            chain_id, appl, objstat = str(row[0]), row[1], row[2]
            runs_per_day, frequency, last_run = self._summarize_cadence(run_info.get(chain_id))
            summaries.append(
                ChainSummary(
                    chain_id=chain_id,
                    description=texts.get(chain_id),
                    active=(objstat == "ACT"),
                    application=str(appl) if appl else None,
                    frequency=frequency,
                    runs_per_day=runs_per_day,
                    last_run=last_run,
                    provenance=[
                        self.provenance("chain_attr", {"CHAIN_ID": chain_id, "OBJVERS": "A"})
                    ],
                )
            )
        return summaries, total

    # --- structure -----------------------------------------------------------------------

    def get_chain(
        self, chain_id: str, *, resolve_subchains: bool = True, max_depth: int = 5
    ) -> Chain | UnsupportedResult:
        unsupported = self.require("chain_edges", "chain_attr")
        if unsupported is not None:
            return unsupported
        return self._build_chain(chain_id, resolve_subchains, max_depth, visited=set())

    def _build_chain(
        self, chain_id: str, resolve_subchains: bool, max_depth: int, visited: set[str]
    ) -> Chain:
        header = self.select(
            self.dialect.build_select(
                columns=["APPLNM", "OBJSTAT"],
                from_logical="chain_attr",
                where=["OBJVERS = 'A'", "CHAIN_ID = ?"],
                params=[chain_id],
            )
        )
        application = str(header[0][0]) if header and header[0][0] else None
        objstat = str(header[0][1]) if header else None

        proc_rows = self.select(
            self.dialect.build_select(
                columns=["TYPE", "VARIANTE", "LNR", "EVENTP_START", "EVENTP_GREEN", "EVENTP_RED"],
                from_logical="chain_edges",
                where=["OBJVERS = 'A'", "CHAIN_ID = ?"],
                params=[chain_id],
                order_by=["LNR"],
            )
        )

        processes: list[ChainProcess] = []
        start_param_to_keys: dict[str, list[str]] = defaultdict(list)
        out_params: dict[str, tuple[Any, Any]] = {}
        for row in proc_rows:
            ptype, variant, lnr, ep_start, ep_green, ep_red = row
            ptype, variant = str(ptype), str(variant)
            key = f"{ptype}:{variant}"
            is_subchain = ptype == "CHAIN"
            processes.append(
                ChainProcess(
                    key=key,
                    process_type=ptype,
                    variant=variant,
                    line_no=int(lnr) if lnr is not None else None,
                    is_subchain=is_subchain,
                    subchain_id=variant if is_subchain else None,
                    provenance=self.provenance(
                        "chain_edges",
                        {"CHAIN_ID": chain_id, "TYPE": ptype, "VARIANTE": variant, "OBJVERS": "A"},
                    ),
                )
            )
            if ep_start:
                start_param_to_keys[str(ep_start)].append(key)
            out_params[key] = (ep_green, ep_red)

        edges: list[ChainEdge] = []
        for key, (ep_green, ep_red) in out_params.items():
            for param, link in ((ep_green, "green"), (ep_red, "red")):
                if not param:
                    continue
                for successor in start_param_to_keys.get(str(param), []):
                    if successor != key:
                        edges.append(
                            ChainEdge(
                                source_key=key,
                                target_key=successor,
                                link=link,  # type: ignore[arg-type]
                                provenance=self.provenance(
                                    "chain_edges", {"CHAIN_ID": chain_id, "EVENTP": str(param)}
                                ),
                            )
                        )

        subchain_ids = sorted({p.subchain_id for p in processes if p.subchain_id})
        chain = Chain(
            chain_id=chain_id,
            description=self._chain_texts([chain_id]).get(chain_id),
            active=(objstat == "ACT") if objstat else bool(proc_rows),
            application=application,
            processes=processes,
            edges=edges,
            subchain_ids=subchain_ids,
            provenance=self.provenance("chain_attr", {"CHAIN_ID": chain_id, "OBJVERS": "A"}),
        )

        if resolve_subchains and subchain_ids:
            next_visited = visited | {chain_id}
            for sub_id in subchain_ids:
                if max_depth <= 0 or sub_id in next_visited:
                    chain.truncated_recursion = True
                    continue
                chain.subchains.append(
                    self._build_chain(sub_id, resolve_subchains, max_depth - 1, next_visited)
                )
        return chain

    # --- runtimes ------------------------------------------------------------------------

    def get_chain_runtimes(
        self, chain_id: str, *, days: int = 90
    ) -> ChainRuntimes | UnsupportedResult:
        unsupported = self.require("log_chain", "process_log")
        if unsupported is not None:
            return unsupported

        # Cap the window at the owner-decided 1-year maximum (mission Known Limitation 5).
        days = min(days, MAX_RUNTIME_WINDOW_DAYS)
        cutoff = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
        run_rows = self.select(
            self.dialect.build_select(
                columns=["LOG_ID", "DATUM", "ZEIT", "ANALYZED_STATUS"],
                from_logical="log_chain",
                where=["CHAIN_ID = ?", "DATUM >= ?"],
                params=[chain_id, cutoff],
                order_by=["DATUM", "ZEIT"],
            )
        )
        provenance = [
            self.provenance("log_chain", {"CHAIN_ID": chain_id}),
            self.provenance("process_log", {"CHAIN_ID": chain_id}),
        ]
        if not run_rows:
            return ChainRuntimes(
                chain_id=chain_id,
                window_days_requested=days,
                window_days_actual=0,
                total_runs=0,
                caveats=["no runs recorded in the requested window"],
                provenance=provenance,
            )

        total_runs = len(run_rows)
        successful = sum(1 for r in run_rows if str(r[3]) == _SUCCESS_STATUS)
        start_dates = [d for d in (_date_from_datum(r[1]) for r in run_rows) if d is not None]
        window_start = min(start_dates) if start_dates else None
        window_actual = (date.today() - window_start).days + 1 if window_start else 0

        # Cap step-level analysis to the most recent runs for very frequent chains.
        capped = run_rows[-_MAX_RUNS_FOR_STEPS:] if total_runs > _MAX_RUNS_FOR_STEPS else run_rows
        log_ids = [str(r[0]) for r in capped]
        run_start_by_log = {str(r[0]): _parse_dats_tims(r[1], r[2]) for r in capped}

        durations, bottlenecks, run_intervals = self._step_analysis(log_ids, run_start_by_log)

        stats = DurationStats(count=len(durations))
        if durations:
            stats = DurationStats(
                count=len(durations),
                min_s=round(min(durations), 1),
                median_s=round(statistics.median(durations), 1),
                mean_s=round(statistics.fmean(durations), 1),
                p95_s=round(_percentile(durations, _P95) or 0.0, 1),
                max_s=round(max(durations), 1),
            )

        caveats = [
            "frequency/critical-path are observed approximations; "
            "'bottleneck' = longest-running steps, not a full longest-path",
            "step STATE / ANALYZED_STATUS decode is empirical",
        ]
        overlaps = run_intervals
        if overlaps:
            caveats.append(
                "overlapping runs observed; durations reflect contention, not intrinsic cost"
            )
        if total_runs > _MAX_RUNS_FOR_STEPS:
            caveats.append(
                f"duration stats use the most recent {_MAX_RUNS_FOR_STEPS} of {total_runs} runs"
            )

        return ChainRuntimes(
            chain_id=chain_id,
            window_days_requested=days,
            window_days_actual=window_actual,
            window_start=window_start,
            total_runs=total_runs,
            successful_runs=successful,
            success_rate=round(successful / total_runs, 4) if total_runs else None,
            duration_seconds=stats,
            bottleneck_steps=bottlenecks,
            observed_overlap_runs=overlaps,
            caveats=caveats,
            provenance=provenance,
        )

    def _step_analysis(
        self, log_ids: list[str], run_start_by_log: dict[str, datetime | None]
    ) -> tuple[list[float], list[StepRuntime], int]:
        """Return (per-run durations, top bottleneck steps, observed overlap count)."""
        if not log_ids:
            return [], [], 0
        placeholders = ", ".join("?" for _ in log_ids)
        step_rows = self.select(
            self.dialect.build_select(
                columns=[
                    "LOG_ID",
                    "TYPE",
                    "VARIANTE",
                    "INSTANCE",
                    "STATE",
                    "STARTTIMESTAMP",
                    "ENDTIMESTAMP",
                ],
                from_logical="process_log",
                where=[f"LOG_ID IN ({placeholders})"],
                params=list(log_ids),
            )
        )
        steps_by_log: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        all_steps: list[StepRuntime] = []
        for row in step_rows:
            log_id, ptype, variant, instance, state, start_ts, end_ts = row
            start, end = _parse_bw_timestamp(start_ts), _parse_bw_timestamp(end_ts)
            if start is None or end is None or end < start:
                continue
            steps_by_log[str(log_id)].append((start, end))
            all_steps.append(
                StepRuntime(
                    process_type=str(ptype),
                    variant=str(variant),
                    instance=str(instance) if instance else None,
                    duration_s=round((end - start).total_seconds(), 1),
                    state=str(state) if state else None,
                    provenance=self.provenance(
                        "process_log", {"LOG_ID": str(log_id), "TYPE": str(ptype)}
                    ),
                )
            )

        durations: list[float] = []
        run_windows: list[tuple[datetime, datetime]] = []
        for log_id, spans in steps_by_log.items():
            run_start = min(s for s, _ in spans)
            run_end = max(e for _, e in spans)
            durations.append((run_end - run_start).total_seconds())
            anchor = run_start_by_log.get(log_id) or run_start
            run_windows.append((anchor, run_end))

        run_windows.sort(key=lambda w: w[0])
        overlaps = 0
        prev_end: datetime | None = None
        for start, end in run_windows:
            if prev_end is not None and start < prev_end:
                overlaps += 1
            prev_end = max(prev_end, end) if prev_end else end

        bottlenecks = sorted(all_steps, key=lambda s: s.duration_s, reverse=True)[:_TOP_BOTTLENECK]
        return durations, bottlenecks, overlaps

    # --- schedule matrix -----------------------------------------------------------------

    def get_schedule_matrix(
        self, *, active_only: bool = True, window_days: int = 30, limit: int = 100, offset: int = 0
    ) -> tuple[list[ScheduleMatrixEntry], int] | UnsupportedResult:
        unsupported = self.require("chain_attr", "log_chain")
        if unsupported is not None:
            return unsupported

        where = ["OBJVERS = 'A'"]
        params: list[Any] = []
        if active_only:
            where.append("OBJSTAT = ?")
            params.append("ACT")
        base = self.dialect.build_select(
            columns=["CHAIN_ID"],
            from_logical="chain_attr",
            where=where,
            params=params,
            order_by=["CHAIN_ID"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        chain_ids = [str(r[0]) for r in rows]
        texts = self._chain_texts(chain_ids)
        run_info = self._run_summary(chain_ids, window_days)
        start_times = self._median_start_times(chain_ids, window_days)

        entries: list[ScheduleMatrixEntry] = []
        for chain_id in chain_ids:
            runs_per_day, frequency, _ = self._summarize_cadence(run_info.get(chain_id))
            typical_start = start_times.get(chain_id)
            p95_completion = self._p95_completion(chain_id, typical_start, window_days)
            entries.append(
                ScheduleMatrixEntry(
                    chain_id=chain_id,
                    description=texts.get(chain_id),
                    frequency=frequency,
                    runs_per_day=runs_per_day,
                    typical_start=typical_start,
                    p95_completion=p95_completion,
                    provenance=[self.provenance("log_chain", {"CHAIN_ID": chain_id})],
                )
            )
        return entries, total

    def _p95_completion(
        self, chain_id: str, typical_start: str | None, window_days: int
    ) -> str | None:
        if typical_start is None:
            return None
        runtimes = self.get_chain_runtimes(chain_id, days=window_days)
        if isinstance(runtimes, UnsupportedResult) or runtimes.duration_seconds.p95_s is None:
            return None
        try:
            start = datetime.strptime(typical_start, "%H:%M")
        except ValueError:
            return None
        completion = start + timedelta(seconds=runtimes.duration_seconds.p95_s)
        return completion.strftime("%H:%M")

    # --- shared helpers ------------------------------------------------------------------

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _chain_texts(self, chain_ids: list[str], *, langu: str = "E") -> dict[str, str]:
        if not chain_ids or not self.capability.is_available("chain_text"):
            return {}
        placeholders = ", ".join("?" for _ in chain_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["CHAIN_ID", "TXTLG"],
                from_logical="chain_text",
                where=["OBJVERS = 'A'", "LANGU = ?", f"CHAIN_ID IN ({placeholders})"],
                params=[langu, *chain_ids],
            )
        )
        return {str(r[0]): str(r[1]) for r in rows if r[1]}

    def _run_summary(
        self, chain_ids: list[str], window_days: int
    ) -> dict[str, tuple[int, Any, Any]]:
        """Per-chain (run_count, min_datum, max_datum) over the window."""
        if not chain_ids or not self.capability.is_available("log_chain"):
            return {}
        cutoff = (date.today() - timedelta(days=window_days)).strftime("%Y%m%d")
        placeholders = ", ".join("?" for _ in chain_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["CHAIN_ID", "COUNT(*)", "MIN(DATUM)", "MAX(DATUM)"],
                from_logical="log_chain",
                where=[f"CHAIN_ID IN ({placeholders})", "DATUM >= ?"],
                params=[*chain_ids, cutoff],
                group_by=["CHAIN_ID"],
            )
        )
        return {str(r[0]): (int(r[1]), r[2], r[3]) for r in rows}

    def _median_start_times(self, chain_ids: list[str], window_days: int) -> dict[str, str]:
        if not chain_ids or not self.capability.is_available("log_chain"):
            return {}
        cutoff = (date.today() - timedelta(days=window_days)).strftime("%Y%m%d")
        placeholders = ", ".join("?" for _ in chain_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["CHAIN_ID", "ZEIT"],
                from_logical="log_chain",
                where=[f"CHAIN_ID IN ({placeholders})", "DATUM >= ?"],
                params=[*chain_ids, cutoff],
            )
        )
        seconds_by_chain: dict[str, list[int]] = defaultdict(list)
        for chain_id, zeit in rows:
            try:
                value = int(zeit)
            except (ValueError, TypeError):
                continue
            seconds_by_chain[str(chain_id)].append(
                (value // 10000) * 3600 + (value // 100 % 100) * 60
            )
        result: dict[str, str] = {}
        for chain_id, seconds in seconds_by_chain.items():
            median = int(statistics.median(seconds))
            result[chain_id] = f"{median // 3600:02d}:{median % 3600 // 60:02d}"
        return result

    @staticmethod
    def _summarize_cadence(
        info: tuple[int, Any, Any] | None,
    ) -> tuple[float | None, FrequencyClass, date | None]:
        if not info:
            return None, "unknown", None
        count, min_datum, max_datum = info
        first, last = _date_from_datum(min_datum), _date_from_datum(max_datum)
        span_days = max((last - first).days, 1) if first and last else 1
        runs_per_day = count / span_days if span_days else None
        frequency = _classify_frequency(runs_per_day, count)
        return (round(runs_per_day, 2) if runs_per_day is not None else None), frequency, last
