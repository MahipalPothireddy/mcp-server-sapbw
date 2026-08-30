"""Landscape assessment: the ten risk analyses rolled up, with the coverage that qualifies it.

**The question this answers.** Ten analyses exist and each ran on its own, so "how healthy is this
landscape?" meant ten calls and a mental merge. An executive summary is a legitimate ask.

**Why the score carries coverage everywhere it goes.** A roll-up is the easiest place in this whole
project to produce a confident falsehood. Three of the ten analyses cannot run without metadata that
some releases lack or that lives outside BW entirely, and a fourth may examine no candidates because
none exist. Summing findings across whatever happened to run yields a high score for a landscape
nobody managed to look at - the score would be highest exactly when the analysis was weakest.

So every scenario is classified by *why* it contributed what it did:

* ``assessed`` - ran, and examined at least one candidate. The only status that earns a score.
* ``nothing_to_assess`` - ran and examined nothing, because no candidate exists. Not a pass.
* ``unsupported`` - this release does not carry the metadata. No evidence either way.
* ``connector_required`` - the answer needs a system outside BW.
* ``failed`` - errored; what it would have found is unknown.

``score`` is computed over ``assessed`` scenarios only, states its formula, and is ``provisional``
whenever coverage is short of complete. It is ``None`` - never zero, never a default - when nothing
was assessed, because a number there would be indistinguishable from a clean bill of health.

**No confidence percentage.** Consistent with :class:`~.analysis.AnalysisConfidence`, which
deliberately carries a level and component counts rather than a percentage: a single number cannot
say whether it is low because the landscape is bad or because the evidence is thin, and those two
call for opposite actions. Here the score answers the first and coverage answers the second, and
they are never blended.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .completeness import BoundedResult
from .findings import Finding, Severity

#: Why a scenario contributed what it did. Only ``assessed`` is evidence about the landscape.
ScenarioStatus = Literal[
    "assessed",
    "nothing_to_assess",
    "unsupported",
    "connector_required",
    "failed",
]

#: Statuses that contribute evidence, and therefore a score.
SCORING_STATUSES: frozenset[str] = frozenset({"assessed"})

#: Points deducted per finding. Published rather than tuned: a score whose weights are private is
#: not decomposable, and a customer disputing a grade has to be able to recompute it.
SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 12.0,
    "high": 6.0,
    "medium": 2.0,
    "low": 0.5,
    "info": 0.0,
}

#: Band labels. The number stays alongside; the letter is a convenience, not a replacement.
GRADE_BANDS: tuple[tuple[int, str], ...] = (
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (40, "D"),
    (0, "E"),
)


def grade_for(score: int) -> str:
    """The band label for a score."""
    for floor, label in GRADE_BANDS:
        if score >= floor:
            return label
    return GRADE_BANDS[-1][1]  # pragma: no cover - the last band floors at 0


class ScenarioOutcome(BoundedResult):
    """What one analysis contributed, and why."""

    scenario: str = Field(min_length=1)
    title: str
    status: ScenarioStatus
    #: Why, when the status is not ``assessed``. Required for those by a validator's contract in
    #: the service: an unexplained gap is the thing this model exists to prevent.
    reason: str | None = None
    finding_count: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    #: Candidates examined. Zero with ``assessed`` is impossible by construction; zero is what
    #: makes ``nothing_to_assess`` distinguishable from a clean result.
    analyzed_count: int = 0
    #: Points this scenario removed from the score, so the total is decomposable per analysis.
    deduction: float = 0.0

    @property
    def contributed_evidence(self) -> bool:
        return self.status in SCORING_STATUSES


class AssessmentCoverage(BaseModel):
    """How much of the intended analysis actually happened."""

    model_config = ConfigDict(extra="forbid")

    assessed: int = 0
    total: int = 0
    #: Counts by the status of everything that was not assessed, so a gap is attributable.
    not_assessed: dict[str, int] = Field(default_factory=dict)
    #: Scenario ids that produced no evidence, named so the gap is actionable rather than a count.
    gaps: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.assessed == self.total

    @property
    def fraction(self) -> float:
        return (self.assessed / self.total) if self.total else 0.0


class LandscapeAssessment(BaseModel):
    """The ten analyses rolled up: what was found, over how much, and how the score was reached."""

    model_config = ConfigDict(extra="forbid")

    system: str
    bw_release: str
    #: 0-100 over the assessed scenarios. ``None`` when nothing was assessed - never zero, because
    #: zero and "we could not look" are opposite readings.
    score: int | None = None
    grade: str | None = None
    #: True whenever coverage is short of complete. A provisional score is still useful; a
    #: provisional score presented as final is not.
    provisional: bool = True
    #: The formula in words, so the number can be recomputed and disputed.
    score_basis: str = ""
    coverage: AssessmentCoverage = Field(default_factory=AssessmentCoverage)
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    worst_severity: Severity | None = None
    total_findings: int = 0
    outcomes: list[ScenarioOutcome] = Field(default_factory=list)
    #: The most severe findings, carried whole so the headline is traceable to evidence.
    top_findings: list[Finding] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
