"""Tests for the external-system connector interface (B9)."""

from __future__ import annotations

from mcp_server_sapbw.connectors.base import (
    ConnectorKind,
    ConnectorRegistry,
    ConnectorStatus,
    NullConnector,
)
from mcp_server_sapbw.connectors.ecc import EccConnector
from mcp_server_sapbw.connectors.external_bi import BobjConnector, TableauConnector


class _ConfiguredTableau:
    kind: ConnectorKind = "tableau"

    def is_configured(self) -> bool:
        return True

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(kind="tableau", configured=True, detail="ok")


def test_null_connector_reports_absent() -> None:
    null = NullConnector("ecc")
    assert null.is_configured() is False
    assert null.status().configured is False


def test_empty_registry_yields_reasons_for_each_kind() -> None:
    registry = ConnectorRegistry()
    kinds: tuple[ConnectorKind, ...] = ("ecc", "tableau", "bobj")
    for kind in kinds:
        assert registry.is_configured(kind) is False
        reason = registry.unpopulated_reason(kind)
        assert reason is not None
        assert kind.upper() in reason


def test_registry_returns_configured_connector() -> None:
    registry = ConnectorRegistry([_ConfiguredTableau()])
    assert registry.is_configured("tableau") is True
    assert registry.unpopulated_reason("tableau") is None
    # An unregistered kind still reports its absence.
    assert registry.unpopulated_reason("ecc") is not None


def test_deferred_bi_connectors_are_not_configured() -> None:
    for connector in (TableauConnector(), BobjConnector()):
        assert connector.is_configured() is False
        assert "deferred" in connector.status().detail


def test_ecc_connector_without_a_profile_is_unconfigured_not_deferred() -> None:
    """ECC is implemented; with no profile it says how to configure it, not that it is deferred."""
    status = EccConnector().status()
    assert status.configured is False
    assert "deferred" not in status.detail
    assert "ecc_systems" in status.detail
