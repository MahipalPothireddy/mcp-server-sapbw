"""Tests for the vendor-neutral BI connector and the two scenarios it unlocks.

Scenarios 9.7 (report schedules vs feeding-chain completion) and 9.8 (dashboards bypassing BW) were
the only two that could not produce a finding for anybody: they need metadata BW does not hold, and
the original design named Tableau and BOBJ specifically. Reading an exported inventory instead makes
them work for any BI platform, and — unlike a vendor API client — it is testable offline.

What is checked here is that they *populate*, that the safety-margin and shared-view classifications
are right, and that an unmatched or malformed input degrades into a stated gap rather than a
confident wrong answer.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server_sapbw.connectors.base import ConnectorRegistry, unpopulated_reason
from mcp_server_sapbw.connectors.bi import (
    BiDashboardSource,
    BiReportSchedule,
    FileBiConnector,
)
from mcp_server_sapbw.models.findings import ScenarioReport
from mcp_server_sapbw.models.provenance import UnsupportedResult
from tests.bi_landscape import Analyzers, ScriptedConnection, capability

_INVENTORY = {
    "platform": "Power BI",
    "reports": [
        {"name": "Late Extract", "provider": "SALES_DSO", "scheduled_start": "06:10"},
        {"name": "Safe Extract", "provider": "SALES_DSO", "scheduled_start": "09:00"},
        {"name": "Unmatched Extract", "provider": "NO_SUCH_PROVIDER", "scheduled_start": "07:00"},
        {"name": "No Schedule", "provider": "SALES_DSO"},
    ],
    "dashboards": [
        {"name": "Shared Margin", "source_object": "PKG/SHARED_CV", "source_kind": "calc_view"},
        {"name": "Lonely KPI", "source_object": "PKG/PRIVATE_CV", "source_kind": "calc_view"},
        {"name": "Via BW", "source_object": "SALES_CP", "source_kind": "bw_provider"},
    ],
}


def _write(tmp_path: Path, payload: object, name: str = "inventory.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- the connector itself -------------------------------------------------------------------


def test_loads_a_json_inventory(tmp_path: Path) -> None:
    connector = FileBiConnector(_write(tmp_path, _INVENTORY))
    assert connector.is_configured() is True
    assert connector.platform() == "Power BI"
    assert len(connector.report_schedules()) == 4
    assert len(connector.dashboard_sources()) == 3
    assert "Power BI" in connector.status().detail


def test_loads_a_yaml_inventory(tmp_path: Path) -> None:
    path = tmp_path / "inventory.yaml"
    path.write_text(
        "platform: SAP Analytics Cloud\nreports:\n  - name: R1\n    provider: P1\n"
        '    scheduled_start: "05:00"\n',
        encoding="utf-8",
    )
    connector = FileBiConnector(path)
    assert connector.platform() == "SAP Analytics Cloud"
    assert connector.report_schedules()[0].name == "R1"


def test_no_path_means_not_configured() -> None:
    connector = FileBiConnector()
    assert connector.is_configured() is False
    assert connector.report_schedules() == []
    assert "no BI inventory configured" in connector.status().detail


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    connector = FileBiConnector(tmp_path / "absent.json")
    assert connector.is_configured() is False
    assert "does not exist" in connector.status().detail


def test_malformed_file_degrades_to_a_stated_gap(tmp_path: Path) -> None:
    """A half-populated analysis would be worse than a documented gap."""
    path = tmp_path / "broken.json"
    path.write_text("{not json at all", encoding="utf-8")
    connector = FileBiConnector(path)
    assert connector.is_configured() is False
    assert "could not be parsed" in connector.status().detail


def test_parse_failure_does_not_echo_the_path(tmp_path: Path) -> None:
    """Inventory paths can be operator-sensitive; the reason names the problem, not the location."""
    path = tmp_path / "secret-location.json"
    path.write_text("{oops", encoding="utf-8")
    detail = FileBiConnector(path).status().detail
    assert "secret-location" not in detail


def test_unknown_keys_in_an_export_are_ignored(tmp_path: Path) -> None:
    """A real export carries extra columns; rejecting the file over them would be useless."""
    payload = {
        "platform": "Qlik",
        "exported_at": "2026-01-01",  # not a field this server knows
        "reports": [
            {"name": "R1", "provider": "P1", "scheduled_start": "05:00", "workbook_id": "abc"}
        ],
        "dashboards": [],
    }
    connector = FileBiConnector(_write(tmp_path, payload))
    assert connector.is_configured() is True
    assert connector.platform() == "Qlik"
    assert connector.report_schedules()[0].name == "R1"


# --- registry: capability rather than product ------------------------------------------------


class _Configured:
    kind = "bi"

    def is_configured(self) -> bool:
        return True

    def status(self) -> object:  # pragma: no cover - not used by these assertions
        return None

    def platform(self) -> str | None:
        return "Looker"

    def report_schedules(self) -> list[BiReportSchedule]:
        return []

    def dashboard_sources(self) -> list[BiDashboardSource]:
        return []


def test_registry_resolves_the_bi_capability() -> None:
    registry = ConnectorRegistry([_Configured()])  # type: ignore[list-item]
    assert registry.bi() is not None
    assert registry.bi_unpopulated_reason() is None


def test_empty_registry_explains_what_a_bi_connector_would_unlock() -> None:
    reason = ConnectorRegistry().bi_unpopulated_reason()
    assert reason is not None
    assert reason == unpopulated_reason("bi")
    # The reason must name platforms generally, not one vendor.
    for platform in ("Tableau", "Power BI", "Looker"):
        assert platform in reason


def test_an_existing_tableau_configuration_still_satisfies_the_capability() -> None:
    """Backwards compatibility: 'tableau'/'bobj' remain valid kinds for the BI capability."""

    class _Tableau(_Configured):
        kind = "tableau"

    registry = ConnectorRegistry([_Tableau()])  # type: ignore[list-item]
    assert registry.bi() is not None
    assert registry.bi_unpopulated_reason() is None


# --- 9.7 and 9.8 now produce findings --------------------------------------------------------


def _schedule(tmp_path: Path) -> ScenarioReport:
    """9.7 for the inventory landscape, with the unsupported branch ruled out once."""
    report = _analyzers_with_bi(tmp_path).schedule_risk(limit=10)
    assert not isinstance(report, UnsupportedResult)
    return report


def _dashboards(tmp_path: Path) -> ScenarioReport:
    """9.8 for the inventory landscape."""
    report = _analyzers_with_bi(tmp_path).dashboards_on_calc_views(limit=10)
    assert not isinstance(report, UnsupportedResult)
    return report


def _analyzers_with_bi(tmp_path: Path) -> Analyzers:
    """Analyzers over a scripted BW landscape plus a BI inventory."""
    connector = FileBiConnector(_write(tmp_path, _INVENTORY))
    return Analyzers(
        ScriptedConnection(),
        capability(),
        None,
        registry=ConnectorRegistry([connector]),
    )


def test_schedule_risk_populates_per_report(tmp_path: Path) -> None:
    """The regression: 9.7 could only ever return the 'connector required' placeholder."""
    report = _schedule(tmp_path)
    assert report.scenario == "9.7"
    assert report.connector_required is None  # it is populated, not gated
    titles = [f.title for f in report.findings]
    assert any("Late Extract" in t for t in titles)
    assert any("Safe Extract" in t for t in titles)


def test_schedule_risk_flags_the_report_that_starts_too_early(tmp_path: Path) -> None:
    report = _schedule(tmp_path)
    by_report = {f.metrics["report"]: f for f in report.findings}
    late = by_report["Late Extract"]  # starts 06:10, chain p95 completes 06:40
    assert late.metrics["safety_margin_minutes"] < 0
    assert late.severity == "critical"
    assert "event-based" in late.recommendation
    safe = by_report["Safe Extract"]  # starts 09:00, comfortably after
    assert safe.metrics["safety_margin_minutes"] > 0
    assert safe.severity == "low"


def test_schedule_risk_excludes_what_it_cannot_match_and_says_so(tmp_path: Path) -> None:
    """An unmatched report must not be silently dropped nor assumed safe."""
    report = _schedule(tmp_path)
    reported = {f.metrics["report"] for f in report.findings}
    assert "Unmatched Extract" not in reported  # provider resolves to no chain
    assert "No Schedule" not in reported  # no start time to compare
    assert any("could not be matched" in c for c in report.caveats)
    assert any("assumed safe" in c for c in report.caveats)


def test_dashboards_distinguishes_shared_from_separate_views(tmp_path: Path) -> None:
    """Mission 9.8: both are findings, and which applies changes the advice."""
    report = _dashboards(tmp_path)
    assert report.scenario == "9.8"
    assert report.connector_required is None
    by_dashboard = {f.metrics["dashboard"]: f for f in report.findings}

    shared = by_dashboard["Shared Margin"]
    assert shared.metrics["path"] == "shared_view"
    assert shared.metrics["shared_with_bw_providers"] == ["SALES_CP"]
    assert shared.severity == "high"
    assert shared.detail is not None and "both" in shared.detail

    separate = by_dashboard["Lonely KPI"]
    assert separate.metrics["path"] == "separate_view"
    assert separate.metrics["shared_with_bw_providers"] == []
    assert separate.severity == "medium"
    assert separate.detail is not None and "diverge" in separate.detail


def test_dashboards_reading_bw_directly_are_not_bypasses(tmp_path: Path) -> None:
    report = _dashboards(tmp_path)
    assert "Via BW" not in {f.metrics["dashboard"] for f in report.findings}
    assert any("not bypasses" in c for c in report.caveats)


def test_findings_name_the_platform_they_came_from(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    assert schedule.findings[0].metrics["platform"] == "Power BI"
    assert any("Power BI" in c for c in schedule.caveats)
