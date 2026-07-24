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
