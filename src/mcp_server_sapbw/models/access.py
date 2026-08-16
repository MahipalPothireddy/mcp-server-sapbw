"""Access models: deployment mode, grant groups, and what a missing grant costs.

**The question this answers.** A customer's Basis team is asked to provision a user for this
server and reasonably wants to know exactly which SELECT privileges that means, and what the
server stops being able to answer for each one withheld. Until this existed the answer was "read
access to BW metadata", which is not a grant script, and the consequence of withholding any part
of it was discoverable only by running a tool and reading a failure.

**Why a mode rather than a single privilege list.** Two provisioning postures are legitimate and
they trade off differently. A full technical read is one grant and answers everything; a
least-privilege allow-list is a longer conversation with a security team and answers less. Naming
both, and stating what each forfeits, is more useful than publishing one list and calling the other
unsupported.

**Declared, then observed.** ``declared_mode`` is what the profile says was provisioned;
``observed_mode`` is what the connected user's probe results actually show. They can disagree - a
profile claiming a full technical read whose dictionary probe was refused is a provisioning fault,
and it is reported rather than absorbed. This mirrors the ``environment`` decision: a label a human
sets is never inferred from evidence, and evidence is never overwritten by a label.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How the connecting user was provisioned.
#:
#:   technical_read   SELECT across the ABAP schema and the SYS catalog. Everything implemented
#:                    here can answer. This is the posture the shipped verification was run under.
#:   least_privilege  SELECT on an explicit allow-list. Some questions have no answer by design;
#:                    which ones is stated per grant group rather than discovered by failure.
#:   unknown          not declared on the profile, and not yet established from probe evidence.
AccessMode = Literal["technical_read", "least_privilege", "unknown"]

#: Whether a grant group was readable on this connection.
#:
#:   granted       every capability in the group was probed successfully
#:   denied        at least one capability was explicitly refused - a GRANT would settle it
#:   partial       some readable, some refused
#:   absent        readable, but this release does not carry the objects (not a grant problem)
#:   undetermined  the group was not probed, or probes failed for a non-permission reason
GrantState = Literal["granted", "denied", "partial", "absent", "undetermined"]


class GrantGroupStatus(BaseModel):
    """One functional bundle of metadata objects, and whether this user can read it."""

    model_config = ConfigDict(extra="forbid")

    group: str = Field(min_length=1)
    title: str
    #: True when the server cannot function at all without this group.
    required: bool = False
    state: GrantState
    #: What having this group lets the server answer.
    purpose: str
    #: What is forfeited when it is withheld. Populated for every group, not only denied ones, so
    #: the cost of a least-privilege decision is readable before the decision is made.
    without_it: str
    #: Physical objects to grant, fully qualified where the schema is fixed. The ABAP schema is
    #: substituted from the connected system when the report is built for a live profile.
    objects: list[str] = Field(default_factory=list)
    #: Logical capability names in this group that were explicitly refused.
    denied_capabilities: list[str] = Field(default_factory=list)
    #: Logical names whose presence could not be established for a non-permission reason.
    undetermined_capabilities: list[str] = Field(default_factory=list)
    #: Ready-to-run grant statements for whatever is missing. Empty when nothing is needed.
    grant_statements: list[str] = Field(default_factory=list)


class AccessReport(BaseModel):
    """Which deployment mode is in force, and what it can and cannot answer.

    ``blocked_tools`` is derived from the support matrix's measured per-tool capability
    attribution, not from a hand-kept list, so it cannot drift away from what the tools actually
    read. It inherits that measurement's caveat: attribution is a lower bound, so a tool absent
    from this list is not proven unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    system: str
    bw_release: str
    #: What the profile says was provisioned. Never inferred.
    declared_mode: AccessMode = "unknown"
    #: What the probe evidence shows. Never overwritten by the declaration.
    observed_mode: AccessMode = "unknown"
    #: True when declaration and evidence disagree - a provisioning fault worth naming.
    mode_mismatch: bool = False
    #: Whether the connect-time read-only grant assertion was requested for this profile.
    read_only_asserted: bool = True
    groups: list[GrantGroupStatus] = Field(default_factory=list)
    #: Counts by grant state, so the headline is readable without walking the list.
    totals: dict[str, int] = Field(default_factory=dict)
    #: Every grant statement needed to reach a full technical read, de-duplicated and ordered.
    grants_required: list[str] = Field(default_factory=list)
    #: Tools that read at least one refused capability, from measured attribution.
    blocked_tools: list[str] = Field(default_factory=list)
    #: Object-model variants reported ``False`` for lack of evidence rather than for lack of the
    #: object. Called out because "this system has no CompositeProviders" is a strong claim.
    undetermined_object_models: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
