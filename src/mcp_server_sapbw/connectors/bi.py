"""BI-platform connector: report schedules and dashboard sources from outside BW.

Scenarios 9.7 and 9.8 need two facts BW does not hold: *when each report is scheduled to run*, and
*which reports read a calc view directly instead of going through BW*. Both live in the BI platform.

The original design named Tableau and BOBJ specifically. That does not survive contact with other
organisations — Power BI, SAP Analytics Cloud, Looker and Qlik are all just as likely — and building
a client per vendor would mean shipping code that cannot be tested and credentials the server has no
business holding.

So the interface is vendor-neutral, and the reference implementation reads a **file** you export
from whatever platform you run: a small YAML or JSON inventory. That has three properties a
vendor-specific API client would not:

* it works for every BI platform today, including ones not thought of here;
* the export is auditable, and the customer decides what leaves their BI system;
* it needs no extra credentials in this server, which keeps the read-only, BW-only trust boundary
  intact.

A live API-backed connector for a specific platform can be added later by implementing
:class:`BiConnector` — nothing in the analyzers needs to change, which is the point of the
:class:`~.base.ConnectorRegistry`.

Inventory format (both keys optional; unknown keys are ignored so an export can carry extras)::

    platform: Tableau            # free text, reported back so findings name the source
    reports:
      - name: Daily Sales Extract
        provider: SALES_CP       # the BW provider (or dataset) it reads
        scheduled_start: "06:30" # local wall clock, 24h
        frequency: daily         # optional, free text
        owner: someone           # optional
    dashboards:
      - name: Margin Dashboard
        source_object: PKG.SALES/SALES_CV
        source_kind: calc_view   # calc_view | bw_provider
        connection: prod-hana    # optional
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .base import ConnectorStatus

BiSourceKind = Literal["calc_view", "bw_provider", "unknown"]


# These three models parse an *external* file, so they ignore unknown keys rather than rejecting
# them. Every other model in this server uses extra="forbid", which is right for internal contracts:
# a surprise field there is a bug. Here it is the opposite — a real export from Tableau, Power BI or
# SAC carries columns this server has no interest in, and refusing the whole inventory because of an
# 'exported_at' column would make the integration useless in practice.


class BiReportSchedule(BaseModel):
    """One report/extract and when it is scheduled to run."""

    model_config = ConfigDict(extra="ignore")

    name: str
    provider: str | None = None  # the BW provider or dataset the report reads
    scheduled_start: str | None = None  # "HH:MM", local wall clock
    frequency: str | None = None
    owner: str | None = None


class BiDashboardSource(BaseModel):
    """One dashboard and an object it reads directly."""

    model_config = ConfigDict(extra="ignore")

    name: str
    source_object: str
    source_kind: BiSourceKind = "unknown"
    connection: str | None = None


class BiInventory(BaseModel):
    """A platform's exported reporting inventory."""

    model_config = ConfigDict(extra="ignore")

    platform: str | None = None
    reports: list[BiReportSchedule] = Field(default_factory=list)
    dashboards: list[BiDashboardSource] = Field(default_factory=list)


@runtime_checkable
class BiConnector(Protocol):
    """What an analyzer needs from a BI platform, whatever the platform is."""

    @property
    def kind(self) -> Any: ...

    def is_configured(self) -> bool: ...

    def status(self) -> ConnectorStatus: ...

    def platform(self) -> str | None: ...

    def report_schedules(self) -> list[BiReportSchedule]: ...

    def dashboard_sources(self) -> list[BiDashboardSource]: ...


class FileBiConnector:
    """Reads a BI inventory exported to a local YAML or JSON file.

    Deliberately dumb: no network, no credentials, no vendor SDK. Parse failures make the connector
    report itself unconfigured with the reason, so a malformed export degrades into a documented gap
    instead of a half-populated analysis.
    """

    kind: Literal["bi"] = "bi"

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path else None
        self._inventory: BiInventory | None = None
        self._error: str | None = None
        if self._path is not None:
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.is_file():
            self._error = "the configured BI inventory file does not exist"
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            raw: Any
            if self._path.suffix.lower() in {".json"}:
                raw = json.loads(text)
            else:
                import yaml  # noqa: PLC0415 - only needed for the YAML form

                raw = yaml.safe_load(text)
            self._inventory = BiInventory.model_validate(raw or {})
        except Exception as exc:
            # The filename can be operator-sensitive; report the shape of the problem, not the path.
            self._error = f"the BI inventory file could not be parsed ({type(exc).__name__})"

    def is_configured(self) -> bool:
        return self._inventory is not None

    def status(self) -> ConnectorStatus:
        if self._inventory is not None:
            platform = self._inventory.platform or "unnamed platform"
            return ConnectorStatus(
                kind="bi",
                configured=True,
                detail=(
                    f"BI inventory loaded for {platform}: "
                    f"{len(self._inventory.reports)} report(s), "
                    f"{len(self._inventory.dashboards)} dashboard source(s)"
                ),
            )
        detail = self._error or "no BI inventory configured"
        return ConnectorStatus(kind="bi", configured=False, detail=detail)

    def platform(self) -> str | None:
        return self._inventory.platform if self._inventory else None

    def report_schedules(self) -> list[BiReportSchedule]:
        return list(self._inventory.reports) if self._inventory else []

    def dashboard_sources(self) -> list[BiDashboardSource]:
        return list(self._inventory.dashboards) if self._inventory else []
