"""Domain models for process chains (B3).

Covers chain structure (processes + event-linked edges + nested sub-chains), runtime statistics
(min/median/mean/p95/max, success rate, bottleneck steps, observed overlaps over a measured window),
observed-cadence frequency classification, and the schedule matrix. Every returned fact carries
provenance (mission Rule 3).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .completeness import Completeness, bounded
from .evidence import Evidence, evidence_for
from .provenance import Provenance

# Observed-cadence frequency buckets (derived from RSPCLOGCHAIN run history, never from names).
FrequencyClass = Literal[
    "hourly",
    "multiple_daily",
    "daily",
    "weekly",
    "monthly",
    "irregular",
    "unknown",
]

# Event link colour on a chain edge (RSPCCHAIN EVENTP_GREEN vs EVENTP_RED).
LinkKind = Literal["green", "red"]


class ChainProcess(BaseModel):
    """One process node in a chain (a row of RSPCCHAIN)."""

    model_config = ConfigDict(extra="forbid")

    key: str  # composite "TYPE:VARIANTE"
    process_type: str  # TYPE
    variant: str  # VARIANTE
    line_no: int | None = None  # LNR
    is_subchain: bool = False  # TYPE == 'CHAIN' (a nested/meta sub-chain reference)
    subchain_id: str | None = None  # VARIANTE when is_subchain
    provenance: Provenance


class ChainEdge(BaseModel):
    """A directed success/error link between two processes in a chain."""

    model_config = ConfigDict(extra="forbid")

    source_key: str
    target_key: str
    link: LinkKind
    provenance: Provenance


class ChainSummary(BaseModel):
    """Compact chain entry for list results."""

    model_config = ConfigDict(extra="forbid")

    chain_id: str
    description: str | None = None
    active: bool = True
    application: str | None = None
    frequency: FrequencyClass = "unknown"
    runs_per_day: float | None = None
    last_run: date | None = None
    provenance: Provenance | list[Provenance]


class Chain(BaseModel):
    """Full chain structure: processes, event-linked edges, and nested sub-chains."""

    model_config = ConfigDict(extra="forbid")

    chain_id: str
    description: str | None = None
    active: bool = True
    application: str | None = None
    processes: list[ChainProcess] = Field(default_factory=list)
    edges: list[ChainEdge] = Field(default_factory=list)
    subchain_ids: list[str] = Field(default_factory=list)
    subchains: list[Chain] = Field(default_factory=list)  # resolved recursively (cycle-guarded)
    truncated_recursion: bool = False  # True if a cycle or max depth stopped resolution
    #: Which bound stopped nesting, where the flag said only that one did (D6).
    completeness: Completeness = Field(default_factory=Completeness)
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _reconcile_recursion_bound(self) -> Chain:
        if not self.completeness.is_complete:
            self.truncated_recursion = True
        elif self.truncated_recursion:
            self.completeness = bounded("recursion_limit", scope="subchains")
        return self


class DurationStats(BaseModel):
    """Duration distribution (seconds) over the runs in the window."""

    model_config = ConfigDict(extra="forbid")

    count: int = 0
    min_s: float | None = None
    median_s: float | None = None
    mean_s: float | None = None
    p95_s: float | None = None
    max_s: float | None = None


class StepRuntime(BaseModel):
    """A single (bottleneck) step's observed duration within a run."""

    model_config = ConfigDict(extra="forbid")

    process_type: str
    variant: str
    instance: str | None = None
    duration_s: float
    state: str | None = None
    #: The dictionary text for ``state`` (domain ``RSPC_STATE``). Decoded once in the repository so
    #: no consumer has to know that ``R`` means "Ended with errors" - every place that re-derived it
    #: would be a place to get it wrong.
    state_label: str | None = None
    #: True only for a state that genuinely means failure. **Not** simply "state is
    #: not G or F": "Skipped at restart" and "Active" sit outside the success set
    #: and are not failures - on the reference system they account for 103,347 steps
    #: inside otherwise-green runs (D65).
    failed: bool = False
    provenance: Provenance


class FailedStep(BaseModel):
    """A step that failed, aggregated over the window rather than listed run by run.

    **Why aggregated.** The question a reader has is "what keeps breaking", and the answer is a step
    identity plus how often. On the reference system one DTP accounts for 37 of a
    chain's failures, so
    a flat list of the five most recent failing step *rows* would print that one DTP five times and
    name none of the others. Aggregating by ``(process_type, variant)`` makes the list say something
    different from the bottleneck list rather than being a re-sort of it.

    ``longest_s`` earns its place on evidence rather than for completeness: on the S06 validation
    subject the failing DTPs ran for roughly 23.5 hours before dying, and that number is what turns
    "a step failed" into "a step hangs for a day and then fails" - two different problems.
    """

    model_config = ConfigDict(extra="forbid")

    process_type: str
    variant: str
    #: Raw ``RSPCPROCESSLOG.STATE`` and its dictionary text, so a reader can see both the code they
    #: would search for in BW and what it means.
    state: str
    state_label: str
    #: How many times this step was seen in this state within the measured window.
    occurrences: int = 1
    #: Longest and shortest observed run of this failing step, in seconds. ``None`` when the step
    #: carried no usable timestamps - which is itself informative: an "Active" step never has an end
    #: time, and a step that was killed may not either.
    longest_s: float | None = None
    shortest_s: float | None = None
    #: A run in which this step failed, so the finding can be opened in RSPC directly.
    example_log_id: str | None = None
    provenance: Provenance


class ChainRuntimes(BaseModel):
    """Runtime statistics for a chain over the measured window.

    The window is measured from the earliest run actually available within the requested days, so
    the reported window never overstates coverage (mission Known Limitation 5).
    """

    model_config = ConfigDict(extra="forbid")

    chain_id: str
    window_days_requested: int
    window_days_actual: int
    window_start: date | None = None
    total_runs: int = 0
    successful_runs: int = 0
    success_rate: float | None = None
    duration_seconds: DurationStats = Field(default_factory=DurationStats)
    bottleneck_steps: list[StepRuntime] = Field(default_factory=list)
    #: The steps that actually **failed**, aggregated by identity and ordered by how often (D65).
    #:
    #: Distinct from ``bottleneck_steps``, which ranks by *duration* - and that difference is the
    #: defect this field fixes. A reader chasing a failure was handed the slowest step, which on a
    #: chain that fails fast is a different step entirely. The two coincided on the validation
    #: subject only because its failing loads hung for ~23.5 hours before dying, which hid the gap.
    failed_steps: list[FailedStep] = Field(default_factory=list)
    #: Steps seen in a state that is neither success nor failure - "Skipped at restart", "Active",
    #: "Planned". Counted rather than listed, because the number's job is to stop the failure count
    #: being read as "everything else succeeded". On the reference system skipped steps alone appear
    #: in 32,405 runs whose own status is green.
    indeterminate_steps: int = 0
    #: Total steps examined, so a zero failure count is distinguishable from nothing
    #: having been read at all.
    steps_examined: int = 0
    #: Runs of **this chain** that began before its own previous run finished. A real and serious
    #: finding on its own - a daily chain still running when the next starts - but **not**
    #: contention
    #: with other chains, which is what mission Known Limitation 6 asks for and what the name used
    #: to
    #: be read as (D64). Renamed so the number and its meaning cannot drift apart again.
    self_overlap_runs: int = 0
    #: Runs of this chain whose wall-clock window overlapped a run of a *different* chain, and which
    #: other chains those were. This is the contention signal: it is why a duration is not an
    #: intrinsic property of a chain. Empty list with a zero count means no contention was observed;
    #: ``contention_measured=False`` means it was not looked for.
    contended_runs: int = 0
    contending_chains: list[str] = Field(default_factory=list)
    contention_measured: bool = False
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class ChainCadence(BaseModel):
    """Observed cadence of a chain, and whether it is still alive *for that cadence*.

    Two signals are combined because each fails alone. Runs-per-day misclassifies a sparse chain
    that ran twice in a year; the **median gap between distinct run days** is robust to that, and is
    what BW cannot tell you directly. ``intraday`` (more than one run on the same day) is the
    precondition for the stale-master-data risk in scenario 9.1.

    Liveness is cadence-aware: a monthly chain that last ran five weeks ago is healthy, whereas a
    daily chain that did is not. ``active`` compares ``days_since_last_run`` against the window for
    its own band rather than one global threshold.

    ``reference_date`` is the latest run date observed in the system, never today's date and never a
    constant - a QA copy or a frozen system would otherwise look uniformly dead.
    """

    model_config = ConfigDict(extra="forbid")

    chain_id: str
    frequency: FrequencyClass = "unknown"
    median_gap_days: float | None = None  # median gap between distinct run days
    runs_per_day: float | None = None
    intraday: bool = False  # more than one run on the same day
    run_count: int = 0
    run_days: int = 0  # distinct days on which it ran
    first_run: date | None = None
    last_run: date | None = None
    reference_date: date | None = None  # latest run date in the system
    days_since_last_run: int | None = None
    liveness_window_days: int | None = None  # window appropriate to this band
    active: bool | None = None  # ran within its own band's window
    confidence: Literal["high", "low"] = "high"  # 'low' for single-run / too-few-runs chains
    evidence: Evidence | None = None
    note: str | None = None
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_evidence(self) -> ChainCadence:
        if self.evidence is None:
            detail = (
                f"Classified as {self.frequency} from {self.run_count} run(s) across "
                f"{self.run_days} distinct day(s) in the retained log window"
                + (
                    f", median gap {self.median_gap_days} day(s)."
                    if self.median_gap_days is not None
                    else "."
                )
            )
            self.evidence = evidence_for("cadence", self.confidence, detail=detail)
        return self


class LoadedProvider(BaseModel):
    """A provider a chain loads, and the path by which the chain reaches it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type_code: str | None = None  # RSBKDTP target type code (ODSO/ADSO/CUBE/IOBJ/...)
    dtp_id: str | None = None
    via_subchain: str | None = None  # set when reached through a nested chain
    update_mode: str | None = None  # full / delta / init, from the DTP
    provenance: Provenance


class InactiveLoader(BaseModel):
    """A transformation that targets a provider but exists only at a **non-active** version (D33).

    **Why this is worth a model rather than a caveat.** Mission Rule 6 reads the active version
    only, and rightly: a loader that is not active does not run, so it cannot be reported as
    loading anything. But the *silence* that follows is misleading. On the reference system, three
    sales-history cubes hold **1,081,275 rows between them, last loaded 2,442 to 3,191 days ago**,
    and each has exactly one inbound transformation carrying ``OBJVERS='R'`` with ``OBJSTAT='ACT'``.
    None of the three has a DTP row at *any* version, so the loading-chain reader finds nothing and
    says the object "may be loaded by an InfoPackage, filled by a routine, or be virtual" - three
    innocent guesses, none of them the truth, which is "it was loaded by a transformation from a
    decommissioned source and is retained deliberately".

    The distinction matters because of what a reader does next. Told a populated provider has no
    loader, the reasonable inference is that it is orphaned - and the owner confirmed these are
    deliberately kept legacy sales history, so acting on that inference would have deleted a million
    rows of it. Reporting the frozen loader converts "nothing loads this" into "nothing loads this
    *now*, and here is what used to".

    ``R`` is worth noting on its own: it is **not a declared value** of domain ``RSOBJVERS``, whose
    fixed values are A, D, H, M, N and T. The version is therefore reported raw and undecoded rather
    than labelled, because this server does not invent a meaning for a code SAP does not document.
    """

    model_config = ConfigDict(extra="forbid")

    #: Transformation id, so the object can be opened in BW directly.
    tran_id: str
    #: Raw ``OBJVERS``. Reported uninterpreted - ``R`` is undeclared in the domain, so a label would
    #: be a guess.
    objvers: str
    #: ``OBJSTAT``. The combination that makes this a finding rather than noise is a non-active
    #: version carrying ``ACT``: an unactivated BW content transformation (``D``/``INA``) is
    #: ordinary and covers 6,223 targets on the reference system, where this narrow case covers 576.
    objstat: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    provenance: Provenance


class LoadClosure(BaseModel):
    """The chain <-> provider closure: what a chain loads, or what loads a provider.

    Resolving this is what makes "when is this object's data current?" answerable, and it is not a
    single lookup: a chain reaches most of its loads through nested sub-chains, so the step graph
    has to be walked recursively (cycle-guarded).

    Exactly one direction is populated per call. ``step_categories`` counts the chain's steps by
    structural role (derived from process type codes, never from chain names) so housekeeping-only
    chains are distinguishable from data loads.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Literal["chain_to_providers", "provider_to_chains"]
    chain_id: str | None = None
    provider: str | None = None
    providers_loaded: list[LoadedProvider] = Field(default_factory=list)
    loading_chains: list[ChainCadence] = Field(default_factory=list)
    #: Transformations targeting this provider that exist only at a non-active version (D33).
    #: Populated **only** when no active loader was found, so it never competes with a real answer -
    #: it exists to replace a misleading silence, not to add noise to a working one.
    inactive_loaders: list[InactiveLoader] = Field(default_factory=list)
    subchains_walked: list[str] = Field(default_factory=list)
    step_categories: dict[str, int] = Field(default_factory=dict)
    truncated_recursion: bool = False
    #: Which bound stopped the walk, where ``truncated_recursion`` said only that one did (D6).
    #: Reconciled with it below, so the two cannot disagree.
    completeness: Completeness = Field(default_factory=Completeness)
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reconcile_recursion_bound(self) -> LoadClosure:
        if not self.completeness.is_complete:
            self.truncated_recursion = True
        elif self.truncated_recursion:
            self.completeness = bounded("recursion_limit", scope="subchains")
        return self


class ScheduleMatrixEntry(BaseModel):
    """One row of the schedule matrix: chain x frequency x typical start x p95 completion."""

    model_config = ConfigDict(extra="forbid")

    chain_id: str
    description: str | None = None
    frequency: FrequencyClass = "unknown"
    runs_per_day: float | None = None
    typical_start: str | None = None  # "HH:MM" (median observed start)
    p95_completion: str | None = None  # "HH:MM" (typical start + p95 duration)
    provenance: Provenance | list[Provenance]
