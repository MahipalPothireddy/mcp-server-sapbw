"""Tests for the external-system connector interface (B9)."""

from __future__ import annotations

from pathlib import Path

from mcp_server_sapbw.connectors.base import (
    ConnectorKind,
    ConnectorRegistry,
    ConnectorStatus,
    NullConnector,
)
from mcp_server_sapbw.connectors.ecc import EccConnector
from mcp_server_sapbw.connectors.external_bi import BobjConnector, TableauConnector
from mcp_server_sapbw.core.capabilities import CapabilityResolver
from mcp_server_sapbw.core.connection import ReadOnlyConnectionPool
from mcp_server_sapbw.core.profiles import ProfileManager
from mcp_server_sapbw.models.ecc import ConnectorUnavailable
from mcp_server_sapbw.server import ServerRuntime
from mcp_server_sapbw.services.exit_analysis import ExitAnalysisService


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


def test_external_bi_connectors_without_a_profile_are_unconfigured_not_deferred() -> None:
    """Both are implemented now; with no profile they say how to configure, not that they are stubs.

    This test previously asserted the opposite - that the detail contained "deferred" - which was
    correct while ``external_bi.py`` was a 51-line placeholder. It is kept rather than deleted, and
    inverted, because the assertion it now makes is the one that matters: an unconfigured connector
    has to tell an operator what to add. Behaviour tests for the live readers live in
    ``tests/test_external_bi.py``.
    """
    for connector, key in ((TableauConnector(), "tableau"), (BobjConnector(), "bobj")):
        assert connector.is_configured() is False
        detail = connector.status().detail
        assert "deferred" not in detail
        assert "bi_platforms" in detail
        assert f"kind: {key}" in detail


def test_ecc_connector_without_a_profile_is_unconfigured_not_deferred() -> None:
    """ECC is implemented; with no profile it says how to configure it, not that it is deferred."""
    status = EccConnector().status()
    assert status.configured is False
    assert "deferred" not in status.detail
    assert "ecc_systems" in status.detail


# --- Source-system resolution through the runtime (D66) ---------------------------------------
#
# ``ServerRuntime._ecc_connector`` honours ``serves``, and ``_registry`` passes the BW system into
# it -- but ``exit_analysis`` did not, so the tool built on it declined on any landscape with more
# than one source system configured and told the caller the mapping was unrecorded. That claim
# stopped being true when ``serves`` was added. Neither resolution path had a test, which is the
# reason the two drifted apart unnoticed; both are pinned here.
#
# No BW connection is opened: ``exit_analysis`` reads only the profile manager, so these run
# offline like everything else in the suite.

_ECC_RESOLUTION_ENV = {"BWPW": "bwsecret", "APW": "asecret", "BPW": "bsecret"}


def _resolution_profiles(tmp_path: Path) -> Path:
    """Two source systems, one declaring that it feeds BW ``prd``. Passwords are ``${VAR}`` refs."""
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        "systems:\n"
        "  prd:\n"
        "    host: bwh\n"
        "    port: 30015\n"
        "    user: bwu\n"
        "    password: ${BWPW}\n"
        "ecc_systems:\n"
        "  ecc_feeder:\n"
        "    host: ah\n"
        "    port: 8443\n"
        "    client: '300'\n"
        "    user: au\n"
        "    password: ${APW}\n"
        "    serves: [prd]\n"
        "  ecc_sandbox:\n"
        "    host: bh\n"
        "    port: 8001\n"
        "    client: '300'\n"
        "    user: bu\n"
        "    password: ${BPW}\n",
        encoding="utf-8",
    )
    return profiles


def _resolution_runtime(tmp_path: Path) -> ServerRuntime:
    manager = ProfileManager(_resolution_profiles(tmp_path), env=_ECC_RESOLUTION_ENV)
    return ServerRuntime(
        manager, ReadOnlyConnectionPool(), CapabilityResolver(), cache_dir=tmp_path / "cache"
    )


def test_exit_analysis_resolves_the_source_system_that_serves_the_bw_system(
    tmp_path: Path,
) -> None:
    """The D66 fix: naming the BW system is enough, because the profile declares what it feeds."""
    service = _resolution_runtime(tmp_path).exit_analysis(None, bw_system="prd")
    assert isinstance(service, ExitAnalysisService)


def test_exit_analysis_still_honours_an_explicitly_named_source_system(tmp_path: Path) -> None:
    """``ecc_system`` stays the most explicit route and must win over any inference."""
    service = _resolution_runtime(tmp_path).exit_analysis("ecc_sandbox", bw_system="prd")
    assert isinstance(service, ExitAnalysisService)


def test_exit_analysis_declines_when_neither_a_name_nor_a_bw_system_is_given(
    tmp_path: Path,
) -> None:
    """Two candidates and nothing to choose between them: decline rather than read the wrong one."""
    result = _resolution_runtime(tmp_path).exit_analysis(None)
    assert isinstance(result, ConnectorUnavailable)
    assert result.configured_profiles == ["ecc_feeder", "ecc_sandbox"]
    # The old text asserted the mapping was unrecorded. It is recorded, so the reason now points at
    # the parameter that would use it.
    assert "not recorded in the profiles file" not in result.detail
    assert "system" in result.detail


def test_exit_analysis_says_which_bw_system_nothing_claims_to_serve(tmp_path: Path) -> None:
    """A BW system no profile serves is a different failure from having been given no system."""
    result = _resolution_runtime(tmp_path).exit_analysis(None, bw_system="qa")
    assert isinstance(result, ConnectorUnavailable)
    assert "serves: [qa]" in result.detail


def test_a_sandbox_declaring_nothing_is_never_resolved_as_a_source(tmp_path: Path) -> None:
    """``serves`` defaults to empty so an unrelated profile cannot be picked up by accident."""
    manager = ProfileManager(_resolution_profiles(tmp_path), env=_ECC_RESOLUTION_ENV)
    assert manager.get_ecc("ecc_sandbox").serves == []
    assert manager.get_ecc("ecc_feeder").serves == ["prd"]
