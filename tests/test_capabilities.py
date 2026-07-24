"""Tests for the capability resolver (B1), against simulated release fixtures.

A scripted fake connection returns canned rows keyed on stable SQL substrings, so the tests
exercise the resolver's *logic* (existence vs. discover, object-model detection, gating) rather
than the exact provisional SQL (validated live in B2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_server_sapbw.core.capabilities import (
    ABAP_TABLES,
    CapabilityError,
    CapabilityResolver,
    unsupported_result,
)
from mcp_server_sapbw.core.profiles import Profile

Responder = Callable[[str, Sequence[Any] | None], list[tuple[Any, ...]]]

ALL_ABAP = set(ABAP_TABLES.values())
HANA_PRESENT = {"OBJECT_DEPENDENCIES", "VIEWS", "VIEW_COLUMNS", "COLUMNS", "M_CS_TABLES"}


class ScriptedConnection:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.queries: list[tuple[str, Sequence[Any] | None]] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append((sql, parameters))
        return self._responder(sql, parameters)


def make_responder(
    *,
    schema: str = "SAPHANADB",
    release_rows: list[tuple[str, str]],
    present_abap: set[str],
    discover: dict[str, list[str]],
    present_hana: set[str] = HANA_PRESENT,
    repo: bool = True,
    min_datum: str | None = None,
) -> Responder:
    def respond(sql: str, params: Sequence[Any] | None) -> list[tuple[Any, ...]]:
        if "SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'RSTRAN'" in sql:
            return [(schema,)]
        if "CVERS" in sql:
            return list(release_rows)
        if "FROM SYS.VIEWS" in sql:
            names = list(params or [])
            return [(n,) for n in names if str(n) in present_hana]
        if "_SYS_REPO" in sql and "ACTIVE_OBJECT" in sql:
            return [("_SYS_REPO",)] if repo else []
        if "MIN(DATUM)" in sql:
            return [(min_datum,)] if min_datum is not None else [(None,)]
        if "DD02L" in sql and "LIKE" in sql:
            pattern = str((params or [""])[0])
            return [(name,) for name in discover.get(pattern, [])]
        if "DD02L" in sql and "TABNAME IN" in sql:
            names = list(params or [])
            return [(n,) for n in names if str(n) in present_abap]
        return []

    return respond


def auto_profile() -> Profile:
    return Profile(
        name="qa",
        host="h.example.invalid",
        port=30015,
        user="ro",
        password=SecretStr("pw-secret"),
        abap_schema="auto",
    )


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).strftime("%Y%m%d")


# --- BW 7.4: classic only, no ADSO / CP ---------------------------------------------------


def test_resolve_bw74_classic_only() -> None:
    responder = make_responder(
        release_rows=[("SAP_BW", "740")],
        present_abap=ALL_ABAP,
        discover={
            "RSOADSO%": [],
            "RSOHCPR%": [],
            "RSDDSTAT%": ["RSDDSTAT_OLAP", "RSDDSTATHEADER"],
            "RSTRAN%": ["RSTRAN", "RSTRANFIELD", "RSTRANRULE", "RSTRANT"],
            "ROOSOURCE": ["ROOSOURCE"],
            "ROOSFIELD": ["ROOSFIELD"],
        },
        min_datum=days_ago(90),
    )
    conn = ScriptedConnection(responder)
    record = CapabilityResolver().resolve(auto_profile(), conn)

    assert record.abap_schema == "SAPHANADB"
    assert record.bw_release == "BW 7.40"
    assert record.has_object_model("classic_dso") is True
    assert record.has_object_model("multiprovider") is True
    assert record.has_object_model("adso") is False
    assert record.has_object_model("composite_provider") is False
    assert record.is_available("transformation") is True
    assert record.is_available("adso") is False
    assert record.hana_repo_style == "sys_repo"
    assert record.processlog_retention_days == 90
    # discover-tier transformation text table picked by the 'T' heuristic
    text_table = record.table("transformation_text")
    assert text_table is not None
    assert text_table.resolved_name == "RSTRANT"


def test_unsupported_result_for_absent_adso() -> None:
    responder = make_responder(
        release_rows=[("SAP_BW", "740")],
        present_abap=ALL_ABAP,
        discover={"RSOADSO%": [], "RSOHCPR%": [], "RSDDSTAT%": [], "RSTRAN%": ["RSTRAN"]},
    )
    record = CapabilityResolver().resolve(auto_profile(), ScriptedConnection(responder))
    result = unsupported_result(record, ["RSOADSO"], alternative="RSDODSO")
    assert result.status == "unsupported_on_release"
    assert result.release == "BW 7.40"
    assert result.alternative == "RSDODSO"


# --- BW 7.5: ADSO + CP present ------------------------------------------------------------


def test_resolve_bw75_with_adso_and_cp() -> None:
    responder = make_responder(
        release_rows=[("SAP_BW", "750")],
        present_abap=ALL_ABAP,
        discover={
            "RSOADSO%": ["RSOADSO", "RSOADSOT", "RSOADSOIOBJ"],
            "RSOHCPR%": ["RSOHCPR", "RSOHCPRT", "RSOHCPRIOBJ"],
            "RSDDSTAT%": ["RSDDSTAT_OLAP"],
            "RSTRAN%": ["RSTRAN", "RSTRANT"],
        },
        min_datum=days_ago(30),
    )
    record = CapabilityResolver().resolve(auto_profile(), ScriptedConnection(responder))

    assert record.bw_release == "BW 7.50"
    assert record.has_object_model("adso") is True
    assert record.has_object_model("composite_provider") is True
    assert record.is_available("adso") is True
    adso = record.table("adso")
    assert adso is not None
    assert adso.resolved_name == "RSOADSO"  # shortest = header
    composite = record.table("composite_provider")
    assert composite is not None
    assert composite.resolved_name == "RSOHCPR"
    assert record.processlog_retention_days == 30


# --- BW/4HANA: ADSO + CP core; classic cube/DSO absent ------------------------------------


def test_resolve_bw4hana() -> None:
    classic_absent = {"RSDODSO", "RSDODSOT", "RSDODSOIOBJ", "RSDCUBE", "RSDCUBEMULTI"}
    responder = make_responder(
        release_rows=[("DW4CORE", "200")],
        present_abap=ALL_ABAP - classic_absent,
        discover={
            "RSOADSO%": ["RSOADSO", "RSOADSOT"],
            "RSOHCPR%": ["RSOHCPR", "RSOHCPRT"],
            "RSDDSTAT%": ["RSDDSTATWHM"],
            "RSTRAN%": ["RSTRAN", "RSTRANT"],
        },
        min_datum=days_ago(14),
    )
    record = CapabilityResolver().resolve(auto_profile(), ScriptedConnection(responder))

    assert record.bw_release == "BW/4HANA 200"
    assert record.has_object_model("classic_dso") is False
    assert record.has_object_model("multiprovider") is False
    assert record.has_object_model("adso") is True
    assert record.has_object_model("composite_provider") is True
    assert record.is_available("dso_header") is False


# --- schema resolution edge cases ---------------------------------------------------------


def test_explicit_schema_skips_lookup() -> None:
    profile = Profile(
        name="prd",
        host="h.example.invalid",
        port=30015,
        user="ro",
        password=SecretStr("pw"),
        abap_schema="SAPHANADB",
    )
    responder = make_responder(
        release_rows=[("SAP_BW", "750")],
        present_abap=ALL_ABAP,
        discover={"RSOADSO%": [], "RSOHCPR%": [], "RSDDSTAT%": [], "RSTRAN%": ["RSTRAN"]},
    )
    conn = ScriptedConnection(responder)
    record = CapabilityResolver().resolve(profile, conn)
    assert record.abap_schema == "SAPHANADB"
    # The SYS.TABLES schema-resolution query must NOT have been issued.
    assert not any("TABLE_NAME = 'RSTRAN'" in sql for sql, _ in conn.queries)


def test_schema_resolution_failure_raises() -> None:
    def respond(sql: str, params: Sequence[Any] | None) -> list[tuple[Any, ...]]:
        return []  # RSTRAN not found anywhere

    with pytest.raises(CapabilityError):
        CapabilityResolver().resolve(auto_profile(), ScriptedConnection(respond))
