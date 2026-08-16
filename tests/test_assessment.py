"""The landscape assessment: does the score stay honest when the analysis could not be done?

That is the whole risk in a roll-up. Three of the nine analyses need metadata a release may lack or
a system outside BW, and any of them may examine no candidates. Every one of those contributes zero
findings, so a naive sum *raises* the score - it would peak on the landscape nobody could look at.

These assert the score is computed over assessed scenarios only, that each other case is reported
with its reason, and that the number is absent rather than perfect when nothing was assessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_server_sapbw.core.budget import BudgetExceeded
from mcp_server_sapbw.models.assessment import SEVERITY_WEIGHTS, grade_for
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.findings import Finding, ScenarioReport
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.assessment import ASSESSED_SCENARIOS, AssessmentService


def capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema="SAPHANADB",
        tables={"transformation": TableStatus(logical_name="transformation", present=True)},
        discovered_at=datetime.now(UTC),
    )


def finding(severity: str, scenario: str = "9.1") -> Finding:
    return Finding(
        scenario=scenario,
        severity=severity,  # type: ignore[arg-type]
        title=f"a {severity} problem",
        recommendation="do something about it",
    )


class FakeAnalyzers:
    """Returns a scripted result per scenario id."""

    def __init__(self, results: dict[str, Any]) -> None:
        self._results = results
        self.calls: list[str] = []

    def run_scenario(self, scenario: str, *, limit: int = 50) -> Any:
        self.calls.append(scenario)
        result = self._results.get(scenario)
        if result is None:
            return ScenarioReport(scenario=scenario, title=scenario, analyzed_count=5)
        if isinstance(result, Exception):
            raise result
        return result


def service(results: dict[str, Any]) -> AssessmentService:
    return AssessmentService(FakeAnalyzers(results), capability(), "qa")  # type: ignore[arg-type]


# --- coverage governs the score -----------------------------------------------------------


def test_a_clean_landscape_scores_full_marks() -> None:
    report = service({}).assess()
    assert report.coverage.complete is True
    assert report.provisional is False
    assert report.score == 100
    assert report.grade == "A"
    assert report.total_findings == 0


def test_findings_deduct_by_published_weight() -> None:
    report = service(
        {
            "9.1": ScenarioReport(
                scenario="9.1",
                title="t",
                analyzed_count=10,
                findings=[finding("critical"), finding("medium")],
            )
        }
    ).assess()
    expected = 100 - SEVERITY_WEIGHTS["critical"] - SEVERITY_WEIGHTS["medium"]
    assert report.score == round(expected)
    outcome = next(o for o in report.outcomes if o.scenario == "9.1")
    assert outcome.deduction == pytest.approx(
        SEVERITY_WEIGHTS["critical"] + SEVERITY_WEIGHTS["medium"]
    )


def test_the_total_is_the_sum_of_the_per_scenario_deductions() -> None:
    """Decomposable, so a disputed grade can be recomputed rather than argued about."""
    report = service(
        {
            "9.1": ScenarioReport(
                scenario="9.1", title="t", analyzed_count=3, findings=[finding("high")]
            ),
            "9.3": ScenarioReport(
                scenario="9.3", title="t", analyzed_count=3, findings=[finding("low", "9.3")]
            ),
        }
    ).assess()
    assert report.score == round(100 - sum(o.deduction for o in report.outcomes))


def test_info_findings_cost_nothing() -> None:
    report = service(
        {
            "9.1": ScenarioReport(
                scenario="9.1", title="t", analyzed_count=3, findings=[finding("info")]
            )
        }
    ).assess()
    assert report.score == 100
    assert report.total_findings == 1


# --- the failure modes that would otherwise inflate the score ------------------------------


def test_an_unsupported_scenario_is_excluded_rather_than_counted_clean() -> None:
    report = service(
        {
            "9.1": UnsupportedResult(
                missing=["RSTRAN"], release="BW 7.50", detail="RSTRAN not available"
            )
        }
    ).assess()
    outcome = next(o for o in report.outcomes if o.scenario == "9.1")
    assert outcome.status == "unsupported"
    assert outcome.contributed_evidence is False
    assert outcome.deduction == 0.0
    assert report.provisional is True
    assert report.coverage.assessed == len(ASSESSED_SCENARIOS) - 1
    assert "9.1" in report.coverage.gaps
    assert any("does not carry the metadata" in c for c in report.caveats)


def test_a_scenario_with_no_candidates_is_not_a_pass() -> None:
    """analyzed_count == 0 means an absence of subjects, which is not an absence of problems."""
    report = service({"9.4": ScenarioReport(scenario="9.4", title="t", analyzed_count=0)}).assess()
    outcome = next(o for o in report.outcomes if o.scenario == "9.4")
    assert outcome.status == "nothing_to_assess"
    assert outcome.contributed_evidence is False
    assert any("absence of subjects, not a" in c for c in report.caveats)


def test_a_connector_gated_scenario_is_reported_as_such() -> None:
    report = service(
        {
            "9.7": ScenarioReport(
                scenario="9.7",
                title="t",
                analyzed_count=4,
                connector_required="a BI platform inventory",
                findings=[finding("info", "9.7")],
            )
        }
    ).assess()
    outcome = next(o for o in report.outcomes if o.scenario == "9.7")
    assert outcome.status == "connector_required"
    assert outcome.contributed_evidence is False
    assert any("outside BW" in c for c in report.caveats)


def test_a_failed_scenario_never_reads_as_a_clean_one() -> None:
    report = service({"9.5": RuntimeError("boom")}).assess()
    outcome = next(o for o in report.outcomes if o.scenario == "9.5")
    assert outcome.status == "failed"
    assert outcome.reason is not None
    assert "RuntimeError" in outcome.reason
    assert outcome.contributed_evidence is False


def test_no_evidence_at_all_means_no_score_rather_than_a_perfect_one() -> None:
    """The headline case: a score here would be indistinguishable from a clean bill of health."""
    unsupported = UnsupportedResult(missing=["X"], release="BW 7.50", detail="absent")
    report = service(dict.fromkeys(ASSESSED_SCENARIOS, unsupported)).assess()
    assert report.score is None
    assert report.grade is None
    assert report.coverage.assessed == 0
    assert any("not a clean result" in c for c in report.caveats)


def test_partial_coverage_is_declared_an_upper_bound() -> None:
    unsupported = UnsupportedResult(missing=["X"], release="BW 7.50", detail="absent")
    report = service({"9.1": unsupported, "9.3": unsupported}).assess()
    assert report.provisional is True
    assert any("upper bound" in c for c in report.caveats)


# --- budget behaviour ----------------------------------------------------------------------


def test_budget_exhaustion_stops_the_run_and_says_so() -> None:
    """The remaining analyses would all fail too, so they are reported, not silently omitted."""
    exhausted = BudgetExceeded(reason="query budget exhausted", queries=10, elapsed_seconds=1.0)
    report = service({ASSESSED_SCENARIOS[1]: exhausted}).assess()

    hit = next(o for o in report.outcomes if o.scenario == ASSESSED_SCENARIOS[1])
    assert hit.status == "failed"
    later = [o for o in report.outcomes if o.scenario in ASSESSED_SCENARIOS[2:]]
    assert later and all(o.status == "failed" for o in later)
    assert all(o.reason and "budget" in o.reason for o in later)
    assert any("budget was spent partway" in c for c in report.caveats)
    assert len(report.outcomes) == len(ASSESSED_SCENARIOS)


# --- shape and traceability ----------------------------------------------------------------


def test_every_scenario_is_reported_exactly_once() -> None:
    report = service({}).assess()
    assert [o.scenario for o in report.outcomes] == list(ASSESSED_SCENARIOS)


def test_every_non_assessed_outcome_states_a_reason() -> None:
    """An unexplained gap is the thing this model exists to prevent."""
    unsupported = UnsupportedResult(missing=["X"], release="BW 7.50", detail="absent")
    report = service(
        {
            "9.1": unsupported,
            "9.4": ScenarioReport(scenario="9.4", title="t", analyzed_count=0),
            "9.5": RuntimeError("boom"),
        }
    ).assess()
    for outcome in report.outcomes:
        if not outcome.contributed_evidence:
            assert outcome.reason, outcome.scenario


def test_top_findings_are_carried_whole_with_their_evidence() -> None:
    report = service(
        {
            "9.1": ScenarioReport(
                scenario="9.1",
                title="t",
                analyzed_count=3,
                findings=[finding("low"), finding("critical")],
            )
        }
    ).assess()
    assert report.top_findings[0].severity == "critical"
    assert report.worst_severity == "critical"
    assert report.top_findings[0].recommendation


def test_the_score_basis_publishes_the_weights() -> None:
    """A grade nobody can recompute is not decomposable."""
    report = service({}).assess()
    assert "critical" in report.score_basis
    assert "assessed scenarios only" in report.score_basis
    assert any("stated convention, not a measurement" in c for c in report.caveats)


def test_truncation_is_reported_as_a_lower_bound() -> None:
    report = service(
        {
            "9.1": ScenarioReport(
                scenario="9.1",
                title="t",
                analyzed_count=9,
                truncated=True,
                findings=[finding("low")],
            )
        }
    ).assess()
    assert any("lower bounds" in c for c in report.caveats)


def test_the_score_never_goes_below_zero() -> None:
    many = [finding("critical") for _ in range(50)]
    report = service(
        {"9.1": ScenarioReport(scenario="9.1", title="t", analyzed_count=99, findings=many)}
    ).assess()
    assert report.score == 0
    assert report.grade == "E"


@pytest.mark.parametrize(
    ("score", "expected"),
    [(100, "A"), (90, "A"), (89, "B"), (75, "B"), (60, "C"), (40, "D"), (0, "E")],
)
def test_grade_bands(score: int, expected: str) -> None:
    assert grade_for(score) == expected
