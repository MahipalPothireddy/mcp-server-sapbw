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
from mcp_server_sapbw.server import RefreshResult, SystemStatus

_TABLES = {"chain_attr": "RSPCCHAINATTR", "chain_text": "RSPCCHAINT", "log_chain": "RSPCLOGCHAIN"}


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
