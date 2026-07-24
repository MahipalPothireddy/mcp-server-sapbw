"""Tests for the risk-analyzer result models (B9)."""

from __future__ import annotations

from mcp_server_sapbw.models.findings import (
    SEVERITY_ORDER,
    Finding,
    ScenarioReport,
    severity_rank,
)
from mcp_server_sapbw.models.provenance import Provenance


def _finding(scenario: str, severity: str, title: str) -> Finding:
    return Finding(
        scenario=scenario,
        severity=severity,  # type: ignore[arg-type]
        title=title,
        recommendation="do the thing",
    )


def test_severity_rank_orders_low_to_high() -> None:
    assert severity_rank("info") < severity_rank("medium") < severity_rank("critical")
    assert SEVERITY_ORDER[-1] == "critical"


def test_scenario_report_sorts_findings_by_severity_desc() -> None:
    report = ScenarioReport(
        scenario="9.3",
        title="t",
        findings=[
            _finding("9.3", "low", "a"),
            _finding("9.3", "critical", "b"),
            _finding("9.3", "medium", "c"),
        ],
    )
    assert [f.severity for f in report.findings] == ["critical", "medium", "low"]


def test_scenario_report_syncs_finding_count() -> None:
    report = ScenarioReport(
        scenario="9.1",
        title="t",
        findings=[_finding("9.1", "high", "x"), _finding("9.1", "low", "y")],
        finding_count=99,  # deliberately wrong; validator must correct it
    )
    assert report.finding_count == 2


def test_finding_carries_evidence_and_optionals() -> None:
    finding = Finding(
        scenario="9.6",
        severity="low",
        title="enhancement",
        affected_objects=["DS_ONE"],
        evidence=[Provenance(source_table="RSDSSEGFD", source_key={"DATASOURCE": "DS_ONE"})],
        recommendation="review",
        metrics={"zy_field_count": 5},
        unpopulated_reason="requires an ECC connector",
    )
    assert finding.evidence[0].source_table == "RSDSSEGFD"
    assert finding.metrics["zy_field_count"] == 5
    assert finding.unpopulated_reason is not None
