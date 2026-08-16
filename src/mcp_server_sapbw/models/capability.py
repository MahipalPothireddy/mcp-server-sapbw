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
    # Owning schema (ABAP schema for RS* tables; SYS/_SYS_REPO/_SYS_BIC for HANA objects).
    # Named schema_name rather than "schema" to avoid shadowing pydantic's model API.
    schema_name: str | None = None
    row_estimate: int | None = None


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
    reason: str = ""

    @property
    def implemented(self) -> bool:
        return self.state in IMPLEMENTED_STATES


class CapabilitySupport(BaseModel):
    """One capability, joined: what the server does with it against what this system has."""

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)
    object_name: str
    state: ContractState
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
    capabilities: list[CapabilitySupport] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
