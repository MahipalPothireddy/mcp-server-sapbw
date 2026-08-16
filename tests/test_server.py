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
from pydantic import SecretStr

from mcp_server_sapbw import server
from mcp_server_sapbw.connectors.ecc import AdtResponse, EccConnector
from mcp_server_sapbw.core.identity import StorageIdentity
from mcp_server_sapbw.core.profiles import EccProfile
from mcp_server_sapbw.core.snapshots import IN_MEMORY, SnapshotStore
from mcp_server_sapbw.models.analysis import Analysis, AnalysisConfidence
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.ecc import ConnectorUnavailable
from mcp_server_sapbw.models.provenance import Provenance, UnsupportedResult
from mcp_server_sapbw.models.providers import ObjectNotFound, Provider, ProviderField
from mcp_server_sapbw.models.security import QueryAuthExposure
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.hana import HanaRepository
from mcp_server_sapbw.repositories.health import HealthRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.queries import QueriesRepository
from mcp_server_sapbw.repositories.search import SearchRepository
from mcp_server_sapbw.repositories.security import SecurityRepository
from mcp_server_sapbw.repositories.sources import SourcesRepository
from mcp_server_sapbw.repositories.threex import ThreeXRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.server import CacheStatus, RefreshResult, SystemStatus
from mcp_server_sapbw.services.analysis import AnalysisReaders, AnalysisService
from mcp_server_sapbw.services.analyzers import Analyzers
from mcp_server_sapbw.services.docgen import DocGenerator
from mcp_server_sapbw.services.exit_analysis import ExitAnalysisService
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.load_closure import LoadClosureService
from mcp_server_sapbw.services.routine_register import RoutineRegisterService
from mcp_server_sapbw.services.snapshot import SnapshotService

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


def _capability(extra: dict[str, str] | None = None) -> CapabilityRecord:
    """The fixture release.

    ``extra`` widens it for one test. Kept opt-in rather than added to ``_TABLES``: declaring a
    table present opens every code path that reads it, and paths this fake cannot answer with the
    right column shape would fail tests that have nothing to do with the table being added.
    """
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema="TESTSCHEMA",
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical, resolved_name=physical, present=True, schema_name="TESTSCHEMA"
            )
            for logical, physical in {**_TABLES, **(extra or {})}.items()
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
        if "RSPCCHAIN" in sql:  # steps: TYPE, VARIANTE, LNR, EVENTP_START/GREEN/RED
            return [("LOADING", "DTP_1", 1, "", "", "")]
        if "RSPCPROCESSLOG" in sql:  # per-step runtimes; empty is a valid measured window
            return []
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
            if "BEXFL" in sql:  # snapshot capture: ODSOBJECT, ODSOTYPE, INFOAREA, BEXFL
                return [("SALES_DSO", "", "SALES", "X"), ("STAGE_DSO", "", "SALES", "")]
            if "ODSOTYPE" in sql:  # describe header
                return [("", "SALES", "DEVUSER", "SD")]
            return [("SALES_DSO",)]  # search-by-name query (ODSOBJECT)
        if "RSTRANSTEPROUT" in sql:  # no field routines in this fixture
            return []
        if "RSTRANFIELD" in sql:  # RULEID, PARAMTYPE, FIELDNM, KEYFLAG
            return [(1, "1", "TARGETF", "X"), (1, "0", "SOURCEF", "")]
        if "RSTRANRULE" in sql:  # RULEID, RULETYPE, AGGR, GROUPTYPE, NO_CONV
            return [(1, "DIRECT", "MOV", "S", "")]
        if "RSTRANT" in sql:
            return [("E", "Load one", "Load one target set")]
        if "RSAABAP" in sql:
            if "COUNT(*)" in sql:  # routine-register size aggregate: CODEID, line count
                return [("CODE1", 3)]
            if "CODEID IN" in sql:  # routine-register bulk source fetch: CODEID, LINE
                return [
                    ("CODE1", "METHOD start."),
                    ("CODE1", "  SELECT * FROM mara INTO TABLE lt."),
                    ("CODE1", "ENDMETHOD."),
                ]
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
        if "TRANID, SOURCETYPE, SOURCENAME" in sql:  # snapshot capture, 9 columns
            # The DataSource endpoint carries its logical system the way BW stores it, so the
            # tool-level test exercises the BDLS normalisation rather than a tidy name.
            return [
                ("TR1", "RSDS", "DS_A   BWCLNT100", "ADSO", "ADSO_T", "ACT", "C1", "", "", "", "")
            ]
        # B9 analyzer query shapes -> empty here (analyzer data is covered in test_analyzers;
        # these contract tests only prove the tool -> analyzer -> MCP-client path and shape).
        if (
            "COUNT(DISTINCT SOURCENAME)" in sql
            or "STARTROUTINE <> ''" in sql
            or "SOURCETYPE = ?" in sql
            or "SOURCETYPE IN ('ODSO', 'ADSO')" in sql
            # circular-dependency scans (self-loop / multi-object) plus the unused-provider scan
            or "SOURCENAME = TARGETNAME" in sql
            or "SOURCENAME <> TARGETNAME" in sql
            or "GROUP BY" in sql
        ):
            return []
        if "OBJSTAT" in sql:  # get_transformation / get_routine_code header (12 cols)
            return [("ACT", "RSDS", "", "DS_A", "ADSO", "", "ADSO_T", "CODE1", "", "", "", "")]
        # Routine-register ownership scan: TRANID, endpoints, 5 routine slots. Checked after the
        # header query, which also selects GLBCODE2 but is identified by OBJSTAT.
        if "GLBCODE2" in sql:
            return [("TR1", "DS_A", "ADSO_T", "CODE1", "", "", "", "")]
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
    def __init__(self, *, extra_tables: dict[str, str] | None = None) -> None:
        self._cap = _capability(extra_tables)
        self._snapshot_store: SnapshotStore | None = None

    def list_systems(self) -> list[SystemStatus]:
        identity = self.identity("qa")
        return [
            SystemStatus(
                name="qa",
                status="discovered",
                release="7.50",
                read_only_user=True,
                tenant=identity.tenant,
                environment=identity.environment,
                label=identity.label,
                isolated_by_tenant=identity.isolated_by_tenant,
            )
        ]

    def identity(self, system: str) -> StorageIdentity:
        """A tenant is set in the fixture so the isolation path is the one exercised."""
        return StorageIdentity(system=system, tenant="acme", environment="qa")

    def capability(self, system: str) -> CapabilityRecord:
        return self._cap

    def refresh_capabilities(self, system: str) -> CapabilityRecord:
        return self._cap

    def refresh_cache(self, system: str, scope: str) -> RefreshResult:
        return RefreshResult(system=system, scope=scope, removed=0)

    def cache_status(self, system: str) -> CacheStatus:
        return CacheStatus(system=system, enabled=True, location="/tmp/cache")

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

    def threex(self, system: str) -> ThreeXRepository:
        return ThreeXRepository(_Conn(), self._cap)

    def security(self, system: str) -> SecurityRepository:
        return SecurityRepository(_Conn(), self._cap)

    def query_auth_exposure(self, system: str, query: str) -> QueryAuthExposure | UnsupportedResult:
        return self.security(system).query_exposure(
            compuid=query, compid=query, providers=[], characteristics=[]
        )

    def analyzers(self, system: str) -> Analyzers:
        return Analyzers(_Conn(), self._cap)

    def docgen(self, system: str) -> DocGenerator:
        return DocGenerator(_Conn(), self._cap)

    def load_closure(self, system: str) -> LoadClosureService:
        return LoadClosureService(_Conn(), self._cap)

    def health(self, system: str) -> HealthRepository:
        return HealthRepository(_Conn(), self._cap)

    def sources(self, system: str) -> SourcesRepository:
        return SourcesRepository(_Conn(), self._cap)

    def routine_register(self, system: str) -> RoutineRegisterService:
        return RoutineRegisterService(_Conn(), self._cap)

    def analysis(self, system: str) -> AnalysisService:
        """Every reader an analysis composes, over the one scripted connection.

        Built the way the real runtime builds it - one instance per reader, shared across sections -
        so the fixture exercises the memoisation path rather than a shape the server never uses.
        """
        conn = _Conn()
        return AnalysisService(
            AnalysisReaders(
                system=system,
                capability=self._cap,
                providers=ProvidersRepository(conn, self._cap),
                lineage=LineageService(conn, self._cap),
                transformations=TransformationsRepository(conn, self._cap),
                queries=QueriesRepository(conn, self._cap),
                chains=ChainsRepository(conn, self._cap),
                load_closure=LoadClosureService(conn, self._cap),
                health=HealthRepository(conn, self._cap),
                hana=HanaRepository(conn, self._cap),
                query_auth_exposure=lambda query: self.query_auth_exposure(system, query),
            )
        )

    def snapshots(self, system: str) -> SnapshotService:
        return SnapshotService(_Conn(), self._cap)

    def snapshot_store(self, system: str) -> SnapshotStore | None:
        """An in-memory store, fresh per fake runtime.

        Not ``None``: returning nothing would exercise only the no-store branch, and the store is
        where the round trip that carries a snapshot between two calls actually happens. In memory
        rather than in a temporary file so the suite writes no customer-shaped metadata to disk and
        nothing has to be cleaned up afterwards; on-disk behaviour is covered in test_snapshot.
        """
        if self._snapshot_store is None:
            self._snapshot_store = SnapshotStore(IN_MEMORY)
        return self._snapshot_store

    def exit_analysis(self, ecc_system: str | None) -> ExitAnalysisService | ConnectorUnavailable:
        """No source system is configured in the fixture, mirroring a BW-only install."""
        return ConnectorUnavailable(
            configured_profiles=[],
            detail="no ABAP source system is configured; add an 'ecc_systems' entry",
        )


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


def test_capability_report_via_client_separates_absent_from_unimplemented() -> None:
    """The support profile reaches the client with a verdict per capability, and cites its basis."""
    server.set_runtime(FakeRuntime())
    result = asyncio.run(_call("bw_capability_report", {"system": "qa"}))
    body = _report_body(result)

    assert body["system"] == "qa"
    assert body["bw_release"] == "7.50"
    assert sum(body["totals"].values()) == len(body["capabilities"])
    # The fixture record marks these 22 tables present, and they are all read by the server.
    usable = {r["capability"] for r in body["capabilities"] if r["verdict"] == "usable"}
    assert _TABLES.keys() <= usable
    assert any("contract revision" in c for c in body["caveats"])


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


# --- compound analysis --------------------------------------------------------------------


def test_analyze_object_via_client() -> None:
    """The composed answer reaches the client with its audit trail and confidence intact."""
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_analyze_object", {"system": "qa", "name": "SALES_DSO"}))
    )
    assert body["kind"] == "object"
    assert body["subject_name"] == "SALES_DSO"
    assert body["summary"]
    assert body["steps"], "no audit trail reached the client"
    assert body["confidence"]["level"] in ("high", "medium", "low")
    assert body["confidence"]["reasons"]


def test_analyze_query_via_client() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_analyze_query", {"system": "qa", "query": "QRY1"})))
    assert body["kind"] == "query"
    assert body["subject_name"] == "QRY1"
    assert body["query"]["compid"] == "QRY1"
    assert any(step["section"] == "security" for step in body["steps"])


#: Chain structure and per-step runtimes, which the base fixture leaves absent. Declared only for
#: the chain tests, so the two tables' code paths do not open for every other test.
_CHAIN_TABLES = {"chain_edges": "RSPCCHAIN", "process_log": "RSPCPROCESSLOG"}


def test_analyze_process_chain_via_client() -> None:
    server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
    body = _report_body(
        asyncio.run(_call("bw_analyze_process_chain", {"system": "qa", "chain_id": "DAILY_LOAD"}))
    )
    assert body["kind"] == "process_chain"
    assert body["chain"]["chain_id"] == "DAILY_LOAD"
    assert any(step["section"] == "runtimes" for step in body["steps"])


def test_analyze_process_chain_reports_an_absent_table_as_unsupported() -> None:
    """Not as 'no such chain'. The two have different remedies, and only one is about the chain.

    Without this distinction a release that cannot report chain structure sends the caller hunting
    for a typo in an id that is spelled correctly.
    """
    server.set_runtime(FakeRuntime())  # base fixture: chain_edges absent
    body = _report_body(
        asyncio.run(_call("bw_analyze_process_chain", {"system": "qa", "chain_id": "DAILY_LOAD"}))
    )
    assert body["code"] == "unsupported_on_release"
    # The logical name is reported, because an absent table has no resolved physical name to give.
    assert "chain_edges" in body["missing"]


def test_assess_change_impact_via_client() -> None:
    """The pre-transport checklist is generated from what was found, and opens with a snapshot."""
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_assess_change_impact", {"system": "qa", "name": "ADSO_T"}))
    )
    assert body["kind"] == "change_impact"
    tools = [action["tool"] for action in body["next_actions"]]
    assert tools[0] == "bw_create_snapshot"
    assert "bw_find_layer_violations" in tools
    assert "bw_check_load_latency" in tools


def test_troubleshoot_missing_data_via_client() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_troubleshoot_missing_data", {"system": "qa", "target": "QRY1"}))
    )
    assert body["kind"] == "missing_data"
    assert any(step["section"] == "subject" for step in body["steps"])
    assert body["next_actions"]


def test_a_compound_answer_bounds_its_embedded_payloads() -> None:
    """A composed answer embeds several payloads at once, so the bound matters more here, not less.

    Measured live before this was applied: 79 KiB for one object analysis and 90 KiB for one query,
    dominated by a 171-field provider and two full lineage graphs. The bounds are the same ones
    ``bw_describe_object`` and ``bw_get_lineage`` apply, so a caller learns one rule.
    """
    wide = Provider(
        name="WIDE_DSO",
        object_type="dso",
        key_field_names=["K"],
        fields=[
            ProviderField(
                name=("K" if i == 0 else f"F{i:03d}"),
                position=i,
                is_key=i == 0,
                provenance=Provenance(source_table="RSDODSOIOBJ"),
            )
            for i in range(120)
        ],
        provenance=Provenance(source_table="RSDODSO"),
    )
    analysis = Analysis(
        kind="object",
        system="qa",
        subject_name="WIDE_DSO",
        title="t",
        definition=wide,
        confidence=AnalysisConfidence(level="high"),
    )

    trimmed = server._shape_analysis(analysis, system="qa", detail="auto")
    assert isinstance(trimmed, Analysis) and trimmed.definition is not None
    assert len(trimmed.definition.fields) < 120
    assert any("field list summarised" in c for c in trimmed.definition.caveats)
    # The resource URI holding the whole record is named, so nothing becomes unreachable.
    assert any("bw://qa/provider/WIDE_DSO" in c for c in trimmed.definition.caveats)

    whole = server._shape_analysis(analysis, system="qa", detail="full")
    assert isinstance(whole, Analysis) and whole.definition is not None
    assert len(whole.definition.fields) == 120


def test_shaping_leaves_a_not_found_untouched() -> None:
    """The shaper runs on every return, so it has to pass the failure branches through."""
    missing = ObjectNotFound(name="X", detail="nope")
    assert server._shape_analysis(missing, system="qa", detail="auto") is missing


def test_a_compound_tool_reports_a_missing_subject_as_not_found() -> None:
    """An envelope of nulls would read as 'this object has no dependencies'."""
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_analyze_query", {"system": "qa", "query": "NOSUCHQUERY"}))
    )
    assert body["code"] == "object_not_found"


def test_compound_tools_cite_only_registered_tools() -> None:
    """Across all five: every audit row and next action must name a tool a client can call.

    This is what makes a composed answer auditable rather than merely detailed - each section can be
    re-run on its own. A drifted name silently turns that promise into an unknown-tool error.
    """
    server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
    registered = set(asyncio.run(_list_tool_names()))
    cited: set[str] = set()
    for tool, args in (
        ("bw_analyze_object", {"system": "qa", "name": "SALES_DSO"}),
        ("bw_analyze_query", {"system": "qa", "query": "QRY1"}),
        ("bw_analyze_process_chain", {"system": "qa", "chain_id": "DAILY_LOAD"}),
        ("bw_assess_change_impact", {"system": "qa", "name": "ADSO_T"}),
        ("bw_troubleshoot_missing_data", {"system": "qa", "target": "QRY1"}),
    ):
        body = _report_body(asyncio.run(_call(tool, args)))
        cited |= {step["tool"] for step in body["steps"]}
        cited |= {a["tool"] for a in body["next_actions"] if a["tool"]}
    assert cited, "no tools were cited by any compound answer"
    assert cited <= registered, f"cited but not registered: {sorted(cited - registered)}"


# --- snapshots and environment comparison -------------------------------------------------


def test_create_snapshot_via_client() -> None:
    server.set_runtime(FakeRuntime())
    result = asyncio.run(
        _call("bw_create_snapshot", {"system": "qa", "families": ["providers", "transformations"]})
    )
    body = _report_body(result)
    assert body["system"] == "qa"
    assert body["snapshot_id"].startswith("qa-")
    assert {o["ref"]["id"] for o in body["objects"]} >= {"dso:SALES_DSO", "transformation:TR1"}
    assert body["scope"]["families"] == ["providers", "transformations"]
    # The DataSource endpoint's logical system is stripped from the edge, not carried into it.
    assert body["edges"][0]["src"] == "datasource:DS_A"


def test_create_snapshot_rejects_an_unknown_family_by_name() -> None:
    """A misspelled family must not silently produce a snapshot missing that family."""
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_create_snapshot", {"system": "qa", "families": ["dsos"]}))
    )
    assert body["code"] == "invalid_argument"
    assert "unknown snapshot families" in body["message"]
    assert "providers" in body["message"]  # names the valid set rather than just refusing


def test_list_snapshots_via_client() -> None:
    server.set_runtime(FakeRuntime())
    asyncio.run(_call("bw_create_snapshot", {"system": "qa", "families": ["providers"]}))
    body = _report_body(asyncio.run(_call("bw_list_snapshots", {"system": "qa"})))
    assert body["snapshots"], "a stored snapshot did not come back from listing"
    assert body["snapshots"][0]["object_count"] > 0
    assert "objects" not in body["snapshots"][0]  # summaries only


def test_list_snapshots_without_a_system_says_which_one_to_name() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_list_snapshots", {})))
    assert body["snapshots"] == []
    assert any("per profile" in c for c in body["caveats"])


def test_compare_snapshots_against_the_system_as_it_is_now() -> None:
    """The 'what changed since then' path: one stored id, no second capture by the caller."""
    server.set_runtime(FakeRuntime())
    created = _report_body(
        asyncio.run(_call("bw_create_snapshot", {"system": "qa", "families": ["providers"]}))
    )
    body = _report_body(
        asyncio.run(_call("bw_compare_snapshots", {"system": "qa", "left": created["snapshot_id"]}))
    )
    assert body["counts"]["changed"] == 0
    assert body["counts"]["unchanged"] > 0
    assert body["families_compared"] == ["providers"]


def test_compare_snapshots_reports_an_unknown_id_as_not_found() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_compare_snapshots", {"system": "qa", "left": "qa-19700101T000000Z"}))
    )
    assert body["code"] == "object_not_found"


def test_compare_systems_via_client() -> None:
    """Two profiles, captured and diffed in one call, with the corrections applied stated."""
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(
            _call(
                "bw_compare_systems",
                {"left_system": "qa", "right_system": "prd", "families": ["providers"]},
            )
        )
    )
    assert body["left"]["system"] == "qa" and body["right"]["system"] == "prd"
    joined = " ".join(body["normalisations"])
    assert "logical system" in joined and "fingerprint" in joined


def test_cache_status_reports_snapshots_alongside_the_cache() -> None:
    """A snapshot is the larger body of customer metadata at rest, so it has to be reported."""
    runtime = FakeRuntime()
    server.set_runtime(runtime)
    body = _report_body(asyncio.run(_call("bw_cache_status", {"system": "qa"})))
    assert "snapshots" in body and "snapshot_size_bytes" in body


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
    assert body["connector_required"] == "BI platform"  # no BI connector configured
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


def test_extractor_exit_code_reports_an_unconfigured_connector() -> None:
    """With no source system configured the tool says how to configure it, and does not guess."""
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_get_extractor_exit_code", {})))
    assert body["status"] == "connector_not_configured"
    assert body["connector"] == "ecc"
    assert body["configured_profiles"] == []
    assert "ecc_systems" in body["detail"]


def test_extractor_exit_code_returns_the_inventory_when_configured() -> None:
    """A configured source system yields all four slots with per-branch risk attribution."""
    abap = (
        "FUNCTION EXIT_SAPLRSAP_001.\n"
        "  CASE i_datasource.\n"
        "    WHEN 'DS_A'.\n"
        "      SELECT f FROM tbl_a INTO lv.\n"
        "  ENDCASE.\n"
        "ENDFUNCTION.\n"
    )

    class Fetcher:
        def get_text(self, path: str, params: Any) -> AdtResponse:
            return AdtResponse(200, abap) if "zxrsau01" in path else AdtResponse(404, "")

    class EccRuntime(FakeRuntime):
        def exit_analysis(
            self, ecc_system: str | None
        ) -> ExitAnalysisService | ConnectorUnavailable:
            profile = EccProfile(
                name="src",
                host="src.example.invalid",
                port=44300,
                client="300",
                user="reader",
                password=SecretStr("pw"),  # pragma: allowlist secret
            )
            return ExitAnalysisService(EccConnector(profile, Fetcher()))

    server.set_runtime(EccRuntime())
    body = _report_body(asyncio.run(_call("bw_get_extractor_exit_code", {"ecc_system": "src"})))
    assert body["profile"] == "src"
    assert body["client"] == "300"
    assert len(body["exits"]) == 4
    assert body["available_count"] == 1
    assert body["handled_datasources"] == ["DS_A"]
    # No host anywhere in the payload (mission Rule 5).
    assert "src.example.invalid" not in str(body)
    # Source text is opt-in.
    assert body["exits"][0]["source"] is None


def test_routine_register_via_client() -> None:
    """The portfolio register reaches the client with its budget and completeness reported."""
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_get_routine_register", {"system": "qa"})))
    assert body["parse_budget"] > 0
    assert body["total_routines"] >= 1
    entry = body["entries"][0]
    assert entry["code_id"] == "CODE1"
    assert entry["transformation_id"] == "TR1"
    assert entry["kind"] == "start"
    # Provenance cites the row the fact came from.
    assert entry["provenance"]["source_table"] == "RSAABAP"
    assert any("lower bound" in c for c in body["caveats"])


def test_routine_register_clamps_paging() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_get_routine_register", {"system": "qa", "limit": 99999}))
    )
    assert body["limit"] <= 500


def test_find_unused_providers_via_client() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_find_unused_providers", {"system": "qa"})))
    assert body["scenario"] == "unused_providers"
    # The external-consumption gap must be stated wherever this is surfaced.
    assert any("bw_get_hana_crossings" in c for c in body["caveats"])


def test_list_queries_exposes_origin() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(asyncio.run(_call("bw_list_queries", {"system": "qa"})))
    assert body["items"]
    assert all(item["origin"] in ("designed", "ad_hoc") for item in body["items"])


def test_review_scenario_accepts_unused_providers() -> None:
    server.set_runtime(FakeRuntime())
    body = _report_body(
        asyncio.run(_call("bw_review_scenario", {"system": "qa", "scenario": "unused_providers"}))
    )
    assert body["scenario"] == "unused_providers"
