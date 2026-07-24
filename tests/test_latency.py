"""Tests for the shared latency math (B9)."""

from __future__ import annotations

from mcp_server_sapbw.services import latency


def test_parse_hhmm() -> None:
    assert latency.parse_hhmm("08:30") == 8 * 60 + 30
    assert latency.parse_hhmm("00:00") == 0
    assert latency.parse_hhmm(None) is None
    assert latency.parse_hhmm("25:00") is None
    assert latency.parse_hhmm("noon") is None


def test_safety_margin_minutes() -> None:
    assert latency.safety_margin_minutes("09:00", "08:30") == 30
    assert latency.safety_margin_minutes("08:00", "08:30") == -30
    assert latency.safety_margin_minutes(None, "08:30") is None


def test_classify_margin_thresholds() -> None:
    assert latency.classify_margin(-10)[0] == "critical"
    assert latency.classify_margin(15)[0] == "high"
    assert latency.classify_margin(90)[0] == "low"
    assert latency.classify_margin(None)[0] == "info"


def test_is_stale_master_risk() -> None:
    # Consumer runs more often than the looked-up object refreshes -> risk.
    assert latency.is_stale_master_risk("multiple_daily", "daily") is True
    assert latency.is_stale_master_risk("hourly", "daily") is True
    # Same or finer refresh -> no risk.
    assert latency.is_stale_master_risk("daily", "daily") is False
    assert latency.is_stale_master_risk("daily", "hourly") is False
    # Unknown / irregular cadence -> no assertion of risk (documented gap).
    assert latency.is_stale_master_risk("daily", "unknown") is False
    assert latency.is_stale_master_risk("unknown", "daily") is False
