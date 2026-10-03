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
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil, floor
from statistics import median
from typing import Any, Literal

from ..core.capabilities import MAX_RUNTIME_WINDOW_DAYS
from ..core.dialect import like_term
from ..models.chains import (
    Chain,
    ChainCadence,
    ChainEdge,
    ChainProcess,
    ChainRuntimes,
    ChainSummary,
    DurationStats,
    FailedStep,
    FrequencyClass,
    ScheduleMatrixEntry,
    StepRuntime,
)
from ..models.provenance import Provenance, UnsupportedResult
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
#: Distinct failing (type, variant) steps named per chain. Aggregated rather than listed row by row:
#: on the reference system one DTP accounts for 37 of a chain's failures, so a flat
#: top-5 of step rows would print that one step five times and name none of the rest.
_TOP_FAILED = 5
_SUCCESS_STATUS = "G"  # RSPCLOGCHAIN ANALYZED_STATUS green (empirically confirmed)

# --- RSPCPROCESSLOG.STATE ------------------------------------------------------------------------
#
# **Dictionary-backed, not empirical.** ``DD03L`` types this column as domain ``RSPC_STATE`` and
# ``DD07T`` maintains English texts for all eleven values, so this decode is read from SAP's own
# dictionary rather than inferred from data. The caveat that used to say "step STATE decode is
# empirical" was wrong about this column and is corrected where it is raised.
#
# **Three classes, not two, and that distinction is the whole substance of the fix.** Defect D65
# proposed treating every state outside ``('G','F')`` as a failure. Measured on the reference system
# that is wrong by two orders of magnitude: ``S`` ("Skipped at restart") accounts for
# **102,895 steps** and ``A`` ("Active") for 452, neither a failure - a skipped step
# is a normal
# artefact of restarting a chain, and an active one has simply not finished. The decisive evidence
# is
# that **32,405 runs whose own ANALYZED_STATUS is green contain a step outside ('G','F')**, so the
# two-class rule would report failures inside a million successful runs.
_STEP_STATE_LABEL: dict[str, str] = {
    "": "Undefined",
    "A": "Active",
    "F": "Completed",
    "G": "Successfully completed",
    "J": "Ended with Error (for example, subsequent job missing)",
    "P": "Planned",
    "Q": "Released",
    "R": "Ended with errors",
    "S": "Skipped at restart",
    "X": "Canceled",
    "Y": "Ready",
}
#: States that mean the step finished cleanly.
_STEP_OK_STATES: frozenset[str] = frozenset({"G", "F"})
#: States that mean the step **failed**. Nothing else is called a failure: a skipped,
#: active, planned, released or ready step is reported as neither succeeded nor
#: failed, because that is what it is.
_STEP_FAILED_STATES: frozenset[str] = frozenset({"R", "J", "X"})


@dataclass
class _FailedAccumulator:
    """Running totals for one failing ``(process_type, variant)`` across the window."""

    state: str
    example_log_id: str
    provenance: Provenance
    occurrences: int = 0
    longest_s: float | None = None
    shortest_s: float | None = None

    def record(self, duration: float | None) -> None:
        """Count one occurrence. ``None`` duration still counts - the failure happened."""
        self.occurrences += 1
        if duration is None:
            return
        self.longest_s = duration if self.longest_s is None else max(self.longest_s, duration)
        self.shortest_s = duration if self.shortest_s is None else min(self.shortest_s, duration)

    def build(self, process_type: str, variant: str) -> FailedStep:
        return FailedStep(
            process_type=process_type,
            variant=variant,
            state=self.state,
            state_label=_STEP_STATE_LABEL.get(self.state, "unknown"),
            occurrences=self.occurrences,
            longest_s=self.longest_s,
            shortest_s=self.shortest_s,
            example_log_id=self.example_log_id,
            provenance=self.provenance,
        )


@dataclass
class _StepAnalysis:
    """Everything one read of the step log establishes.

    A record rather than a tuple. The tuple had already grown to four elements and every caller had
    to remember that the third was the self-overlap count and not the run windows; adding two more
    would have made a mis-ordered unpack a silent wrong answer rather than a type error.
    """

    durations: list[float] = field(default_factory=list)
    bottlenecks: list[StepRuntime] = field(default_factory=list)
    failed_steps: list[FailedStep] = field(default_factory=list)
    #: Distinct failing (type, variant) pairs seen, which may exceed the ``_TOP_FAILED`` reported.
    distinct_failed: int = 0
    indeterminate_steps: int = 0
    steps_examined: int = 0
    self_overlaps: int = 0
    run_windows: list[tuple[datetime, datetime]] = field(default_factory=list)


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

# Median-gap bands: (inclusive_max_gap_days, label). More robust than runs-per-day for sparse
# cadences, where a couple of runs in a long window skews the average.
_GAP_BANDS: tuple[tuple[float, FrequencyClass], ...] = (
    (1.5, "daily"),
    (4.0, "irregular"),  # between daily and weekly: no standard cadence
    (10.0, "weekly"),
    (24.0, "irregular"),
    (35.0, "monthly"),
)
_LONG_GAP_DAYS = 180.0  # a gap beyond this is effectively annual

# How long a chain may go unrun before it is presumed dormant, per band. A monthly chain needs a
# far longer window than a daily one, or every monthly chain reads as dead.
_LIVENESS_WINDOW_DAYS: dict[FrequencyClass, int] = {
    "hourly": 30,
    "multiple_daily": 90,
    "daily": 180,
    "weekly": 180,
    "monthly": 395,  # ~13 months, so a yearly-ish monthly chain is not misjudged
    "irregular": 395,
    "unknown": 395,
}
_INTRADAY_RPD = 1.5  # more than this many runs per run-day means it runs more than once a day
_MIN_RUNS_FOR_GAP = 2  # a single run yields no gap at all
#: Runs below which a 95th percentile is labelled rather than presented as a planning number (D63).
#: At n runs the p95 interpolates at index 0.95*(n-1), so for small n it sits against the maximum;
#: 20 is the point where the two are distinguishable enough to plan against.
_MIN_RUNS_FOR_PERCENTILE = 20
#: Other-chain start rows read when measuring contention (D64), and how many contenders are named in
#: the caveat and the payload. A busy 90-day window holds tens of thousands of runs, so the read is
#: capped and ordered; the full list is a landscape question for bw_get_schedule_matrix, not this
#: one.
_MAX_CONTENTION_ROWS = 60000
_MAX_CONTENDERS = 20
_NAMED_CONTENDERS = 3
_MAX_RUN_DAY_ROWS = 200000  # cap on (chain, run-day) rows pulled for median-gap computation
#: Chains per run-day read. Keeps the cap above from binding, which it demonstrably did when every
#: chain was read in one statement: 411,581 pairs on the reference system against a 200,000 cap, so
#: the alphabetically late half lost its run days and silently classified as ``unknown`` (D62).
_RUN_DAY_CHUNK = 25


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


def _classify_cadence(
    *,
    median_gap: float | None,
    runs_per_day: float | None,
    run_count: int,
    run_days: int,
) -> tuple[FrequencyClass, str | None, Literal["high", "low"]]:
    """Classify observed cadence, returning ``(frequency, note, confidence)``.

    The median gap between run days is the primary signal; runs-per-day only promotes a daily band
    to ``multiple_daily``/``hourly``. A chain with too few runs to establish a gap is reported as
    low confidence rather than being forced into a band.
    """
    if run_days <= 1:
        return "unknown", "single run day - too little history to establish a cadence", "low"
    if median_gap is None or run_count < _MIN_RUNS_FOR_GAP:
        return "unknown", "insufficient run history to establish a cadence", "low"

    band: FrequencyClass = "irregular"
    for max_gap, label in _GAP_BANDS:
        if median_gap <= max_gap:
            band = label
            break
    else:
        band = "monthly" if median_gap <= _LONG_GAP_DAYS else "irregular"

    note: str | None = None
    if band == "daily" and runs_per_day is not None:
        for threshold, label in _FREQUENCY_BUCKETS[:2]:  # hourly, multiple_daily
            if runs_per_day >= threshold:
                band = label
                break
    if band == "irregular":
        note = f"median gap {median_gap:.1f}d does not match a standard cadence band"
    confidence: Literal["high", "low"] = "high" if run_count >= _MIN_RUNS_FOR_FREQ else "low"
    return band, note, confidence


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
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ChainSummary], int] | UnsupportedResult:
        """Chains with their observed cadence.

        ``window_days`` used to be a parameter here and is deliberately gone rather than kept and
        ignored (D61). It bounded the run-history read, which made ``last_run`` report ``None`` for
        any chain dormant longer than the window - a wrong answer rather than a narrower one - and
        no caller ever passed it. A parameter accepted and disregarded is a worse contract than one
        that does not exist.
        """
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
        # The same derivation `get_cadence` uses, not a second one (D61). This listing ran its own
        # window-bounded summary, and the two disagreed about the same chains: a chain whose newest
        # run predated the window got no row, so it was reported `frequency='unknown'` with
        # `last_run=None` while the cadence reader called the identical chain `daily` and named the
        # date. Measured on the reference system: **81 chains reported `last_run=None` against
        # 23,789 recorded runs**, one with 2,290. "This chain has never run" and "this chain last
        # ran in 2023" are different facts and only the second was true.
        #
        # A window still governs *runtime statistics*, where it belongs, in `get_chain_runtimes`.
        cadences = self.get_cadence(chain_ids) if chain_ids else {}

        summaries: list[ChainSummary] = []
        for row in rows:
            chain_id, appl, objstat = str(row[0]), row[1], row[2]
            cadence = cadences.get(chain_id)
            summaries.append(
                ChainSummary(
                    chain_id=chain_id,
                    description=texts.get(chain_id),
                    active=(objstat == "ACT"),
                    application=str(appl) if appl else None,
                    frequency=cadence.frequency if cadence else "unknown",
                    runs_per_day=cadence.runs_per_day if cadence else None,
                    last_run=cadence.last_run if cadence else None,
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
        """Chain structure with sub-chains resolved. Cached (scope ``chain``).

        The key carries the recursion options, because they change the shape of the result.
        """
        return self.cached_model(
            "chain",
            f"{chain_id}|{int(resolve_subchains)}|{max_depth}",
            model=Chain,
            build=lambda: self._get_chain_uncached(chain_id, resolve_subchains, max_depth),
        )

    def _get_chain_uncached(
        self, chain_id: str, resolve_subchains: bool, max_depth: int
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
        """Runtime statistics over the retained window. Cached on the ``runtime`` tier.

        Runtime figures move with every run, so they use the runtime tier (hard-capped at one hour,
        mission Section 3) rather than the long structural TTL. RSPCPROCESSLOG is the largest table
        the server reads, so even a one-hour cache removes most of the cost of repeated questions
        about the same chain.
        """
        return self.cached_model(
            "chain_runtimes",
            f"{chain_id}|{days}",
            model=ChainRuntimes,
            build=lambda: self._get_chain_runtimes_uncached(chain_id, days),
            tier="runtime",
        )

    def _get_chain_runtimes_uncached(
        self, chain_id: str, days: int
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

        steps = self._step_analysis(log_ids, run_start_by_log)
        durations, run_spans = steps.durations, steps.run_windows

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
            "'bottleneck' = longest-running steps, not a full longest-path. The failed steps are a "
            "separate list, ranked by how often they failed, not by duration",
            # Corrected: STATE is *not* empirical. DD03L types it as domain RSPC_STATE and DD07T
            # maintains texts for all eleven values, so the step decode is read from SAP's own
            # dictionary. Only the run-level ANALYZED_STATUS remains an empirical reading.
            "run-level ANALYZED_STATUS decode is empirical; step STATE is decoded from its ABAP "
            "dictionary domain (RSPC_STATE)",
        ]
        if steps.distinct_failed > _TOP_FAILED:
            caveats.append(
                f"{steps.distinct_failed} distinct step(s) failed; the {_TOP_FAILED} most frequent "
                "are reported"
            )
        if steps.indeterminate_steps:
            caveats.append(
                f"{steps.indeterminate_steps} step(s) were in a state that is neither success nor "
                "failure (for example 'Skipped at restart' or 'Active'); they are counted but not "
                "reported as failures, so the failed list is not simply everything that did not "
                "succeed"
            )
        # A p95 over a handful of runs is arithmetic wearing the clothes of a statistic (D63). At
        # three runs the 95th percentile interpolates between the second and third values, so it is
        # the maximum by another name - and a reader planning a schedule against "p95" is entitled
        # to assume it means something. REQ-03 asks for such a percentile to be withheld *or
        # labelled*; labelling is chosen because the number is still the best available estimate and
        # withholding it leaves a caller nothing. What was not acceptable is the previous state:
        # computed, presented, and indistinguishable from a p95 over a thousand runs.
        #
        # Found by measuring the clause rather than the code, on a chain with three runs in ninety
        # days. Worth recording that my first check *passed* it: the matcher looked for any caveat
        # mentioning "run" and matched "longest-running steps". A sloppy test is how a real gap gets
        # certified as covered.
        if stats.p95_s is not None and stats.count < _MIN_RUNS_FOR_PERCENTILE:
            caveats.append(
                f"p95 is computed over only {stats.count} run(s); below "
                f"{_MIN_RUNS_FOR_PERCENTILE} runs a 95th percentile is not meaningfully distinct "
                f"from the maximum ({stats.max_s}s) - treat it as an upper estimate, not a "
                "planning percentile"
            )
        self_overlaps = steps.self_overlaps
        if self_overlaps:
            caveats.append(
                f"{self_overlaps} run(s) of this chain began before its own previous run finished; "
                "its durations overlap each other and cannot be read as independent samples"
            )
        # Contention with *other* chains, which is what mission Known Limitation 6 is about and what
        # the old single number was captioned as while measuring something else entirely (D64).
        contended, contending, measured = self._contention(chain_id, run_spans)
        if measured and contended:
            caveats.append(
                f"{contended} run(s) overlapped a run of another chain "
                f"({', '.join(contending[:_NAMED_CONTENDERS])}"
                + (", and others" if len(contending) > _NAMED_CONTENDERS else "")
                + "); durations reflect contention, not intrinsic cost"
            )
            # Said because measuring it showed the count is a weak discriminator on a busy system,
            # not because it is a nice disclaimer. The reference landscape runs ~35,000 chain
            # executions in 90 days, so nearly every run has *something* else start during it - both
            # chains checked came back at 88 of 88 and 3 of 3. A number that is almost always "all
            # of
            # them" earns its place through the names it carries, not through its ratio, and a
            # reader
            # comparing two chains on this figure would learn nothing. The stronger measure is the
            # *degree* of concurrency rather than its presence; that is recorded as an enhancement
            # rather than implied by a count that cannot support it.
            if contended == len(run_spans) and len(run_spans) > 1:
                caveats.append(
                    "every run in the window was contended, so this count does not distinguish "
                    "this chain from any other on a busy system; the named contending chains are "
                    "the useful part, and the degree of concurrency is not measured"
                )
        elif not measured:
            caveats.append(
                "contention with other chains could not be measured, so a duration here may still "
                "reflect competition for resources rather than the chain's own cost"
            )
        if total_runs > _MAX_RUNS_FOR_STEPS:
            caveats.append(
                f"duration stats and failed steps use the most recent {_MAX_RUNS_FOR_STEPS} of "
                f"{total_runs} runs, while the success rate covers all of them - so the two cannot "
                "be reconciled on a chain this frequent, and a step that only failed outside that "
                "window is not listed"
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
            bottleneck_steps=steps.bottlenecks,
            failed_steps=steps.failed_steps,
            indeterminate_steps=steps.indeterminate_steps,
            steps_examined=steps.steps_examined,
            self_overlap_runs=self_overlaps,
            contended_runs=contended,
            contending_chains=contending[:_MAX_CONTENDERS],
            contention_measured=measured,
            caveats=caveats,
            provenance=provenance,
        )

    def _step_analysis(
        self, log_ids: list[str], run_start_by_log: dict[str, datetime | None]
    ) -> _StepAnalysis:
        """Everything the step log says about these runs, from one read.

        Returns durations, the slowest steps, the **failed** steps, an indeterminate count, the
        steps examined, the self-overlap count and the run windows.

        The windows are returned rather than consumed here because contention with *other* chains
        has to be measured against them, a different question from this chain overlapping
        itself (D64). Keeping both means the two can never again be reported as one number.

        Failures are derived in this same loop rather than by a second statement: ``STATE`` was
        already selected and already kept on every ``StepRuntime``, so D65 was never a missing
        read - the duration ranking simply dropped the information on its way out.
        """
        if not log_ids:
            return _StepAnalysis()
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
        # Failures accumulate by (type, variant) so the list says "what keeps breaking" rather than
        # repeating one step. Keyed before the timestamp guard below, deliberately: a step that died
        # mid-run can have no usable end time, and on the reference system every "Active" step has
        # none at all - deriving failures after the guard would silently drop exactly the cases the
        # reader is looking for.
        failed: dict[tuple[str, str], _FailedAccumulator] = {}
        indeterminate = 0
        for row in step_rows:
            log_id, ptype, variant, instance, state, start_ts, end_ts = row
            code = str(state) if state is not None else ""
            start, end = _parse_bw_timestamp(start_ts), _parse_bw_timestamp(end_ts)
            # Narrowed explicitly rather than through a boolean flag: mypy cannot carry
            # "usable implies both are not None" across the failure accumulation below, and a cast
            # would be asserting what the type checker is asking us to prove.
            duration: float | None = None
            if start is not None and end is not None and end >= start:
                duration = round((end - start).total_seconds(), 1)

            if code in _STEP_FAILED_STATES:
                key = (str(ptype), str(variant))
                entry = failed.get(key)
                if entry is None:
                    entry = _FailedAccumulator(
                        state=code,
                        example_log_id=str(log_id),
                        provenance=self.provenance(
                            "process_log",
                            {"LOG_ID": str(log_id), "TYPE": str(ptype), "STATE": code},
                        ),
                    )
                    failed[key] = entry
                entry.record(duration)
            elif code not in _STEP_OK_STATES:
                # Skipped, active, planned, released, ready, undefined. Counted, never called a
                # failure - the count exists so a zero failure total is not read as "all fine".
                indeterminate += 1

            if start is None or end is None or duration is None:
                continue
            steps_by_log[str(log_id)].append((start, end))
            all_steps.append(
                StepRuntime(
                    process_type=str(ptype),
                    variant=str(variant),
                    instance=str(instance) if instance else None,
                    duration_s=duration,
                    state=code or None,
                    state_label=_STEP_STATE_LABEL.get(code),
                    failed=code in _STEP_FAILED_STATES,
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
        # Ordered by how often a step failed, then by how long it ran before failing. Frequency
        # first because "this breaks every night" outranks "this broke once for a long time", and
        # the
        # duration is the tie-break that distinguishes a fast error from a step that hangs.
        failures = sorted(
            (entry.build(ptype, variant) for (ptype, variant), entry in failed.items()),
            key=lambda f: (f.occurrences, f.longest_s or 0.0),
            reverse=True,
        )[:_TOP_FAILED]
        return _StepAnalysis(
            durations=durations,
            bottlenecks=bottlenecks,
            failed_steps=failures,
            distinct_failed=len(failed),
            indeterminate_steps=indeterminate,
            steps_examined=len(step_rows),
            self_overlaps=overlaps,
            run_windows=run_windows,
        )

    def _contention(
        self, chain_id: str, run_spans: list[tuple[datetime, datetime]]
    ) -> tuple[int, list[str], bool]:
        """Runs of this chain during which **another chain started** (D64).

        Mission Known Limitation 6 wants contention reported, because a duration measured while
        other
        work was running says more about the system than about the chain. The previous single
        ``observed_overlap_runs`` was captioned as contention and computed the opposite - this chain
        overlapping *itself* - so a chain whose p95 was twenty-three times its median reported
        **zero**
        while sharing its window with other chains on thousands of runs.

        **Named for precisely what it measures, which is the lesson of the defect it fixes.** The
        ideal
        signal is interval intersection against every other chain's full run window, and that is not
        affordable: a chain's end time lives only in the step log, the dialect expresses no joins,
        and
        reading every step in a 90-day window is on the order of half a million rows. What *is*
        cheap
        and exact is other chains' **start** times, one grouped read of the chain log. So the
        measure
        is "another chain started while this run was in progress", which under-counts a chain that
        was
        already running and kept running - and says so rather than implying full intersection.

        Returns ``(runs contended, the chains that started during them, whether it was measured)``.
        """
        if not run_spans or not self.capability.is_available("log_chain"):
            return 0, [], False
        earliest = min(start for start, _ in run_spans)
        latest = max(end for _, end in run_spans)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["CHAIN_ID", "DATUM", "ZEIT"],
                        from_logical="log_chain",
                        where=["CHAIN_ID <> ?", "DATUM >= ?", "DATUM <= ?"],
                        params=[
                            chain_id,
                            earliest.strftime("%Y%m%d"),
                            latest.strftime("%Y%m%d"),
                        ],
                        # Ordered because the read is capped (D8): an arbitrary slice would report a
                        # different contention count on each identical call.
                        order_by=["DATUM", "ZEIT", "CHAIN_ID"],
                    ),
                    limit=_MAX_CONTENTION_ROWS,
                )
            )
        except Exception:
            return 0, [], False

        starts: list[tuple[datetime, str]] = []
        for other_chain, datum, zeit in rows:
            started = _parse_dats_tims(datum, zeit)
            name = str(other_chain).strip()
            if started is not None and name:
                starts.append((started, name))
        if not starts:
            return 0, [], True
        starts.sort()

        contended = 0
        contenders: set[str] = set()
        for start, end in run_spans:
            # Bisect over the sorted start times rather than scanning them per run: 88 runs against
            # 35,000 other-chain starts is 3 million comparisons done naively, for a number nobody
            # would wait for.
            left = bisect_left(starts, (start, ""))
            right = bisect_left(starts, (end, ""))
            during = {name for _when, name in starts[left:right]}
            if during:
                contended += 1
                contenders.update(during)
        return contended, sorted(contenders), True

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

    # --- observed cadence ------------------------------------------------------------------

    def get_cadence(self, chain_ids: list[str] | None = None) -> dict[str, ChainCadence]:
        """Observed cadence per chain, keyed by chain id.

        Derived entirely from run history (``RSPCLOGCHAIN``): chain *names* are not evidence of
        schedule, and on real systems they contradict it. Reads one aggregate row per chain plus the
        distinct run days needed for the median gap.
        """
        if not self.capability.is_available("log_chain"):
            return {}
        reference = self._reference_date()
        summaries = self._run_day_summary(chain_ids)
        run_days, day_read_truncated = self._distinct_run_days(chain_ids)

        result: dict[str, ChainCadence] = {}
        for chain_id, (run_count, day_count, first_run, last_run) in summaries.items():
            days = sorted(run_days.get(chain_id, []))
            gaps = [(days[i] - days[i - 1]).days for i in range(1, len(days))]
            median_gap = median(gaps) if gaps else None
            runs_per_day = (run_count / day_count) if day_count else None
            frequency, note, confidence = _classify_cadence(
                median_gap=median_gap,
                runs_per_day=runs_per_day,
                run_count=run_count,
                run_days=day_count,
            )
            # A cadence derived from a truncated day list is not a cadence, it is an artefact of the
            # cap (D62). Said out loud rather than folded into the classification, because the whole
            # defect was a bound that changed the answer without leaving a trace: `run_days` looked
            # right, the median silently went missing, and the frequency degraded to "unknown".
            if chain_id in day_read_truncated:
                confidence = "low"
                note = (
                    f"the run-day read hit its {_MAX_RUN_DAY_ROWS:,}-row cap, so the median gap "
                    "and the cadence from it are a lower bound for this chain, not a measurement"
                    + (f". {note}" if note else "")
                )
            window = _LIVENESS_WINDOW_DAYS.get(frequency, 395)
            days_since = (reference - last_run).days if (reference and last_run) else None
            result[chain_id] = ChainCadence(
                chain_id=chain_id,
                frequency=frequency,
                median_gap_days=round(median_gap, 1) if median_gap is not None else None,
                runs_per_day=round(runs_per_day, 2) if runs_per_day is not None else None,
                intraday=bool(runs_per_day and runs_per_day > _INTRADAY_RPD),
                run_count=run_count,
                run_days=day_count,
                first_run=first_run,
                last_run=last_run,
                reference_date=reference,
                days_since_last_run=days_since,
                liveness_window_days=window,
                active=(days_since <= window) if days_since is not None else None,
                confidence=confidence,
                note=note,
                provenance=self.provenance("log_chain", {"CHAIN_ID": chain_id}),
            )
        return result

    def _reference_date(self) -> date | None:
        """Latest run date in the system - the only honest 'now' for a copied or frozen system."""
        rows = self.select(
            self.dialect.build_select(columns=["MAX(DATUM)"], from_logical="log_chain")
        )
        return _date_from_datum(rows[0][0]) if rows and rows[0][0] is not None else None

    def _run_day_summary(
        self, chain_ids: list[str] | None
    ) -> dict[str, tuple[int, int, date | None, date | None]]:
        where, params = self._chain_filter(chain_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=[
                    "CHAIN_ID",
                    "COUNT(DISTINCT LOG_ID)",
                    "COUNT(DISTINCT DATUM)",
                    "MIN(DATUM)",
                    "MAX(DATUM)",
                ],
                from_logical="log_chain",
                where=where,
                params=params,
                group_by=["CHAIN_ID"],
            )
        )
        out: dict[str, tuple[int, int, date | None, date | None]] = {}
        for chain_id, run_count, day_count, first_run, last_run in rows:
            key = str(chain_id).strip()
            if key:
                out[key] = (
                    int(run_count or 0),
                    int(day_count or 0),
                    _date_from_datum(first_run),
                    _date_from_datum(last_run),
                )
        return out

    def _distinct_run_days(
        self, chain_ids: list[str] | None
    ) -> tuple[dict[str, list[date]], set[str]]:
        """Distinct run days per chain, plus the chains whose read hit the row cap (D62).

        **Read in chunks, because one read for every chain silently changed the answer.** This
        pulled every ``(chain, day)`` pair in a single capped statement ordered by chain, and the
        reference system holds **411,581 such pairs against a 200,000 cap**. So the cap bound, and
        everything sorting after the 200,000th row - which fell about two thirds of the way through
        the customer-namespace chains - came back with no run days at all. No gaps meant no median
        gap, and ``_classify_cadence`` turns a missing median into ``"unknown"``.

        That made cadence **depend on how many chains you asked about**. Measured on the S03
        subject: asked alone it returned ``daily`` with a median gap of 1.0; asked as one of 280 it
        returned ``unknown`` with no median at all, from identical data. It is the D8 bounded-read
        class with a sharper edge - a cap returning an arbitrary subset is bad, and one that
        silently changes a *classification* is worse, because nothing downstream can tell.

        Chunked so the cap cannot bind on a realistic population: the longest history on the
        reference system is about 5,500 run days, so a chunk of 25 chains tops out near 137,500
        rows. Any chunk that still hits the cap is returned in the second element rather than
        discarded quietly, so the caller can degrade its confidence instead of reporting a
        fabricated cadence.
        """
        out: dict[str, list[date]] = defaultdict(list)
        truncated: set[str] = set()
        batches: list[list[str] | None] = (
            [chain_ids[i : i + _RUN_DAY_CHUNK] for i in range(0, len(chain_ids), _RUN_DAY_CHUNK)]
            if chain_ids
            else [None]
        )
        for batch in batches:
            where, params = self._chain_filter(batch)
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["CHAIN_ID", "DATUM"],
                        from_logical="log_chain",
                        where=where,
                        params=params,
                        group_by=["CHAIN_ID", "DATUM"],
                        order_by=["CHAIN_ID", "DATUM"],
                    ),
                    limit=_MAX_RUN_DAY_ROWS,
                )
            )
            if len(rows) >= _MAX_RUN_DAY_ROWS:
                truncated.update(batch or [])
            for chain_id, datum in rows:
                parsed = _date_from_datum(datum)
                key = str(chain_id).strip()
                if key and parsed is not None:
                    out[key].append(parsed)
        return dict(out), truncated

    def _chain_filter(self, chain_ids: list[str] | None) -> tuple[list[str], list[Any]]:
        if not chain_ids:
            return [], []
        placeholders = ", ".join("?" for _ in chain_ids)
        return [f"CHAIN_ID IN ({placeholders})"], list(chain_ids)
