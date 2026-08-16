"""The support matrix: which tool works on which BW release, answerable without connecting.

**Why this exists separately from the capability contract.** The contract answers "what does this
server do with metadata object X", per object, at build time. ``bw_capability_report`` crosses that
with one system's discovery result. Both are useful and neither answers the question a customer asks
before installing anything:

    *I run BW 7.4 (or BW/4HANA). Which of your tools will work on my landscape?*

Three things stand between the contract and that answer, and this module supplies all three.

**The unit is wrong.** A customer thinks in tools, not metadata tables. Nobody asks whether
``RSPCPROCESSLOG`` is present; they ask whether ``bw_get_chain_runtimes`` works. So the matrix is
keyed by tool, and ``ToolSupport.requires`` is the bridge - **measured**, by attributing each read
to the tool that caused it. A hand-written mapping across 55 tools would drift the first time a tool
gained a reader, and drift silently, with nothing to contradict it.

**It needs a connection.** Every existing answer does. This one is built from data shipped in the
package, so it answers before a profile exists.

**There is no release dimension.** The contract records a single ``validated_on`` release. A matrix
has to say what is known per release - and, far more importantly, what is *not*. Only BW 7.50 has
been verified here, and :class:`ReleaseSupport` says so on every other release rather than implying
coverage from silence. ``unverified`` is a first-class verdict, not an omission: a customer scoping
a proof of concept needs to know exactly which capabilities decide the answer on their release, and
that list is what this produces.

Everything here is derived from measurement or from a stated decision. Nothing is inferred from a
release number, because SAP's object model across 7.4 / 7.5 / BW/4HANA is exactly the thing this
server discovers at runtime rather than assuming.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .capability import ImplementationStatus, ValidationStatus

#: Whether this build could determine what a tool needs.
#:
#: ``not_measured`` is not the same as "needs nothing". A tool the offline suite never invokes at
#: the tool boundary has an unknown requirement set, and reporting that as an empty one would turn
#: a measurement gap into a false claim of universal compatibility - the most misleading answer the
#: matrix could give.
Measurement = Literal["measured", "not_measured"]

#: How firmly a tool is known to work on one release.
#:
#: The vocabulary deliberately has no ``supported``. Every value says where the claim comes from:
#:
#: * ``verified`` - every capability the tool needs was read through a feature against this release
#:   and the output inspected.
#: * ``expected`` - the tool is implemented and its capabilities are read, but not all of them were
#:   verified on this release. It should work; nobody has proven it here.
#: * ``unverified`` - nobody has run this tool against this release at all. Not a prediction.
#: * ``needs_connector`` - the BW half works, but the answer is completed by a system outside BW, so
#:   without that connector the tool returns a template naming what is missing.
#: * ``unknown`` - this build could not determine what the tool needs, so it cannot say.
ReleaseVerdict = Literal["verified", "expected", "unverified", "needs_connector", "unknown"]

#: How much is known about a release as a whole.
ReleaseEvidence = Literal["verified", "not_verified"]


class ReleaseSupport(BaseModel):
    """One BW release the matrix has an opinion about, and the basis for that opinion."""

    model_config = ConfigDict(extra="forbid")

    release: str = Field(min_length=1)
    evidence: ReleaseEvidence
    #: The exact system the verification was performed against, where there was one. A verification
    #: claim without this is not a claim.
    verified_on: str | None = None
    #: Capabilities whose presence this server resolves by *pattern* rather than by a known name.
    #: These are precisely the ones that differ across releases, so on an unverified release they
    #: are
    #: the list to check first - and they are a fact about the code, not a guess about the release.
    release_conditional: list[str] = Field(default_factory=list)
    note: str = ""


class ToolSupport(BaseModel):
    """One tool, what it needs, and how firmly it is known to work per release."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    #: Capabilities observed to be read by this tool. **A lower bound**: the measurement is bounded
    #: by what the offline suite exercises, so a tool may need more than this on a code path no test
    #: reaches. It is never an over-count, which is the direction that matters - a capability listed
    #: here really is read.
    requires: list[str] = Field(default_factory=list)
    measurement: Measurement = "not_measured"
    #: The weakest implementation status across everything the tool needs. A chain is only as built
    #: as its least-built link, so this is a minimum rather than a summary.
    implementation: ImplementationStatus | None = None
    #: The weakest validation status across everything the tool needs, for the same reason.
    validation: ValidationStatus = "not_validated"
    #: Verdict per release, keyed by ``ReleaseSupport.release``.
    releases: dict[str, ReleaseVerdict] = Field(default_factory=dict)
    #: Named when the answer is completed by a system outside BW.
    needs_connector: str | None = None
    note: str = ""

    @property
    def reads_nothing(self) -> bool:
        """True for a tool that answers from shipped data or configuration, not from BW.

        Distinct from ``not_measured``: this is a measured empty set, which is why the two are
        separate fields rather than one nullable list.
        """
        return self.measurement == "measured" and not self.requires


class SupportMatrix(BaseModel):
    """The whole matrix: releases, tools, and what the answer rests on."""

    model_config = ConfigDict(extra="forbid")

    #: Build that produced it, so two customers comparing answers can tell whether they match.
    server_version: str
    contract_revision: str | None = None
    releases: list[ReleaseSupport] = Field(default_factory=list)
    tools: list[ToolSupport] = Field(default_factory=list)
    #: Counts per release per verdict, so the headline is readable without walking 55 rows.
    totals: dict[str, dict[str, int]] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)

    def tool(self, name: str) -> ToolSupport | None:
        return next((entry for entry in self.tools if entry.tool == name), None)
