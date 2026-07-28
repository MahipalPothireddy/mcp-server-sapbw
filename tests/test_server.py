"""Contract tests for the MCP surface (B3), offline via an in-memory FastMCP client.

Drives the real registered tools against a fixture-backed fake runtime — proving the
core -> repository -> tool -> MCP-client path end to end without a live system.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.hana import HanaRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.queries import QueriesRepository
from mcp_server_sapbw.repositories.search import SearchRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.server import RefreshResult, SystemStatus
from mcp_server_sapbw.services.analyzers import Analyzers
from mcp_server_sapbw.services.docgen import DocGenerator
from mcp_server_sapbw.services.lineage import LineageService

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
    "dtp": "RSBKDTP",
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "element_range": "RSZRANGE",
    "element_select": "RSZSELECT",
    "global_variable": "RSZGLOBV",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "hana_views": "VIEWS",
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
        params = list(parameters or [])
        # HANA branches first: they answer their own count queries, so they must precede
        # the generic TOTAL_COUNT interceptor below (which serves every other count query).
        if '"VIEWS"' in sql:  # HANA calc-view list
            return [(2,)] if "TOTAL_COUNT" in sql else [("CV1", "CALC"), ("CV2", "JOIN")]
        if "OBJECT_DEPENDENCIES" in sql:
            return [(0,)] if "TOTAL_COUNT" in sql else []
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "RSPCCHAINATTR" in sql:
            return [("DAILY_LOAD", "FINANCE", "ACT")]
        if "RSPCCHAINT" in sql:
            return [("DAILY_LOAD", "Daily finance load")]
        if "RSPCLOGCHAIN" in sql:
            if "ZEIT" in sql:  # median-start-times query (CHAIN_ID, ZEIT)
                return [("DAILY_LOAD", "080000")]
            # run summary (GROUP BY): 30 runs across 30 days -> daily
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
        if "RSZCOMPDIR" in sql:
            if "TSTPNM" in sql:  # header
                ident = str(params[-1])
                return (
                    [("Q1UID", "QRY1", "DEV", "DEVUSER", "20260101000000", "ACT")]
                    if ident
                    in (
                        "QRY1",
                        "Q1UID",
                    )
                    else []
                )
            return [("Q1UID", "QRY1", "DEV", "20260101000000")]  # list
        if "RSZCOMPIC" in sql:
            if "INFOCUBE = ?" in sql:
                return []
            if "COMPUID IN" in sql:
                return [("Q1UID", "PROV1", "X")]
            return [("PROV1", "X")]
        if "RSZELTXREF" in sql:
            return []  # root has no children in this fixture
        if "RSZELTDIR" in sql:
            return [("Q1UID", "REP", "QRY1", "X")]
        if "RSZELTTXT" in sql:
            return [("Q1UID", "Qry1", "Query one description")]
        if "RSZRANGE" in sql or "RSZSELECT" in sql or "RSZGLOBV" in sql:
            return []
        if "RSBKDTP" in sql:  # no DTP edges in this fixture
            return []
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    @staticmethod
    def _rstran(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        # Synthetic flow: DS_A --TR1--> ADSO_T (TR1 has start routine CODE1).
        name = str(params[-1]) if params else ""
        # B9 analyzer query shapes -> empty here (analyzer data is covered in test_analyzers;
        # these contract tests only prove the tool -> analyzer -> MCP-client path and shape).
        if (
            "COUNT(DISTINCT SOURCENAME)" in sql
            or "STARTROUTINE <> ''" in sql
            or "SOURCETYPE = ?" in sql
            or "SOURCETYPE IN ('ODSO', 'ADSO')" in sql
        ):
            return []
        if "OBJSTAT" in sql:  # get_transformation / get_routine_code header (12 cols)
            return [("ACT", "RSDS", "", "DS_A", "ADSO", "", "ADSO_T", "CODE1", "", "", "", "")]
        if "STARTROUTINE IN" in sql or "TRANID IN" in sql:  # reverse impact search
            return []
        if "SOURCENAME = ?" in sql:  # lineage downstream
            return [("ADSO_T", "ADSO", "TR1")] if name == "DS_A" else []
        if "TARGETNAME = ?" in sql:
            if "SOURCENAME" in sql:  # lineage upstream
                return [("DS_A", "RSDS", "TR1")] if name == "ADSO_T" else []
            return [("TR1",)] if name == "ADSO_T" else []  # transformations_targeting
        return [("TR1", "RSDS", "DS_A", "ADSO", "ADSO_T", "CODE1", "", "")]  # list (8 cols)


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

    def lineage(self, system: str) -> LineageService:
        return LineageService(_Conn(), self._cap)

    def queries(self, system: str) -> QueriesRepository:
        return QueriesRepository(_Conn(), self._cap)

    def hana(self, system: str) -> HanaRepository:
        return HanaRepository(_Conn(), self._cap)

    def analyzers(self, system: str) -> Analyzers:
        return Analyzers(_Conn(), self._cap)

    def docgen(self, system: str) -> DocGenerator:
        return DocGenerator(_Conn(), self._cap)


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


def test_get_lineage_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_get_lineage", {"system": "qa", "name": "DS_A", "direction": "downstream"})
    )
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    names = {n["name"] for n in body["nodes"]}
    assert {"DS_A", "ADSO_T"} <= names
    assert any(e["src"] == "DS_A" and e["dst"] == "ADSO_T" for e in body["edges"])


def test_trace_to_source_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_trace_to_source", {"system": "qa", "name": "ADSO_T"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert "DS_A" in body["datasources_reached"]


def test_get_query_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_get_query", {"system": "qa", "query": "QRY1"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["compid"] == "QRY1"
    assert body["description"] == "Query one description"
    assert body["provider"] == "PROV1"


def test_get_query_usage_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_get_query_usage", {"system": "qa", "query": "QRY1"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["last_used"] is not None


def test_list_calc_views_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_list_calc_views", {"system": "qa"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["total_count"] == 2
    assert {v["name"] for v in body["items"]} == {"CV1", "CV2"}


def test_hana_crossings_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_get_hana_crossings", {"system": "qa"}))
    payload = result.structured_content
    body = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert body["total_count"] == 0  # empty fixture; the tool path works end to end


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


def _report_body(result: Any) -> Any:
    payload = result.structured_content
    return payload["result"] if isinstance(payload, dict) and "result" in payload else payload


def test_check_load_latency_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_check_load_latency", {"system": "qa"}))
    body = _report_body(result)
    assert body["scenario"] == "9.1"
    assert "findings" in body and "caveats" in body


def test_check_schedule_risk_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_check_schedule_risk", {"system": "qa"}))
    body = _report_body(result)
    assert body["scenario"] == "9.7"
    assert body["connector_required"] == "Tableau/BOBJ"  # no BI connector configured
    assert body["findings"][0]["unpopulated_reason"] is not None


def test_find_layer_violations_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_find_layer_violations", {"system": "qa"}))
    body = _report_body(result)
    assert body["scenario"] == "layer_violations"


def test_review_scenario_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_review_scenario", {"system": "qa", "scenario": "9.3"}))
    body = _report_body(result)
    assert body["scenario"] == "9.3"


def test_prompts_registered() -> None:
    names = asyncio.run(_list_prompt_names())
    assert {
        "analyze_impact",
        "troubleshoot_missing_data",
        "document_dataflow",
        "review_scenario",
        "onboard_analyst",
        "pre_change_checklist",
    } <= set(names)


async def _list_prompt_names() -> list[str]:
    async with Client(server.mcp) as client:
        prompts = await client.list_prompts()
        return [p.name for p in prompts]


def test_generate_docs_via_client(tmp_path: Any) -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_generate_docs", {"system": "qa", "output_dir": str(tmp_path / "kb")})
    )
    body = _report_body(result)
    assert body["page_count"] > 0
    assert body["gaps_count"] > 0
    assert (tmp_path / "kb" / "index.md").is_file()
    assert (tmp_path / "kb" / "99-gaps-and-risks.md").is_file()


def test_render_lineage_via_client(tmp_path: Any) -> None:
    """The tool returns displayable image content plus structured completeness metadata."""
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call(
            "bw_render_lineage",
            {"system": "qa", "name": "ADSO_T", "depth": 2, "output_dir": str(tmp_path)},
        )
    )
    body = _report_body(result)
    assert body["root"] == "ADSO_T"
    assert body["node_count"] >= 1
    assert body["image_format"] in ("png", "svg")
    # A vector copy was written where asked.
    assert body["svg_path"] and Path(body["svg_path"]).is_file()
    svg = Path(body["svg_path"]).read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    # Image content is present for inline display.
    assert result.content, "no content blocks returned"


def test_render_lineage_rejects_absurd_depth() -> None:
    """Depth is clamped so a diagram request can never fan out without bound."""
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_render_lineage", {"system": "qa", "name": "ADSO_T", "depth": 9999})
    )
    body = _report_body(result)
    assert body["depth"] <= 8
