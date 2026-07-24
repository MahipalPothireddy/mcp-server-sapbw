"""Risk-analyzer result models: Finding, Severity, and ScenarioReport (B9).

Every risk analyzer (mission Section 9) returns a :class:`ScenarioReport` — a titled collection of
:class:`Finding` objects, each carrying severity, the affected objects, evidence (provenance rows
actually read), and a recommended action. When a scenario needs metadata that lives outside
BW-on-HANA (ECC extractor source for 9.6, Tableau/BOBJ schedules for 9.7/9.8), the finding is still
emitted with ``unpopulated_reason`` naming the connector required, so a gap is documented rather
than guessed (mission Rules 2/3).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .provenance import Provenance

# Ordered from least to most severe; used for sorting findings in a report.
Severity = Literal["info", "low", "medium", "high", "critical"]
SEVERITY_ORDER: tuple[Severity, ...] = ("info", "low", "medium", "high", "critical")


def severity_rank(severity: Severity) -> int:
    """Numeric rank of a severity (higher = more severe), for sorting."""
    return SEVERITY_ORDER.index(severity)


class Finding(BaseModel):
    """One risk finding: what, how bad, which objects, the evidence, and what to do.

    ``evidence`` holds the provenance of the metadata rows the finding was derived from — never a
    guess. ``unpopulated_reason`` is set only when an external connector is required but absent
    (the finding names the gap instead of inventing data). ``metrics`` carries scenario-specific
    scalars (e.g. safety-margin minutes, stream count, layer depth) for machine consumption.
    """

    model_config = ConfigDict(extra="forbid")

    scenario: str  # "9.1", "9.3", "9.7", "layer_violation", ...
    severity: Severity
    title: str
    affected_objects: list[str] = Field(default_factory=list)
    evidence: list[Provenance] = Field(default_factory=list)
    recommendation: str
    detail: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    # Set when an external-system connector (ECC / Tableau / BOBJ) is required but not configured.
    unpopulated_reason: str | None = None


class ScenarioReport(BaseModel):
    """The result of running one scenario analyzer: its findings plus scope and gap metadata.

    ``analyzed_count`` is how many candidate objects were examined (so an empty ``findings`` list
    is distinguishable from "nothing was analyzed"); ``truncated`` is set when the analysis hit its
    object cap; ``connector_required`` names the external system when the scenario could not be
    fully populated from BW alone; ``caveats`` records scope limits and documented gaps.
    """

    model_config = ConfigDict(extra="forbid")

    scenario: str
    title: str
    findings: list[Finding] = Field(default_factory=list)
    finding_count: int = 0
    analyzed_count: int = 0
    truncated: bool = False
    connector_required: str | None = None
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_counts_and_order(self) -> ScenarioReport:
        # Sort findings most-severe first and keep finding_count authoritative.
        self.findings.sort(key=lambda f: severity_rank(f.severity), reverse=True)
        self.finding_count = len(self.findings)
        return self
