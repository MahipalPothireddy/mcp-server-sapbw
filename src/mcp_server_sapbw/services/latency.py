"""Shared timing math for the latency scenarios (B9, task 23).

Scenario 9.7 compares a downstream report's scheduled start against the p95 completion of the chain
feeding its provider and flags negative or sub-30-minute safety margins. Scenario 9.1 compares how
often a full-update load runs against how often the master-data object it looks up is refreshed,
flagging the case where new data is enriched against stale master data.

This module holds only pure, side-effect-free math so it is trivially testable offline; the
analyzers supply the times and frequencies from the chains repository (and, for report schedules,
a connector).
"""

from __future__ import annotations

from ..models.chains import FrequencyClass
from ..models.findings import Severity

# Below this many minutes between a feeding chain's p95 completion and a downstream report's start,
# the report risks running before its data is ready (mission scenario 9.7 / Requirement 22.2).
MIN_SAFE_MARGIN_MINUTES = 30

_MINUTES_PER_DAY = 24 * 60

# Approximate runs-per-day for each observed cadence, used to compare refresh frequencies. Coarser
# (fewer runs/day) master data feeding a more frequent consumer is the 9.1 stale-data risk.
FREQUENCY_RUNS_PER_DAY: dict[FrequencyClass, float] = {
    "hourly": 24.0,
    "multiple_daily": 3.0,
    "daily": 1.0,
    "weekly": 1.0 / 7.0,
    "monthly": 1.0 / 30.0,
    "irregular": 0.0,
    "unknown": 0.0,
}


def parse_hhmm(value: str | None) -> int | None:
    """Minutes since midnight for an ``"HH:MM"`` string, or ``None`` if unparseable."""
    if not value or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        hours, minutes = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):  # noqa: PLR2004 - clock bounds
        return None
    return hours * 60 + minutes


def safety_margin_minutes(consumer_start: str | None, feeder_completion: str | None) -> int | None:
    """Minutes between a feeder's p95 completion and the consumer's start (positive = safe).

    Both are wall-clock ``"HH:MM"`` values. The result is the same-day difference
    ``consumer_start - feeder_completion``; a negative value means the consumer starts before its
    data is ready. Without run dates this cannot resolve across-midnight cases, so callers treat the
    figure as advisory (recorded as a caveat by the analyzer).
    """
    start = parse_hhmm(consumer_start)
    done = parse_hhmm(feeder_completion)
    if start is None or done is None:
        return None
    return start - done


def classify_margin(margin: int | None) -> tuple[Severity, str]:
    """Map a safety margin (minutes) to a severity and a short reason."""
    if margin is None:
        return "info", "safety margin could not be computed (missing schedule or completion time)"
    if margin < 0:
        return (
            "critical",
            f"report starts {abs(margin)} min before its feeding chain completes (p95)",
        )
    if margin < MIN_SAFE_MARGIN_MINUTES:
        return "high", f"only {margin} min between chain completion (p95) and report start"
    return "low", f"{margin} min safety margin between chain completion (p95) and report start"


def is_stale_master_risk(consumer: FrequencyClass, looked_up: FrequencyClass) -> bool:
    """True when a looked-up object refreshes less often than the consuming load runs.

    Both frequencies must be known; ``irregular`` / ``unknown`` (0.0 runs/day) return ``False`` so
    the analyzer records an explicit gap rather than asserting a risk it cannot substantiate.
    """
    consumer_rate = FREQUENCY_RUNS_PER_DAY.get(consumer, 0.0)
    looked_up_rate = FREQUENCY_RUNS_PER_DAY.get(looked_up, 0.0)
    if consumer_rate <= 0.0 or looked_up_rate <= 0.0:
        return False
    return looked_up_rate < consumer_rate


def minutes_between_wrapped(earlier: int, later: int) -> int:
    """Forward minutes from ``earlier`` to ``later`` on a 24h clock (wraps past midnight)."""
    return (later - earlier) % _MINUTES_PER_DAY
