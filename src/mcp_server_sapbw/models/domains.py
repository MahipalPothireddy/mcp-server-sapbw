"""Whether this system's dictionary still declares the codes the server was built to decode (D44).

**Why this exists.** The capability resolver validates *table existence*. Below it sit about fifty
code-to-meaning tables - element types, layout codes, alert levels, aggregation behaviour, request
status, rule types - every one of them read from a live BW 7.50 dictionary at development time and
then frozen into Python source. Nothing re-read them, and the dictionary's own fixed-value tables
were not even declared as tables. So a decode carrying ``confidence="dictionary"`` made a claim
about the *development* system while appearing to make one about the connected system.

**The failure mode is not hypothetical here.** D24 was an incomplete ``RSZLAYTP`` map: the eight
values it did not cover fell silently into a catch-all bucket, and on the reference system that was
24.4% of every element-tree edge. Nothing failed, nothing warned, and the numbers looked plausible.
That is the shape of the risk - not an error, a quiet wrong answer.

**What this reports, and what it deliberately does not.** It answers one crisp question: *does the
connected system's dictionary still declare what the source assumes?* It does **not** check whether
a code that is still declared has come to mean something different - no metadata can answer that -
nor whether values occur in data that the domain never declared, which is a separate question about
data rather than about the dictionary. Both limits travel in the report rather than being left to be
discovered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance

#: What a finding says. Ordered roughly by how much it should worry a reader.
#:
#: ``domain_absent``  - the domain is not in this system's dictionary at all, so every code the
#:                      source decodes from it is unvalidated here.
#: ``code_unmapped``  - the dictionary declares a value the source does not map. **This is the D24
#:                      failure mode**: an unmapped value does not raise, it lands in whatever
#:                      fallback the decode has and is reported as though it were understood.
#: ``code_undeclared``- the source maps a code the dictionary does not declare. Usually benign and
#:                      sometimes correct on purpose (this landscape uses two ranking operators SAP
#:                      does not declare), but it means the label rests on observation.
#: ``label_differs``  - the dictionary's text for a code is not the text recorded in source. Low on
#:                      its own; it matters when the source label is shown to a reader as SAP's.
DomainFindingKind = Literal["domain_absent", "code_unmapped", "code_undeclared", "label_differs"]

DomainFindingSeverity = Literal["high", "medium", "low", "info"]


class DomainFinding(BaseModel):
    """One disagreement between the connected dictionary and what the source assumes."""

    model_config = ConfigDict(extra="forbid")

    kind: DomainFindingKind
    severity: DomainFindingSeverity
    domain: str
    #: The column this domain sits behind, so a reader can tell what an answer would be wrong about.
    column: str
    #: The source symbol that freezes the mapping - where the fix goes.
    owner: str
    code: str | None = None
    #: What the source has recorded for this code, when it has anything.
    expected: str | None = None
    #: What the connected dictionary says, when it says anything.
    actual: str | None = None
    detail: str


class DomainCheck(BaseModel):
    """One domain's result: what was expected, what the dictionary holds, and what differs."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    column: str
    owner: str
    present: bool
    #: ``False`` when the source deliberately maps only part of the domain. An unmapped value is
    #: then expected rather than a finding, and is reported at ``info`` so it stays visible either -
    #: the D26 ``SUBDEFTP`` map covers 4 of 14 declared values on purpose, and pretending that is a
    #: defect would bury the cases that are.
    claims_complete: bool
    declared_codes: int
    mapped_codes: int
    findings: list[DomainFinding] = Field(default_factory=list)


class DomainDriftReport(BaseModel):
    """Whether the frozen code decodes still match the connected system's dictionary (D44)."""

    model_config = ConfigDict(extra="forbid")

    system: str
    bw_release: str
    #: Where the frozen decodes were originally read. Naming it is the point: a clean report means
    #: "this system agrees with that one", which is a different statement from "these decodes are
    #: right".
    decoded_against: str = "BW 7.50 (the development reference system)"
    domains_checked: int = 0
    domains_absent: int = 0
    checks: list[DomainCheck] = Field(default_factory=list)
    findings: list[DomainFinding] = Field(default_factory=list)
    #: Always populated. A clean report is a meaningful result here, so the limits of the check have
    #: to travel with it rather than only appearing when something is wrong.
    limitations: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when nothing above ``info`` was found."""
        return not any(f.severity != "info" for f in self.findings)
