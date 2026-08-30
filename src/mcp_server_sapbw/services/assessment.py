"""Roll the ten risk analyses into one assessment, and keep it honest about what it saw.

The mechanical part is easy: run each analyzer, sum the findings, deduct points. The part that
matters is the bookkeeping around it, because a roll-up is where an absence of evidence most easily
turns into evidence of health.

Three of the ten analyses need something the landscape may not offer - metadata a release does not
carry, or a system outside BW - and any of them may examine no candidates because none exist. Summed
naively, every one of those cases contributes zero findings and therefore *raises* the score. The
score would peak on the landscape nobody could look at.

So each scenario is recorded with the reason it contributed what it did, the score is computed over
the assessed ones only, and coverage travels with the number wherever it goes.
"""

from __future__ import annotations

from ..core.budget import BudgetExceeded
from ..models.assessment import (
    SEVERITY_WEIGHTS,
    AssessmentCoverage,
    LandscapeAssessment,
    ScenarioOutcome,
    ScenarioStatus,
    grade_for,
)
from ..models.capability import CapabilityRecord
from ..models.completeness import COMPLETE
from ..models.findings import Finding, ScenarioReport, Severity, severity_rank
from ..models.provenance import UnsupportedResult
from .analyzers import SCENARIO_TITLES, Analyzers

#: The analyses a full assessment runs. ``9.2`` is deliberately absent: ``layer_violations``
#: already reports deep DSO stacks, so counting both would deduct twice for one structural fact.
ASSESSED_SCENARIOS: tuple[str, ...] = (
    "9.1",
    "9.3",
    "9.4",
    "9.5",
    "9.6",
    "9.7",
    "9.8",
    "layer_violations",
    "unused_providers",
)

#: How many findings are carried whole in the headline, most severe first.
_TOP_FINDINGS = 10

_MAX_SCORE = 100


class AssessmentService:
    """Runs :data:`ASSESSED_SCENARIOS` and composes a :class:`LandscapeAssessment`."""

    def __init__(self, analyzers: Analyzers, capability: CapabilityRecord, system: str) -> None:
        self._analyzers = analyzers
        self._capability = capability
        self._system = system

    def assess(self, *, limit_per_scenario: int = 25) -> LandscapeAssessment:
        outcomes: list[ScenarioOutcome] = []
        findings: list[Finding] = []
        stopped_on_budget = False

        for scenario in ASSESSED_SCENARIOS:
            if stopped_on_budget:
                outcomes.append(
                    self._outcome(
                        scenario,
                        "failed",
                        reason="the per-call budget was spent before this analysis ran",
                    )
                )
                continue
            try:
                result = self._analyzers.run_scenario(scenario, limit=limit_per_scenario)
            except BudgetExceeded as exc:
                stopped_on_budget = True
                outcomes.append(
                    self._outcome(
                        scenario,
                        "failed",
                        reason=f"the per-call budget was spent during this analysis ({exc.reason})",
                    )
                )
                continue
            except Exception as exc:  # a broken analysis must not silently read as a clean one
                outcomes.append(
                    self._outcome(
                        scenario, "failed", reason=f"{type(exc).__name__} while analysing"
                    )
                )
                continue

            outcome = self._classify(scenario, result)
            outcomes.append(outcome)
            if isinstance(result, ScenarioReport) and outcome.contributed_evidence:
                findings.extend(result.findings)

        return self._compose(outcomes, findings, stopped_on_budget=stopped_on_budget)

    # --- classification -------------------------------------------------------------------

    def _classify(
        self, scenario: str, result: ScenarioReport | UnsupportedResult
    ) -> ScenarioOutcome:
        if isinstance(result, UnsupportedResult):
            return self._outcome(scenario, "unsupported", reason=result.detail)

        by_severity: dict[str, int] = {}
        for finding in result.findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

        if result.connector_required:
            return self._outcome(
                scenario,
                "connector_required",
                reason=(
                    f"needs {result.connector_required}, which is outside BW; the BW half of this "
                    "analysis ran but cannot be completed here"
                ),
                result=result,
                by_severity=by_severity,
            )
        if result.analyzed_count == 0:
            return self._outcome(
                scenario,
                "nothing_to_assess",
                reason=(
                    "the analysis ran and found no candidates to examine, so this is an absence "
                    "of subjects rather than an absence of problems"
                ),
                result=result,
                by_severity=by_severity,
            )
        return self._outcome(scenario, "assessed", result=result, by_severity=by_severity)

    def _outcome(
        self,
        scenario: str,
        status: ScenarioStatus,
        *,
        reason: str | None = None,
        result: ScenarioReport | None = None,
        by_severity: dict[str, int] | None = None,
    ) -> ScenarioOutcome:
        counts = by_severity or {}
        scoring = status in ("assessed",)
        deduction = _deduction(counts) if scoring else 0.0
        return ScenarioOutcome(
            scenario=scenario,
            title=(result.title if result else SCENARIO_TITLES.get(scenario, scenario)),
            status=status,
            reason=reason,
            finding_count=result.finding_count if result else 0,
            by_severity=counts,
            analyzed_count=result.analyzed_count if result else 0,
            # Forwarded whole, so a scenario's bound survives the roll-up. A score computed over
            # bounded analyses is an upper bound, which only stays visible if the bound does (D6).
            completeness=(result.completeness if result else COMPLETE),
            deduction=round(deduction, 2),
        )

    # --- composition ----------------------------------------------------------------------

    def _compose(
        self,
        outcomes: list[ScenarioOutcome],
        findings: list[Finding],
        *,
        stopped_on_budget: bool,
    ) -> LandscapeAssessment:
        assessed = [o for o in outcomes if o.contributed_evidence]
        not_assessed: dict[str, int] = {}
        for outcome in outcomes:
            if not outcome.contributed_evidence:
                not_assessed[outcome.status] = not_assessed.get(outcome.status, 0) + 1

        coverage = AssessmentCoverage(
            assessed=len(assessed),
            total=len(outcomes),
            not_assessed=not_assessed,
            gaps=[o.scenario for o in outcomes if not o.contributed_evidence],
        )

        by_severity: dict[str, int] = {}
        for finding in findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

        ranked = sorted(findings, key=lambda f: severity_rank(f.severity), reverse=True)
        worst: Severity | None = ranked[0].severity if ranked else None

        score: int | None = None
        grade: str | None = None
        if assessed:
            total_deduction = sum(o.deduction for o in assessed)
            score = max(0, min(_MAX_SCORE, round(_MAX_SCORE - total_deduction)))
            grade = grade_for(score)

        return LandscapeAssessment(
            system=self._system,
            bw_release=self._capability.bw_release,
            score=score,
            grade=grade,
            provisional=not coverage.complete,
            score_basis=_score_basis(),
            coverage=coverage,
            findings_by_severity=by_severity,
            worst_severity=worst,
            total_findings=len(findings),
            outcomes=outcomes,
            top_findings=ranked[:_TOP_FINDINGS],
            caveats=_caveats(coverage, outcomes, stopped_on_budget=stopped_on_budget),
        )


def _deduction(by_severity: dict[str, int]) -> float:
    return sum(SEVERITY_WEIGHTS.get(sev, 0.0) * count for sev, count in by_severity.items())


def _score_basis() -> str:
    weights = ", ".join(
        f"{sev} -{int(w) if w.is_integer() else w}" for sev, w in SEVERITY_WEIGHTS.items() if w > 0
    )
    return (
        f"100 minus a weighted count of findings ({weights}; info costs nothing), floored at 0, "
        "over the assessed scenarios only. Each scenario reports its own deduction so the total "
        "can be recomputed. An unassessed scenario contributes nothing in either direction."
    )


def _caveats(
    coverage: AssessmentCoverage,
    outcomes: list[ScenarioOutcome],
    *,
    stopped_on_budget: bool,
) -> list[str]:
    caveats: list[str] = []
    if not coverage.assessed:
        caveats.append(
            "no scenario produced evidence, so there is no score. This is not a clean result - it "
            "means the analysis could not be performed here. Read the per-scenario reasons."
        )
    elif not coverage.complete:
        caveats.append(
            f"the score covers {coverage.assessed} of {coverage.total} analyses. The remaining "
            f"{coverage.total - coverage.assessed} contributed no evidence, so the score is an "
            "upper bound: problems they would have found are not deducted."
        )
    unsupported = [o.scenario for o in outcomes if o.status == "unsupported"]
    if unsupported:
        caveats.append(
            f"{', '.join(unsupported)} could not run because this release does not carry the "
            "metadata they read. bw_capability_report says which objects are missing, and "
            "bw_access_report distinguishes that from a missing grant."
        )
    connector = [o.scenario for o in outcomes if o.status == "connector_required"]
    if connector:
        caveats.append(
            f"{', '.join(connector)} need a system outside BW. Their BW half ran; the answer is "
            "completed by configuring the connector, not by reading more BW metadata."
        )
    empty = [o.scenario for o in outcomes if o.status == "nothing_to_assess"]
    if empty:
        caveats.append(
            f"{', '.join(empty)} examined no candidates. That is an absence of subjects, not a "
            "pass, and it is excluded from the score rather than counted as clean."
        )
    truncated = [o.scenario for o in outcomes if o.truncated]
    if truncated:
        caveats.append(
            f"{', '.join(truncated)} hit an object cap, so their finding counts are lower bounds. "
            "Raise the per-scenario limit or run them individually for the remainder."
        )
    if stopped_on_budget:
        caveats.append(
            "the per-call budget was spent partway through, so later analyses did not run. Raise "
            "SAPBW_MAX_QUERIES_PER_CALL / SAPBW_MAX_SECONDS_PER_CALL or lower limit_per_scenario."
        )
    caveats.append(
        "severity weights are published in the score basis so the grade can be recomputed and "
        "disputed. They are a stated convention, not a measurement."
    )
    return caveats
