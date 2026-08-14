"""Tests for the bw:// resource surface and response shaping.

Two things are being verified, and the second matters more than it looks:

1. All seven URIs from mission Section 4 are registered and readable through a real MCP client.
2. Shaping never *loses* anything. A summarised reply must keep exact counts, say that it was
   summarised, and point at the resource holding the full record. A response that quietly drops
   fields is worse than a large one, because a model reading it cannot tell it is incomplete.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph, LineageNode
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.providers import Provider, ProviderField
from tests.test_server import FakeRuntime

_EXPECTED_URIS = {
    "bw://{system}/profile",
    "bw://{system}/catalog",
    "bw://{system}/chain/{chain_id}",
    "bw://{system}/provider/{name}",
    "bw://{system}/transformation/{tran_id}",
    "bw://{system}/query/{query_id}",
    "bw://{system}/calcview/{view_name}",
}


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema="TESTSCHEMA",
        discovered_at=datetime.now(UTC),
        tables={
            "transformation": TableStatus(
                logical_name="transformation",
                resolved_name="RSTRAN",
                present=True,
                schema_name="TESTSCHEMA",
                row_estimate=42,
            ),
            "adso_header": TableStatus(logical_name="adso_header", present=False),
        },
    )


class _Conn:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        return []


def _provenance() -> Provenance:
    return Provenance(source_table="RSDODSO", source_key={"ODSOBJECT": "WIDE_DSO"})


def _wide_provider(field_count: int) -> Provider:
    fields = [
        ProviderField(name=f"FIELD_{i:03d}", position=i, is_key=i < 2, provenance=_provenance())
        for i in range(field_count)
    ]
    return Provider(
        name="WIDE_DSO",
        object_type="dso",
        key_field_names=["FIELD_000", "FIELD_001"],
        fields=fields,
        provenance=_provenance(),
    )


def _graph(nodes: int, edges: int) -> LineageGraph:
    node_list = [
        LineageNode(id=f"N{i}", object_type="dso", name=f"N{i}", provenance=_provenance())
        for i in range(nodes)
    ]
    edge_list = [
        LineageEdge(
            src=f"N{i}",
            dst=f"N{(i + 1) % nodes}",
            kind="transformation",
            derivation="declared",
            confidence="exact",
            provenance=_provenance(),
        )
        for i in range(edges)
    ]
    return LineageGraph(
        root_id="N0",
        direction="both",
        depth=3,
        nodes=node_list,
        edges=edge_list,
        node_count=nodes,
        edge_count=edges,
    )


# --- registration --------------------------------------------------------------------------


def test_all_seven_mission_resources_are_registered() -> None:
    async def run() -> set[str]:
        async with Client(server.mcp) as client:
            templates = await client.list_resource_templates()
            return {str(t.uriTemplate) for t in templates}

    assert asyncio.run(run()) >= _EXPECTED_URIS


def test_resources_are_readable_and_return_json() -> None:
    server.set_runtime(FakeRuntime())  # reuses the fully faked runtime

    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.read_resource("bw://qa/profile")

    contents = asyncio.run(run())
    payload = json.loads(contents[0].text)
    assert payload["system"] == "qa"
    assert payload["bw_release"]


def test_catalog_resource_lists_only_present_tables() -> None:
    server.set_runtime(FakeRuntime())

    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.read_resource("bw://qa/catalog")

    payload = json.loads(asyncio.run(run())[0].text)
    assert "tables" in payload
    assert all(entry["resolved_name"] for entry in payload["tables"].values())


# --- provider shaping ----------------------------------------------------------------------


def test_narrow_provider_is_returned_whole() -> None:
    provider = _wide_provider(10)
    shaped = server._shape_provider(provider, system="qa", detail="auto")
    assert len(shaped.fields) == 10
    assert shaped.caveats == []


def test_wide_provider_is_summarised_to_the_semantic_key() -> None:
    provider = _wide_provider(200)
    shaped = server._shape_provider(provider, system="qa", detail="auto")
    assert len(shaped.fields) == 2
    assert [f.name for f in shaped.fields] == ["FIELD_000", "FIELD_001"]


def test_summary_says_so_and_points_at_the_resource() -> None:
    """A model must be able to tell the reply is partial, and how to get the rest."""
    shaped = server._shape_provider(_wide_provider(200), system="qa", detail="auto")
    caveat = " ".join(shaped.caveats)
    assert "2 of 200" in caveat
    assert "bw://qa/provider/WIDE_DSO" in caveat
    assert "detail='full'" in caveat


def test_full_detail_overrides_the_threshold() -> None:
    shaped = server._shape_provider(_wide_provider(200), system="qa", detail="full")
    assert len(shaped.fields) == 200
    assert shaped.caveats == []


def test_summary_detail_trims_even_a_narrow_provider() -> None:
    shaped = server._shape_provider(_wide_provider(5), system="qa", detail="summary")
    assert len(shaped.fields) == 2


def test_shaping_preserves_everything_except_the_field_list() -> None:
    provider = _wide_provider(200)
    shaped = server._shape_provider(provider, system="qa", detail="auto")
    assert shaped.name == provider.name
    assert shaped.object_type == provider.object_type
    assert shaped.key_field_names == provider.key_field_names
    assert shaped.provenance == provider.provenance


# --- lineage shaping -----------------------------------------------------------------------


def test_small_graph_is_returned_whole() -> None:
    graph = _graph(nodes=5, edges=5)
    shaped = server._shape_graph(graph, detail="auto")
    assert len(shaped.nodes) == 5
    assert shaped.caveats == []


def test_large_graph_keeps_exact_counts() -> None:
    """The caller must still learn the true size, or 'summarised' becomes 'wrong'."""
    graph = _graph(nodes=200, edges=200)
    shaped = server._shape_graph(graph, detail="auto")
    assert shaped.node_count == 200
    assert shaped.edge_count == 200
    assert len(shaped.nodes) < 200


def test_large_graph_stays_a_valid_subgraph() -> None:
    """Every retained edge must have both endpoints retained: no dangling references."""
    shaped = server._shape_graph(_graph(nodes=200, edges=200), detail="auto")
    ids = {node.id for node in shaped.nodes}
    assert all(edge.src in ids and edge.dst in ids for edge in shaped.edges)


def test_large_graph_says_it_was_summarised() -> None:
    shaped = server._shape_graph(_graph(nodes=200, edges=200), detail="auto")
    caveat = " ".join(shaped.caveats)
    assert "summarised" in caveat
    assert "of 200 nodes" in caveat
    assert "detail='full'" in caveat
    assert "N0" in caveat  # names the root the neighbourhood was kept around


def test_full_graph_detail_overrides_the_threshold() -> None:
    shaped = server._shape_graph(_graph(nodes=200, edges=200), detail="full")
    assert len(shaped.nodes) == 200
    assert shaped.caveats == []


@pytest.mark.parametrize("detail", ["auto", "summary", "full"])
def test_shaping_never_invents_nodes(detail: str) -> None:
    graph = _graph(nodes=100, edges=100)
    shaped = server._shape_graph(graph, detail=detail)  # type: ignore[arg-type]
    original = {node.id for node in graph.nodes}
    assert {node.id for node in shaped.nodes} <= original
