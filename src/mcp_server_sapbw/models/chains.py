"""Domain models for process chains (B3).

Covers chain structure (processes + event-linked edges + nested sub-chains), runtime statistics
(min/median/mean/p95/max, success rate, bottleneck steps, observed overlaps over a measured window),
observed-cadence frequency classification, and the schedule matrix. Every returned fact carries
provenance (mission Rule 3).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    provenance: Provenance | list[Provenance]


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
    observed_overlap_runs: int = 0
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
    note: str | None = None
    provenance: Provenance | list[Provenance]


class LoadedProvider(BaseModel):
    """A provider a chain loads, and the path by which the chain reaches it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type_code: str | None = None  # RSBKDTP target type code (ODSO/ADSO/CUBE/IOBJ/...)
    dtp_id: str | None = None
    via_subchain: str | None = None  # set when reached through a nested chain
    update_mode: str | None = None  # full / delta / init, from the DTP
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
    subchains_walked: list[str] = Field(default_factory=list)
    step_categories: dict[str, int] = Field(default_factory=dict)
    truncated_recursion: bool = False
    caveats: list[str] = Field(default_factory=list)


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
