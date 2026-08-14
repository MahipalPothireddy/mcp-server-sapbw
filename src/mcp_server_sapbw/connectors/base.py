"""External-system connector interface (B9, task 22).

The BW-on-HANA connection never reaches ECC, Tableau, or BOBJ — those are distinct systems with
distinct credentials and, for ECC, a different database engine (SQL Server, not HANA). Scenarios
9.6 (ECC extractor enhancements), 9.7 (report schedules), and 9.8 (dashboards on calc views) depend
on metadata that only those systems hold.

This module defines the pluggable interface and a :class:`NullConnector` that reports its own
absence. Analyzers ask the :class:`ConnectorRegistry` for a connector kind; when none is configured
the registry yields the reason string, and the analyzer emits its finding with an
``unpopulated_reason`` rather than inventing data (mission Rules 2/3, Known Limitation 2). Live ECC
and Tableau/BOBJ implementations are deferred (tasks 37/38); their class shapes live in ``ecc.py``
and ``external_bi.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

# ``bi`` is the vendor-neutral kind: report schedules and dashboard sources look the same whether
# they come from Tableau, Power BI, SAP Analytics Cloud, Looker or Qlik, so the analyzers ask for a
# capability rather than a product. ``tableau`` and ``bobj`` are retained so an existing
# configuration and the older reason strings keep working.
ConnectorKind = Literal["ecc", "bi", "tableau", "bobj"]

# What each connector would unlock, named in the "not configured" reason so a gap is actionable.
CONNECTOR_PURPOSE: dict[ConnectorKind, str] = {
    "ecc": (
        "ECC extractor-enhancement source (CMOD/BAdI ABAP, ROOSOURCE/ROOSFIELD) for scenario 9.6 "
        "enhancement logic"
    ),
    "bi": (
        "report/extract schedules and dashboard sources from your BI platform (any of Tableau, "
        "Power BI, SAP Analytics Cloud, Looker, Qlik, BOBJ) for scenarios 9.7 and 9.8"
    ),
    "tableau": (
        "Tableau report/extract schedules and calc-view dashboard usage for scenarios 9.7 and 9.8"
    ),
    "bobj": "BusinessObjects report schedules for scenario 9.7",
}

#: Kinds that satisfy the BI capability, most general first. An analyzer asks for the capability and
#: takes whichever is configured, so a Tableau-specific setup keeps working unchanged.
BI_KINDS: tuple[ConnectorKind, ...] = ("bi", "tableau", "bobj")


class ConnectorStatus(BaseModel):
    """Whether a given external connector is configured, and a human-readable detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ConnectorKind
    configured: bool
    detail: str


@runtime_checkable
class ExternalConnector(Protocol):
    """Structural interface every external connector satisfies.

    ``kind`` is declared read-only so an implementation may narrow it to its own literal (a
    connector's kind is fixed at class level); a mutable attribute would force every implementation
    to widen it back to :data:`ConnectorKind`.
    """

    @property
    def kind(self) -> ConnectorKind: ...

    def is_configured(self) -> bool: ...

    def status(self) -> ConnectorStatus: ...


class NullConnector:
    """Stands in for an absent connector: never fabricates data, only reports its absence."""

    def __init__(self, kind: ConnectorKind) -> None:
        self.kind = kind

    def is_configured(self) -> bool:
        return False

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            kind=self.kind,
            configured=False,
            detail=f"{self.kind.upper()} connector not configured",
        )


def unpopulated_reason(kind: ConnectorKind) -> str:
    """The reason string an analyzer attaches when a required connector is absent."""
    return f"requires a {kind.upper()} connector (not configured): {CONNECTOR_PURPOSE[kind]}"


class ConnectorRegistry:
    """Holds configured external connectors; yields a :class:`NullConnector` for any absent kind.

    The registry is empty by default (BW-only build). Live connectors are registered here once
    implemented, without any change to the analyzers that consume them.
    """

    def __init__(self, connectors: Iterable[ExternalConnector] | None = None) -> None:
        self._by_kind: dict[ConnectorKind, ExternalConnector] = {}
        for connector in connectors or ():
            self._by_kind[connector.kind] = connector

    def get(self, kind: ConnectorKind) -> ExternalConnector:
        existing = self._by_kind.get(kind)
        if existing is not None and existing.is_configured():
            return existing
        return NullConnector(kind)

    def is_configured(self, kind: ConnectorKind) -> bool:
        return self.get(kind).is_configured()

    def unpopulated_reason(self, kind: ConnectorKind) -> str | None:
        """``None`` when the connector is configured, else the reason it cannot be populated."""
        return None if self.is_configured(kind) else unpopulated_reason(kind)

    # --- capability lookup (rather than product lookup) ---------------------------------

    def bi(self) -> ExternalConnector | None:
        """Whichever configured connector can answer BI questions, or ``None``.

        Analyzers ask for the *capability* so that an organisation on any BI platform is served by
        the same code path, and an existing ``tableau``/``bobj`` configuration keeps working.
        """
        for kind in BI_KINDS:
            candidate = self._by_kind.get(kind)
            if candidate is not None and candidate.is_configured():
                return candidate
        return None

    def bi_unpopulated_reason(self) -> str | None:
        """``None`` when some BI connector is configured, else why 9.7/9.8 cannot be populated."""
        return None if self.bi() is not None else unpopulated_reason("bi")
