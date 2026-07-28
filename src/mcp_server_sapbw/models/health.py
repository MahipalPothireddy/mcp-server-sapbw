"""Provider health: how much data a provider holds, and how current it is.

Two independent questions that together answer "can I trust what this object contains right now?":

* **Volume** — row counts and memory per generated table, split by role, from the HANA monitoring
  view. Distinguishes a provider that is genuinely empty from one that was never activated, and
  makes changelog bloat visible next to the active data it shadows.
* **Currency** — the request history BW records per provider: when it was last loaded, whether that
  load succeeded, how many records arrived, and in which update mode. This is the real answer to
  "is this data fresh?", as opposed to inferring it from a chain's schedule.

Volume figures come from live monitoring data, not metadata, so they are a point-in-time reading and
are cached only briefly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance

# Outcome of a load request (RSSTATMANPART.STATUS, domain RSSTATUS, decoded from the dictionary).
RequestStatus = Literal["success", "incomplete", "error", "unknown"]


class TableVolume(BaseModel):
    """Row count and memory for one generated table behind a provider."""

    model_config = ConfigDict(extra="forbid")

    table: str
    role: str  # active / inbound / changelog / fact_f / fact_e / master_attr
    record_count: int = 0
    memory_mb: float | None = None
    provenance: Provenance


class LoadRequest(BaseModel):
    """One load request against a provider (RSSTATMANPART)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: RequestStatus = "unknown"
    status_code: str | None = None  # raw icon code, so an undecoded value stays visible
    started_at: datetime | None = None
    ended_at: datetime | None = None
    records: int | None = None
    update_mode: str | None = None
    source: str | None = None  # DataSource / source provider that fed the request
    provenance: Provenance


class ProviderHealth(BaseModel):
    """Volume + currency for one provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    object_type: str | None = None
    # --- volume
    tables: list[TableVolume] = Field(default_factory=list)
    active_records: int = 0  # rows in the provider's own persisted data
    changelog_records: int = (
        0  # rows in the changelog, shown separately (bloat is invisible summed)
    )
    inbound_records: int = 0  # rows staged but not yet activated
    total_memory_mb: float | None = None
    tables_found: int = 0
    volume_resolved: bool = True  # False when the generated tables could not be located at all
    # True only when tables were *located* and hold no active data. Never set because the tables
    # could not be found - that is `volume_resolved=False`, a different and much weaker statement.
    unloaded: bool = False
    # --- currency
    last_request: LoadRequest | None = None
    last_successful_request: LoadRequest | None = None
    recent_requests: list[LoadRequest] = Field(default_factory=list)
    request_count: int = 0
    failed_request_count: int = 0
    data_age_days: int | None = None  # against the latest request date in the system
    caveats: list[str] = Field(default_factory=list)
