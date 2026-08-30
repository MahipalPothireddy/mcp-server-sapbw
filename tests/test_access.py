"""Deployment modes, the grant manifest, and telling a refused read from an absent object.

The bug these tests pin down: a refused catalog read used to be recorded as an observed absence,
so a missing ``GRANT SELECT`` surfaced as "this release has no Advanced DSOs" and tools went on to
report "not available on BW 7.50". A customer reading that plans a BW upgrade for a permissions
problem. Presence and permission are separate facts here, and these assert they stay separate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_server_sapbw.core.access import (
    GRANT_GROUPS,
    GRANT_PRINCIPAL,
    build_access_report,
    grant_statements,
    group_for_capability,
    mapped_capabilities,
)
from mcp_server_sapbw.core.capabilities import (
    ABAP_TABLES,
    DISCOVER_PATTERNS,
    HANA_VIEWS,
    CapabilityResolver,
    unsupported_result,
)
from mcp_server_sapbw.core.connection import QueryError, is_permission_denied
from mcp_server_sapbw.core.profiles import Profile
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.support import SupportMatrix, ToolSupport

Responder = Callable[[str, Sequence[Any] | None], list[tuple[Any, ...]]]

ALL_ABAP = set(ABAP_TABLES.values())
HANA_PRESENT = {"OBJECT_DEPENDENCIES", "VIEWS", "VIEW_COLUMNS", "COLUMNS", "M_CS_TABLES"}
DISCOVER_FOUND = {
    "RSOADSO%": ["RSOADSO", "RSOADSOT"],
    "RSOHCPR%": ["RSOHCPR", "RSOHCPRT"],
    "RSDDSTAT%": ["RSDDSTATHEADER"],
    "RSTRAN%": ["RSTRAN", "RSTRANT"],
    "ROOSOURCE": ["ROOSOURCE"],
    "ROOSFIELD": ["ROOSFIELD"],
    "RSEC%": ["RSECVAL", "RSECHIE"],
}


class Denied(Exception):
    """Stands in for hdbcli's insufficient-privilege error."""

    errorcode = 258


class Broken(Exception):
    """A failure that is not a permission problem."""


class DenyingConnection:
    """Answers discovery, except for the SQL fragments named in ``deny``/``break_on``."""

    def __init__(self, deny: set[str] | None = None, break_on: set[str] | None = None) -> None:
        self.deny = deny or set()
        self.break_on = break_on or set()

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        for marker in self.deny:
            if marker in sql:
                raise Denied("insufficient privilege: Not authorized")
        for marker in self.break_on:
            if marker in sql:
                raise Broken("column store error")
        if "SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'RSTRAN'" in sql:
            return [("SAPHANADB",)]
        if "CVERS" in sql:
            return [("SAP_BW", "750")]
        if "FROM SYS.VIEWS" in sql:
            return [(n,) for n in (parameters or []) if str(n) in HANA_PRESENT]
        if "_SYS_REPO" in sql and "ACTIVE_OBJECT" in sql:
            return [("ACTIVE_OBJECT",)]
        if "MIN(DATUM)" in sql:
            return [("20240101",)]
        if "DD02L" in sql and "LIKE" in sql:
            return [(n,) for n in DISCOVER_FOUND.get(str((parameters or [""])[0]), [])]
        if "DD02L" in sql and "TABNAME IN" in sql:
            return [(n,) for n in (parameters or []) if str(n) in ALL_ABAP]
        return []


def profile(**kwargs: Any) -> Profile:
    return Profile(
        name="qa",
        host="h.example.invalid",
        port=30015,
        user="ro",
        password=SecretStr("pw-secret"),
        abap_schema="auto",
        **kwargs,
    )


def resolve(deny: set[str] | None = None, break_on: set[str] | None = None) -> CapabilityRecord:
    return CapabilityResolver().resolve(profile(), DenyingConnection(deny, break_on))


# --- classifying the driver error ---------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "insufficient privilege: Not authorized",
        "SQL error: NOT AUTHORIZED",
        "no privilege on table",
        "authorization failed for user",
        "access denied",
    ],
)
def test_permission_markers_are_recognised(message: str) -> None:
    assert is_permission_denied(QueryError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "invalid table name: RSOADSO",  # 259 - absence, not permission
        "connection reset by peer",
        "column store error: search table error",
        "syntax error near SELECT",
    ],
)
def test_non_permission_failures_are_not_misread(message: str) -> None:
    """An unclassified failure must never be reported as a confirmed denial."""
    assert is_permission_denied(QueryError(message)) is False


def test_error_code_is_authoritative_over_wording() -> None:
    """A driver whose text this build has not seen still classifies from its numeric code."""
    assert is_permission_denied(QueryError("unfamiliar wording", errorcode=258)) is True


def test_absent_table_code_is_not_a_denial() -> None:
    assert is_permission_denied(QueryError("invalid table name", errorcode=259)) is False


def test_query_error_exposes_the_classification_for_the_error_model() -> None:
    """models/errors.py reads this attribute rather than importing the marker list."""
    assert QueryError("insufficient privilege").permission_denied is True
    assert QueryError("connection reset").permission_denied is False


# --- the resolver records a refusal as a refusal ------------------------------------------


def test_baseline_records_presence_as_observed() -> None:
    record = resolve()
    assert record.table("dso_header") is not None
    assert record.table("dso_header").probe == "present"  # type: ignore[union-attr]
    assert record.unreadable_tables() == []
    assert record.object_models_undetermined == []


def test_denied_dictionary_does_not_crash_and_does_not_claim_absence() -> None:
    """The whole reason discovery survives a refused DD02L: the report that explains it needs it.

    Before, this raised a raw driver exception out of ``resolve`` - taking away the one tool able
    to say which grant was missing.
    """
    record = resolve(deny={"DD02L"})
    status = record.table("dso_header")
    assert status is not None
    assert status.probe == "denied"
    assert status.present is False
    assert status.determinate is False
    assert record.is_unreadable("dso_header") is True
    assert len(record.denied_tables()) >= len(ABAP_TABLES)


def test_denied_hana_catalog_is_unknown_not_absent() -> None:
    record = resolve(deny={"FROM SYS.VIEWS"})
    for logical in HANA_VIEWS:
        status = record.table(logical)
        assert status is not None
        assert status.probe == "denied", logical
    assert record.is_available("object_dependencies") is False
    assert record.is_unreadable("object_dependencies") is True


def test_denied_discover_probe_no_longer_claims_the_release_lacks_adsos() -> None:
    """The measured wrong answer: adso/composite_provider read False for a missing grant."""
    record = resolve(deny={"LIKE ?"})
    assert record.has_object_model("adso") is False  # the published field is unchanged
    assert record.object_model_determined("adso") is False  # ...and now qualified
    assert set(record.object_models_undetermined) >= {"adso", "composite_provider"}
    for group in DISCOVER_PATTERNS:
        status = record.table(group)
        assert status is not None
        assert status.probe == "denied", group


def test_non_permission_probe_failure_is_failed_not_denied() -> None:
    """Two kinds of "could not look" - only one is fixed by a grant."""
    record = resolve(break_on={"LIKE ?"})
    status = record.table("adso")
    assert status is not None
    assert status.probe == "failed"
    assert status.unreadable is True
    assert record.denied_tables() == []
    assert "adso" in record.object_models_undetermined


def test_absent_is_still_absent_when_the_probe_succeeded() -> None:
    """The distinction must not swallow a genuine absence into "unknown"."""

    class NoAdso(DenyingConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "DD02L" in sql and "LIKE" in sql and str((parameters or [""])[0]) == "RSOADSO%":
                return []
            return super().execute_select(sql, parameters)

    record = CapabilityResolver().resolve(profile(), NoAdso())
    status = record.table("adso")
    assert status is not None
    assert status.probe == "absent"
    assert status.determinate is True
    assert record.object_model_determined("adso") is True
    assert record.has_object_model("adso") is False


# --- what a repository returns to the caller ----------------------------------------------


def test_unsupported_result_names_a_release_when_the_object_is_absent() -> None:
    record = resolve()
    record.tables["adso"] = TableStatus(logical_name="adso", present=False, probe="absent")
    result = unsupported_result(record, ["adso"])
    assert result.code == "unsupported_on_release"
    assert "BW 7.50" in result.detail


def test_unsupported_result_asks_for_a_grant_when_the_object_was_refused() -> None:
    """The customer-facing payoff: a non-retryable configuration failure, not a release verdict."""
    record = resolve(deny={"LIKE ?"})
    result = unsupported_result(record, ["adso"])
    assert result.code == "permission_denied"
    assert result.category == "configuration"
    assert result.retryable is False
    assert "not a statement that the release lacks it" in result.detail
    assert result.remedy is not None
    assert "bw_access_report" in result.remedy


# --- the grant manifest -------------------------------------------------------------------


def test_every_discovered_capability_belongs_to_exactly_one_group() -> None:
    """The drift guard: a table added to discovery cannot escape the manifest.

    Without this, a new capability becomes an ungranted dependency that no grant script mentions
    and no report explains.
    """
    discovered = set(ABAP_TABLES) | set(HANA_VIEWS) | set(DISCOVER_PATTERNS)
    unmapped = sorted(discovered - mapped_capabilities())
    assert unmapped == [], f"not in any grant group: {unmapped}"

    seen: dict[str, str] = {}
    for group in GRANT_GROUPS:
        for capability in group.capabilities:
            assert capability not in seen, (
                f"{capability} in both {seen.get(capability)}/{group.group}"
            )
            seen[capability] = group.group


def test_manifest_claims_nothing_that_discovery_does_not_track() -> None:
    discovered = set(ABAP_TABLES) | set(HANA_VIEWS) | set(DISCOVER_PATTERNS)
    assert mapped_capabilities() - discovered == set()


def test_every_group_states_what_is_lost_without_it() -> None:
    """A grant decision needs its cost stated up front, not discovered by a failing tool."""
    for group in GRANT_GROUPS:
        assert group.purpose.strip(), group.group
        assert group.without_it.strip(), group.group
        assert group.abap_objects or group.other_objects or group.system_privileges, group.group


def test_group_lookup_resolves_a_capability() -> None:
    found = group_for_capability("routine_source")
    assert found is not None
    assert found.group == "dataflow"
    assert group_for_capability("no_such_capability") is None


def test_grant_statements_are_runnable_and_carry_a_placeholder_principal() -> None:
    group = next(g for g in GRANT_GROUPS if g.group == "queries")
    statements = grant_statements(group, "SAPHANADB")
    assert 'GRANT SELECT ON "SAPHANADB"."RSZCOMPDIR" TO <BW_DISCOVERY_USER>;' in statements
    assert all(s.endswith(";") or s.startswith("--") for s in statements)
    assert all(GRANT_PRINCIPAL in s for s in statements)


def test_pattern_objects_are_not_emitted_as_a_literal_grant() -> None:
    """RSDDSTAT* varies by release, so a fixed grant statement would be a guess."""
    group = next(g for g in GRANT_GROUPS if g.group == "statistics")
    statements = grant_statements(group, "SAPHANADB")
    assert not any('"RSDDSTAT%"' in s and s.startswith("GRANT") for s in statements)
    assert any(s.startswith("--") for s in statements)


# --- the report ---------------------------------------------------------------------------


def matrix_with(tool: str, requires: list[str]) -> SupportMatrix:
    return SupportMatrix(
        server_version="test",
        tools=[ToolSupport(tool=tool, requires=requires, measurement="measured")],
    )


def test_full_read_reports_technical_read() -> None:
    report = build_access_report(resolve(), declared_mode="technical_read")
    assert report.observed_mode == "technical_read"
    assert report.mode_mismatch is False
    assert report.grants_required == []
    assert report.blocked_tools == []
    assert any("probed, not audited" in c for c in report.caveats)


def test_a_refusal_is_observed_as_least_privilege_whatever_the_profile_claims() -> None:
    report = build_access_report(resolve(deny={"LIKE ?"}), declared_mode="technical_read")
    assert report.observed_mode == "least_privilege"
    assert report.mode_mismatch is True
    assert report.grants_required


def test_declared_least_privilege_matching_evidence_is_not_a_mismatch() -> None:
    report = build_access_report(resolve(deny={"LIKE ?"}), declared_mode="least_privilege")
    assert report.mode_mismatch is False


def test_undeclared_mode_never_reports_a_mismatch() -> None:
    report = build_access_report(resolve(deny={"LIKE ?"}), declared_mode="unknown")
    assert report.mode_mismatch is False


def test_denied_group_carries_its_grants_and_names_the_cost() -> None:
    report = build_access_report(resolve(deny={"FROM SYS.VIEWS"}))
    hana = next(g for g in report.groups if g.group == "hana")
    assert hana.state == "denied"
    assert hana.denied_capabilities
    assert any("OBJECT_DEPENDENCIES" in s for s in hana.grant_statements)
    assert "calc view lineage" in hana.without_it


def test_blocked_tools_come_from_measured_attribution() -> None:
    report = build_access_report(
        resolve(deny={"FROM SYS.VIEWS"}),
        matrix=matrix_with("bw_get_hana_crossings", ["object_dependencies"]),
    )
    assert report.blocked_tools == ["bw_get_hana_crossings"]
    assert any("lower bound" in c for c in report.caveats)


def test_a_tool_needing_only_readable_objects_is_not_blocked() -> None:
    report = build_access_report(
        resolve(deny={"FROM SYS.VIEWS"}),
        matrix=matrix_with("bw_list_chains", ["chain_edges"]),
    )
    assert report.blocked_tools == []


def test_missing_matrix_is_reported_rather_than_read_as_nothing_blocked() -> None:
    report = build_access_report(resolve(deny={"FROM SYS.VIEWS"}), matrix=None)
    assert report.blocked_tools == []
    assert any("support matrix was unavailable" in c for c in report.caveats)


def test_required_group_breakage_is_called_out() -> None:
    report = build_access_report(resolve(deny={"DD02L"}))
    assert any("required group" in c for c in report.caveats)
    dictionary = next(g for g in report.groups if g.group == "dictionary")
    assert dictionary.required is True
    assert dictionary.state == "denied"


def test_undetermined_object_models_are_surfaced_on_the_report() -> None:
    report = build_access_report(resolve(deny={"LIKE ?"}))
    assert set(report.undetermined_object_models) >= {"adso", "composite_provider"}
    assert any("not because this system lacks them" in c for c in report.caveats)


def test_partial_group_is_distinguished_from_a_wholly_denied_one() -> None:
    record = resolve()
    record.tables["auth_values"] = TableStatus(
        logical_name="auth_values", present=False, probe="denied"
    )
    report = build_access_report(record)
    security = next(g for g in report.groups if g.group == "security")
    assert security.state == "partial"
    assert security.denied_capabilities == ["auth_values"]


def test_security_group_is_marked_optional_by_design() -> None:
    """Withholding row-level-security metadata is a supported posture, not a misconfiguration."""
    group = next(g for g in GRANT_GROUPS if g.group == "security")
    assert group.optional_by_design is True
    assert group.required is False


def test_report_totals_and_objects_are_schema_qualified() -> None:
    report = build_access_report(resolve())
    assert sum(report.totals.values()) == len(GRANT_GROUPS)
    providers = next(g for g in report.groups if g.group == "providers")
    assert '"SAPHANADB"."RSDODSO"' in providers.objects


def test_catalog_group_cannot_be_classified_from_probe_evidence() -> None:
    """Its objects are what the probes run *with*, so a failure there prevents discovery."""
    report = build_access_report(resolve())
    catalog = next(g for g in report.groups if g.group == "catalog")
    assert catalog.state == "undetermined"
    assert catalog.required is True


def test_read_only_assertion_is_reported() -> None:
    record = resolve()
    assert build_access_report(record, read_only_asserted=False).read_only_asserted is False
    assert build_access_report(record).read_only_asserted is True


def test_report_identifies_the_system_and_release() -> None:
    record = CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema="SAPHANADB",
        discovered_at=datetime.now(UTC),
    )
    report = build_access_report(record)
    assert report.system == "qa"
    assert report.bw_release == "BW 7.50"
