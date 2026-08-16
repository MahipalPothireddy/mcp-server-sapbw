"""Capability models: TableStatus and CapabilityRecord.

The capability record is the cached result of runtime discovery for one profile. It is the
mechanism that makes the server portable across BW 7.4 / 7.5 / BW/4HANA: every repository
consults it before building SQL so no tool ever queries a table that does not exist on the
connected release (mission Section 3, Rules 2 and 7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ValidationTier = Literal["existence", "discover"]
HanaRepoStyle = Literal["sys_repo", "hdi", "none"]

# How a presence probe ended. ``present: bool`` cannot express "I was not allowed to look", and a
# boolean forced that case to pick a side - it picked False, which asserts that a release lacks an
# object when the truth is that the connecting user lacks a grant. The two have opposite remedies
# (a different BW release vs. one GRANT SELECT), so they are kept apart here.
#
#   present      the object was observed in the catalog
#   absent       the catalog was read successfully and does not contain it
#   denied       the probe was refused - presence is unknown, and a grant would settle it
#   failed       the probe errored for some other reason - presence is unknown
#   not_probed   discovery did not ask about this object
ProbeOutcome = Literal["present", "absent", "denied", "failed", "not_probed"]

#: Outcomes that establish a fact either way. Anything else means "unknown", never "no".
DETERMINATE_OUTCOMES: frozenset[str] = frozenset({"present", "absent"})

# What the server does with a declared capability, independent of any connected system.
# Generated from measurement by scripts/capability_contract.py; see docs/capability-contract.md.
ContractState = Literal[
    "SUPPORTED",  # read by the server, covered by tests
    "PARTIAL",  # read, but the surface built on it is incomplete - the gap is named
    "DISCOVERY_ONLY",  # not read as a table; used to detect an object-model variant
    "PLANNED",  # declared ahead of implementation, with the intended feature named
    "NOT_SUPPORTED",  # validation plumbing only; no feature will read it
    "DEPRECATED",  # superseded, kept so an older release still resolves
]
IMPLEMENTED_STATES: frozenset[str] = frozenset({"SUPPORTED", "PARTIAL", "DISCOVERY_ONLY"})

# Does the code exist, and how completely. Derived from ContractState, which stays because it is a
# published schema; this is the axis to read when the question is "is it built".
ImplementationStatus = Literal[
    "implemented",
    "partial",
    "discovery_only",
    "planned",
    "unsupported",
    "deprecated",
]

# How far it has been proven, weakest first. A separate axis because one word was doing two jobs:
# `SUPPORTED` reads as "validated against supported BW versions" and only ever meant "a reader
# exists". A buying decision rests on this column, not the other one.
#
#   not_validated       no test has touched it; a reader may still exist
#   unit_tested         exercised by the offline suite against synthetic fixtures
#   integration_tested  read through a feature against a real BW system, output inspected
#   real_bw_validated   a recorded scenario ran against a real BW system and its answer was checked
#                       against human-verified ground truth, not merely observed to return
#   customer_validated  verified on a customer's own system, by that customer
#
# ``real_bw_validated`` has a different *source* from the others, which is why it sits between them
# rather than replacing ``integration_tested``. The first three are emitted by
# ``scripts/capability_contract.py`` from what the test suite measurably touched. This one comes
# by the validation matrix: a scenario with an expected answer, an actual answer, and a correctness
# verdict. "The call returned without error against a live system" is integration testing; "the
# answer was right" is this. The contract generator therefore never produces this value, and nothing
# claims it until a recorded scenario does.
ValidationStatus = Literal[
    "not_validated",
    "unit_tested",
    "integration_tested",
    "real_bw_validated",
    "customer_validated",
]

#: Validation ordering, so a caller can filter on "at least X" without enumerating. Only the
#: *ordering* is meaningful - callers compare ranks, they do not depend on the absolute numbers, so
#: inserting a rung is safe.
VALIDATION_RANK: dict[str, int] = {
    "not_validated": 0,
    "unit_tested": 1,
    "integration_tested": 2,
    "real_bw_validated": 3,
    "customer_validated": 4,
}

# The four ways a contract state and a system's table presence can combine. This is the
# distinction that matters to someone deciding whether to trust an answer: an absent table and an
# unimplemented reader both mean "no answer", but only one of them is fixable in this repo.
CapabilityVerdict = Literal[
    "usable",  # implemented here, present there
    "absent_on_system",  # implemented here, but this release/system does not have the object
    "not_implemented",  # present on the system, but no reader here yet
    "not_applicable",  # neither implemented nor present
]

# Logical object-model variant keys tracked in CapabilityRecord.object_models.
OBJECT_MODEL_KEYS = (
    "classic_dso",
    "adso",
    "composite_provider",
    "multiprovider",
    "open_ods_view",
)


class TableStatus(BaseModel):
    """Runtime-validation result for one logical metadata table.

    ``logical_name`` is the server's internal name (e.g. ``"adso_header"``); ``resolved_name``
    is the actual table found for this release (or ``None`` if absent). ``tier`` records whether
    the canonical name was known (``existence``) or had to be discovered by pattern (``discover``).
    """

    model_config = ConfigDict(extra="forbid")

    logical_name: str = Field(min_length=1)
    resolved_name: str | None = None
    tier: ValidationTier = "existence"
    present: bool = False
    #: How the probe ended. ``present`` stays a bool because it is a published field and every
    #: gating call site reads it, but it answers "was it observed", not "does it exist" - read
    #: ``probe`` to tell an observed absence from an unanswered question.
    probe: ProbeOutcome = "not_probed"
    # Owning schema (ABAP schema for RS* tables; SYS/_SYS_REPO/_SYS_BIC for HANA objects).
    # Named schema_name rather than "schema" to avoid shadowing pydantic's model API.
    schema_name: str | None = None
    row_estimate: int | None = None

    @property
    def determinate(self) -> bool:
        """True when the probe established presence or absence as a fact."""
        return self.probe in DETERMINATE_OUTCOMES

    @property
    def unreadable(self) -> bool:
        """True when presence is unknown because the probe could not be completed."""
        return self.probe in ("denied", "failed")


class CapabilityRecord(BaseModel):
    """Everything discovered about one profile at connect time.

    Cached with a TTL (default 24h). ``object_models`` maps each variant in
    :data:`OBJECT_MODEL_KEYS` to whether it is present and populated. ``tables`` is keyed by
    logical name.
    """

    model_config = ConfigDict(extra="forbid")

    system: str
    bw_release: str
    abap_schema: str
    object_models: dict[str, bool] = Field(default_factory=dict)
    #: Variants whose detection probe could not be completed, so their ``object_models`` entry is
    #: ``False`` for lack of evidence rather than because the system lacks them. Reported
    #: separately because "this system has no CompositeProviders" is a strong claim, and a denied
    #: catalog read is not grounds for making it.
    object_models_undetermined: list[str] = Field(default_factory=list)
    hana_repo_style: HanaRepoStyle = "none"
    # Runtime analysis window in days: the usable span of process-chain log, capped at 1 year
    # (owner decision); bounds runtime-statistics queries and reports the actual span when shorter.
    processlog_retention_days: int = 0
    tables: dict[str, TableStatus] = Field(default_factory=dict)
    discovered_at: datetime
    ttl_seconds: int = 86400

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the record is older than its TTL. Naive datetimes are treated as UTC."""
        reference = now or datetime.now(UTC)
        discovered = self.discovered_at
        if discovered.tzinfo is None:
            discovered = discovered.replace(tzinfo=UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        return (reference - discovered).total_seconds() > self.ttl_seconds

    def table(self, logical_name: str) -> TableStatus | None:
        """Return the :class:`TableStatus` for a logical name, or ``None`` if untracked."""
        return self.tables.get(logical_name)

    def is_available(self, logical_name: str) -> bool:
        """True when the logical table was found (present and resolved) on this release."""
        status = self.tables.get(logical_name)
        return status is not None and status.present and status.resolved_name is not None

    def has_object_model(self, key: str) -> bool:
        """True when the given object-model variant is present and populated."""
        return self.object_models.get(key, False)

    def object_model_determined(self, key: str) -> bool:
        """False when this variant's detection probe could not be completed."""
        return key not in self.object_models_undetermined

    def unreadable_tables(self) -> list[str]:
        """Logical names whose presence is unknown because the probe was denied or failed."""
        return sorted(name for name, status in self.tables.items() if status.unreadable)

    def denied_tables(self) -> list[str]:
        """Logical names the connected user was explicitly refused."""
        return sorted(name for name, status in self.tables.items() if status.probe == "denied")

    def is_unreadable(self, logical_name: str) -> bool:
        """True when this logical table's presence could not be established.

        The distinction :meth:`is_available` cannot make: it returns False both for an object this
        release does not have and for one this user may not read.
        """
        status = self.tables.get(logical_name)
        return status is not None and status.unreadable


class ContractEntry(BaseModel):
    """What the server does with one declared capability, on any system.

    Build-time fact, not a runtime one: generated by ``scripts/capability_contract.py`` from a
    static scan of the readers unioned with the logical names the SQL dialect was actually asked
    for during the test suite. ``reason`` is the useful field for anything not ``SUPPORTED`` - it
    names the gap or the decision rather than leaving the state unexplained.
    """

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)
    # Physical table, view, or discovery pattern this capability resolves to.
    object_name: str
    state: ContractState
    #: Whether the code exists. Derived from ``state`` when a shipped contract predates this field.
    implementation: ImplementationStatus | None = None
    #: How far it has been proven. Measured, never inferred upward - a capability with a reader that
    #: no test touched is ``not_validated``.
    validation: ValidationStatus = "not_validated"
    #: The BW release integration verification was performed against. A validation claim without a
    #: release is not a claim, because these tables differ across releases.
    validated_on: str | None = None
    reason: str = ""

    @property
    def implemented(self) -> bool:
        return self.state in IMPLEMENTED_STATES

    @property
    def validation_rank(self) -> int:
        return VALIDATION_RANK.get(self.validation, 0)


class CapabilitySupport(BaseModel):
    """One capability, joined: what the server does with it against what this system has."""

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)
    object_name: str
    state: ContractState
    implementation: ImplementationStatus | None = None
    validation: ValidationStatus = "not_validated"
    validated_on: str | None = None
    reason: str = ""
    # None when the capability is untracked by discovery (a declared name the resolver did not
    # probe on this release), which is reported rather than guessed at.
    present: bool | None = None
    resolved_name: str | None = None
    row_estimate: int | None = None
    verdict: CapabilityVerdict


class CapabilityReport(BaseModel):
    """Per-system support profile: the contract crossed with what discovery found.

    Answers "what can I actually ask this server about *my* system?" in one call.
    ``bw_system_profile`` reports presence; ``docs/capability-contract.md`` reports implementation;
    neither alone tells a customer whether a given question has an answer here.
    """

    model_config = ConfigDict(extra="forbid")

    system: str
    bw_release: str
    # Counts by verdict, so the headline is readable without walking the list.
    totals: dict[str, int] = Field(default_factory=dict)
    # Counts by contract state, matching docs/capability-contract.md for this build.
    by_state: dict[str, int] = Field(default_factory=dict)
    #: Counts by validation level, over the capabilities this system actually has. The number to
    #: read before trusting a result: "usable" says a reader exists and the object is present, not
    #: that anyone has proven the two work together on a release like yours.
    by_validation: dict[str, int] = Field(default_factory=dict)
    capabilities: list[CapabilitySupport] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
