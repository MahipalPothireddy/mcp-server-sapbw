"""Contract tests for the MCP surface (B3), offline via an in-memory FastMCP client.

Drives the real registered tools against a fixture-backed fake runtime — proving the
core -> repository -> tool -> MCP-client path end to end without a live system.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.search import SearchRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.server import RefreshResult, SystemStatus

_TABLES = {
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "log_chain": "RSPCLOGCHAIN",
    "dso_header": "RSDODSO",
    "dso_field": "RSDODSOIOBJ",
    "dso_text": "RSDODSOT",
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "transformation_text": "RSTRANT",
}


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema="TESTSCHEMA",
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical, resolved_name=physical, present=True, schema_name="TESTSCHEMA"
            )
            for logical, physical in _TABLES.items()
        },
    )


class _Conn:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "RSPCCHAINATTR" in sql:
            return [("DAILY_LOAD", "FINANCE", "ACT")]
        if "RSPCCHAINT" in sql:
            return [("DAILY_LOAD", "Daily finance load")]
        if "RSPCLOGCHAIN" in sql:  # run summary (GROUP BY): 30 runs across 30 days -> daily
            return [("DAILY_LOAD", 30, "20260601", "20260630")]
        if "RSDODSOIOBJ" in sql:  # DSO fields
            return [("DOC", 1, "X"), ("AMOUNT", 2, "")]
        if "RSDODSOT" in sql:
            if "UPPER(TXTLG)" in sql:  # search-by-description query
                return []
            return [("E", "Sales orders", "Daily sales order line items")]  # describe text
        if "RSDODSO" in sql:
            if "ODSOTYPE" in sql:  # describe header
                return [("", "SALES", "DEVUSER", "SD")]
            return [("SALES_DSO",)]  # search-by-name query (ODSOBJECT)
        if "RSTRANSTEPROUT" in sql:  # no field routines in this fixture
            return []
        if "RSTRANFIELD" in sql:
            return [(1, "1", "TARGETF"), (1, "0", "SOURCEF")]
        if "RSTRANRULE" in sql:
            return [(1, "DIRECT")]
        if "RSTRANT" in sql:
            return [("E", "Load one", "Load one target set")]
        if "RSAABAP" in sql:
            return [("METHOD start.",), ("  SELECT * FROM mara INTO TABLE lt.",), ("ENDMETHOD.",)]
        if "RSTRAN" in sql:
            if "OBJSTAT" in sql:  # get_transformation header (12 cols)
                return [("ACT", "RSDS", "", "DS_A", "ADSO", "", "ADSO_T", "CODE1", "", "", "", "")]
            return [("TR1", "RSDS", "DS_A", "ADSO", "ADSO_T", "CODE1", "", "")]  # list (8 cols)
        return []


class FakeRuntime:
    def __init__(self) -> None:
        self._cap = _capability()

    def list_systems(self) -> list[SystemStatus]:
        return [SystemStatus(name="qa", status="discovered", release="7.50", read_only_user=True)]

    def capability(self, system: str) -> CapabilityRecord:
        return self._cap

    def refresh_capabilities(self, system: str) -> CapabilityRecord:
        return self._cap

    def refresh_cache(self, system: str, scope: str) -> RefreshResult:
        return RefreshResult(system=system, scope=scope, removed=0)

    def chains(self, system: str) -> ChainsRepository:
        return ChainsRepository(_Conn(), self._cap)

    def providers(self, system: str) -> ProvidersRepository:
        return ProvidersRepository(_Conn(), self._cap)

    def search(self, system: str) -> SearchRepository:
        return SearchRepository(_Conn(), self._cap)

    def transformations(self, system: str) -> TransformationsRepository:
        return TransformationsRepository(_Conn(), self._cap)


async def _call(tool: str, args: dict[str, Any]) -> Any:
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, args)


def test_list_systems_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_list_systems", {}))
    data = result.structured_content
    # structured output for a list is wrapped under "result"
    systems = data["result"] if isinstance(data, dict) and "result" in data else data
    assert systems[0]["name"] == "qa"


def test_list_chains_via_client_has_provenance_and_total() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_list_chains", {"system": "qa"}))
    payload = result.structured_content
    # union return types are wrapped under "result"
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["total_count"] == 1
    chain = body["items"][0]
    assert chain["chain_id"] == "DAILY_LOAD"
    assert chain["frequency"] == "daily"
    assert chain["provenance"]  # provenance present
    # no secrets could appear (fake has none), but assert host/password strings are absent
    text = str(payload)
    assert "password" not in text.lower()


def test_describe_object_via_client_has_description_and_provenance() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_describe_object", {"system": "qa", "name": "SALES_DSO"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["object_type"] == "dso"
    assert [f["name"] for f in body["fields"]] == ["DOC", "AMOUNT"]
    assert body["key_field_names"] == ["DOC"]
    assert body["description"]["origin"] == "stored"
    assert body["provenance"]  # present


def test_search_objects_via_client_returns_typed_hits() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_search_objects", {"system": "qa", "pattern": "SALES", "object_types": ["dso"]})
    )
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["total_count"] == 1
    hit = body["items"][0]
    assert hit["name"] == "SALES_DSO"
    assert hit["object_type"] == "dso"
    assert hit["matched_on"] == "name"
    assert hit["provenance"]


def test_get_transformation_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_get_transformation", {"system": "qa", "transformation_id": "TR1"})
    )
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["source"]["kind"] == "datasource"
    assert body["target"]["kind"] == "adso"
    mapping = body["field_mappings"][0]
    assert mapping["rule_type"] == "direct"
    assert mapping["target_fields"] == ["TARGETF"]
    assert body["has_start_routine"] is True


def test_analyze_routine_via_client_is_lower_bound() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_analyze_routine", {"system": "qa", "transformation_id": "TR1"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    analyses = body["analyses"]
    assert analyses
    assert all(a["completeness"] == "lower_bound" for a in analyses)
    assert analyses[0]["provenance"]["source_table"] == "RSAABAP"


def test_registered_tool_names_are_valid() -> None:
    # Every registered tool name must satisfy the MCP naming constraint.
    names = asyncio.run(_list_tool_names())
    assert "bw_list_chains" in names
    for name in names:
        assert server._TOOL_NAME_RE.match(name) and len(name) <= server._MAX_TOOL_NAME


async def _list_tool_names() -> list[str]:
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        return [t.name for t in tools]
