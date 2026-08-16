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
from mcp_server_sapbw.models.queries import Query, QueryElement
from tests.test_server import _CHAIN_TABLES, FakeRuntime

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


# --- every template is actually readable, not just registered ------------------------------
#
# Registration and readability are different facts. Before these, five of the seven templates were
# only ever asserted to exist - so a template could name a parameter the function does not take, or
# a reader that raises, and nothing would have noticed.

_READS = {
    "bw://qa/profile": "system",
    "bw://qa/catalog": "system",
    "bw://qa/chain/DAILY_LOAD": "chain_id",
    "bw://qa/provider/SALES_DSO": "name",
    "bw://qa/transformation/TR1": "tran_id",
    "bw://qa/query/QRY1": "compuid",
    "bw://qa/calcview/CV1": "view_name",
}


def _read(uri: str) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.read_resource(uri)

    return json.loads(asyncio.run(run())[0].text)


@pytest.mark.parametrize(("uri", "expected_key"), sorted(_READS.items()))
def test_every_resource_template_is_readable(uri: str, expected_key: str) -> None:
    server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
    payload = _read(uri)
    assert isinstance(payload, dict)
    assert expected_key in payload, f"{uri} returned {sorted(payload)}"


# --- BW names contain slashes, and a URI template expands one path segment ------------------


def test_a_namespaced_name_needs_encoding_and_the_helper_provides_it() -> None:
    """The defect this guards: `bw://qa/provider//IRM/IP_O02` resolves to nothing.

    Measured on the reference system: 484 objects carry a slash, including 29 of 143 cube-table
    objects and 57 of 280 active chains. An unencoded citation is unreadable for a fifth of them.
    """
    uri = server.resource_uri("qa", "provider", "/IRM/IP_O02")
    assert uri == "bw://qa/provider/%2FIRM%2FIP_O02"
    assert "//" not in uri.removeprefix("bw://")


@pytest.mark.parametrize(
    "name",
    [
        "/IRM/IP_O02",  # a namespaced provider
        "0BW:BIA:SALES",  # a generated calc view
        "!!ADHOC_QUERY",  # an ad-hoc BEx query
        "NAME WITH SPACE",
        "A%B",
    ],
)
def test_an_encoded_identifier_round_trips_to_the_reader(name: str) -> None:
    """What the reader receives must be the original name, not the encoded form.

    Encoding is only useful if it is undone before the lookup: a repository asked for
    ``%2FIRM%2FIP_O02`` would correctly report that no such provider exists.
    """
    asked: list[str] = []

    class _Spy:
        def describe(self, requested: str, object_type: Any = None) -> Any:
            asked.append(requested)
            return {"name": requested}

    class _SpyRuntime(FakeRuntime):
        def providers(self, system: str) -> Any:
            return _Spy()

    server.set_runtime(_SpyRuntime())
    _read(server.resource_uri("qa", "provider", name))
    assert asked == [name]


def test_a_summarised_provider_cites_a_uri_that_actually_resolves() -> None:
    """The end-to-end property: follow the citation and get the full record.

    The caveat is the only place a caller is told where the omitted fields went, so a citation that
    does not resolve is worse than no citation - it looks like an answer.
    """
    provider = _wide_provider(200).model_copy(update={"name": "/IRM/IP_O02"})
    shaped = server._shape_provider(provider, system="qa", detail="auto")
    cited = next(token for token in " ".join(shaped.caveats).split() if token.startswith("bw://"))
    assert cited == "bw://qa/provider/%2FIRM%2FIP_O02"

    asked: list[str] = []

    class _Spy:
        def describe(self, requested: str, object_type: Any = None) -> Any:
            asked.append(requested)
            return provider

    class _SpyRuntime(FakeRuntime):
        def providers(self, system: str) -> Any:
            return _Spy()

    server.set_runtime(_SpyRuntime())
    payload = _read(cited)
    assert asked == ["/IRM/IP_O02"]
    assert len(payload["fields"]) == 200, "the citation must lead to the full record"


def test_a_summarised_query_cites_a_uri_that_actually_resolves() -> None:
    query = Query(
        compuid="UID1",
        compid="/IMO/V_MMIM01_Q0001",
        elements=[
            QueryElement(
                eltuid=f"E{i}",
                element_type="restricted_key_figure",
                provenance=_provenance(),
            )
            for i in range(60)
        ],
        provenance=_provenance(),
    )
    shaped = server._shape_query(query, system="qa", detail="auto")
    cited = next(t for t in " ".join(shaped.caveats).split() if t.startswith("bw://"))
    assert cited == "bw://qa/query/%2FIMO%2FV_MMIM01_Q0001"

    server.set_runtime(FakeRuntime())
    # Resolves through the fixture rather than 404-ing on the slash, which is the point.
    assert isinstance(_read(cited), dict)


def test_the_system_alias_is_encoded_too() -> None:
    """A profile alias is caller-chosen, so it cannot be assumed URI-safe either."""
    assert server.resource_uri("prd/eu", "provider", "X") == "bw://prd%2Feu/provider/X"


# --- a resource failure is as informative as the same failure through a tool -----------------


class _Boom:
    """Raises the way a dropped session or a locked-down user would."""

    _MESSAGE = "connection to bwhost.internal.invalid:30015 failed"

    def describe(self, name: str, object_type: Any = None) -> Any:
        raise RuntimeError(self._MESSAGE)


class _BoomRuntime(FakeRuntime):
    def providers(self, system: str) -> Any:
        return _Boom()


def test_a_failed_resource_read_returns_a_structured_error() -> None:
    """It previously reached the client as "Error reading resource" and nothing else.

    The identical failure through `bw_describe_object` returns a code, a category, a remedy and a
    retryable flag. A caller should not have to know which surface it asked through to learn why.
    """
    server.set_runtime(_BoomRuntime())
    payload = _read("bw://qa/provider/X")
    assert payload["status"] == "error"
    assert payload["code"] == "internal_error"
    assert payload["remedy"]
    assert payload["retryable"] is False


def test_a_failed_resource_read_never_carries_the_exception_message() -> None:
    """A driver message can name the host, so the envelope forwards the class and not the text."""
    server.set_runtime(_BoomRuntime())
    serialised = json.dumps(_read("bw://qa/provider/X"))
    assert "bwhost.internal.invalid" not in serialised
    assert "30015" not in serialised
    assert "RuntimeError" in serialised  # the class is useful and safe


def test_a_resource_and_its_tool_agree_on_the_failure_code() -> None:
    """One error model across both surfaces, so a client can branch on `code` either way."""
    server.set_runtime(_BoomRuntime())

    async def call_tool() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool("bw_describe_object", {"system": "qa", "name": "X"})

    tool_body = asyncio.run(call_tool()).structured_content
    tool_body = tool_body.get("result", tool_body)
    assert _read("bw://qa/provider/X")["code"] == tool_body["code"]


def test_an_unsupported_release_still_reads_as_a_structured_result() -> None:
    """Not an exception: the release lacking a table is an answer, and resources give it too."""
    server.set_runtime(FakeRuntime())  # base fixture: chain_edges absent
    payload = _read("bw://qa/chain/DAILY_LOAD")
    assert payload["status"] == "unsupported_on_release"
    assert "chain_edges" in payload["missing"]


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
