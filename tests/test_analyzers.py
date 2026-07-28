"""Tests for the risk-scenario analyzers (B9), offline against a scripted landscape.

Synthetic names only. The one /BIC/ table a full-update routine reads is built by concatenation so
this .py file stays clean for the customer-metadata scan.

Landscape:
  9.3 CP->DSO:      CP_SALES (HCPR) --TR_CP1--> EDW_DSO (ODSO)
  9.4 IOBJ<-CP:     CP_MAT   (HCPR) --TR_CP2--> MATERIAL (IOBJ)
  9.5 merged DSO:   ORD_DSO / BILL_DSO / SHIP_DSO --TR_M1/2/3--> MERGE_DSO
                    (TR_M1 & TR_M2 both write field AMOUNT -> collision)
  9.2 deep stack:   L1_DSO --> L2_DSO --> L3_DSO --> L4_DSO  (3 hops)
  9.1 full-update:  DS_SRC --TR_FULL(start routine reads a generated DSO table)--> FULL_DSO,
                    loaded UPDMODE='F'; DS_SRC extractor DELTA='ADD'
  9.6 heuristic:    DS_ENH has 5 customer-namespace fields
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from mcp_server_sapbw.connectors.base import ConnectorRegistry
from mcp_server_sapbw.connectors.ecc import AdtResponse, EccConnector
from mcp_server_sapbw.core.profiles import EccProfile
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.analyzers import Analyzers

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
    "extractor": "ROOSOURCE",
    "chain_attr": "RSPCCHAINATTR",
    "log_chain": "RSPCLOGCHAIN",
}

_BIC_LOOKUP = "/BIC/" + "ALOOKUP00"  # concatenated -> resolves to DSO LOOKUP; scan stays clean

# get_transformation 12-col header: OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME,
# START, END, EXPERT, GLB, GLB2
_HEADER: dict[str, tuple[Any, ...]] = {
    "TR_M1": ("ACT", "ODSO", "", "ORD_DSO", "ODSO", "", "MERGE_DSO", "", "", "", "", ""),
    "TR_M2": ("ACT", "ODSO", "", "BILL_DSO", "ODSO", "", "MERGE_DSO", "", "", "", "", ""),
    "TR_M3": ("ACT", "ODSO", "", "SHIP_DSO", "ODSO", "", "MERGE_DSO", "", "", "", "", ""),
    "TR_FULL": ("ACT", "RSDS", "", "DS_SRC", "ODSO", "", "FULL_DSO", "CODE_FULL", "", "", "", ""),
    # Same full-update target, but its routine reads nothing resolvable -> no latency contract.
    "TR_NONE": ("ACT", "RSDS", "", "DS_SRC", "ODSO", "", "FULL_DSO", "CODE_NONE", "", "", "", ""),
    # Several looked-up objects -> escalated severity.
    "TR_MANY": ("ACT", "RSDS", "", "DS_SRC", "ODSO", "", "FULL_DSO", "CODE_MANY", "", "", "", ""),
}
# RULEID, RULETYPE, AGGR, GROUPTYPE, NO_CONV
_RULES: dict[str, list[tuple[Any, ...]]] = {
    "TR_M1": [(1, "DIRECT", "MOV", "S", "")],
    "TR_M2": [(1, "DIRECT", "MOV", "S", "")],
    "TR_M3": [(1, "DIRECT", "MOV", "S", "")],
}
# RULEID, PARAMTYPE, FIELDNM, KEYFLAG
_FIELDS: dict[str, list[tuple[Any, ...]]] = {
    "TR_M1": [(1, 1, "AMOUNT", ""), (1, 0, "S_AMT", "")],
    "TR_M2": [(1, 1, "AMOUNT", ""), (1, 0, "B_AMT", "")],  # AMOUNT collides with TR_M1
    "TR_M3": [(1, 1, "QTY", ""), (1, 0, "S_QTY", "")],
}
_BIC_MANY = ["/BIC/" + f"AL{n}DSO00" for n in (1, 2, 3)]  # -> L1DSO / L2DSO / L3DSO
_RSAABAP = {
    "CODE_FULL": ["METHOD start.", "  SELECT * FROM " + _BIC_LOOKUP + " INTO lt.", "ENDMETHOD."],
    # A plain (non-generated) table: parsed as a dependency, but resolves to no BW object.
    "CODE_NONE": ["METHOD start.", "  SELECT * FROM mara INTO TABLE lt.", "ENDMETHOD."],
    "CODE_MANY": [
        "METHOD start.",
        *(f"  SELECT * FROM {table} INTO lt." for table in _BIC_MANY),
        "ENDMETHOD.",
    ],
}
_MERGED_GROUP = [
    ("MERGE_DSO", 3),
    ("EDW_DSO", 1),
    ("FULL_DSO", 1),
    ("L2_DSO", 1),
    ("L3_DSO", 1),
    ("L4_DSO", 1),
]
_DSO_EDGES = [
    ("ORD_DSO", "MERGE_DSO"),
    ("BILL_DSO", "MERGE_DSO"),
    ("SHIP_DSO", "MERGE_DSO"),
    ("L1_DSO", "L2_DSO"),
    ("L2_DSO", "L3_DSO"),
    ("L3_DSO", "L4_DSO"),
]
_INBOUND = {
    "MERGE_DSO": [("TR_M1", "ORD_DSO"), ("TR_M2", "BILL_DSO"), ("TR_M3", "SHIP_DSO")],
}
# Write-back shapes. SELF_DSO is written from itself; A_DSO and B_DSO each feed the other.
# TRANID, SOURCENAME, TARGETNAME, TARGETTYPE
_SELF_LOOPS = [("TR_SELF", "SELF_DSO", "SELF_DSO", "ODSO")]
# Every source <> target edge, which is what the two-cycle scan reads.
_ALL_EDGES = [
    ("TR_AB", "A_DSO", "B_DSO"),
    ("TR_BA", "B_DSO", "A_DSO"),
    ("TR_M1", "ORD_DSO", "MERGE_DSO"),  # one-way: must not be reported
    ("TR_L1", "L1_DSO", "L2_DSO"),
]


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "RSDSSEGFD" in sql:  # 9.6 enhancement inventory
            if "TOTAL_COUNT" in sql:
                return [(3,)]  # 3 DataSources in total
            if "FIELDNM" in sql and "COUNT(*)" not in sql:  # the appended field names
                return [("DS_ENH", "ZZ_A"), ("DS_ENH", "ZZ_B")]
            return [("DS_ENH", 5)]  # DATASOURCE, count of customer-namespace fields
        if '"RSDS"' in sql:  # DataSource detail: DATASOURCE, LOGSYS, TYPE, DELTA, APPLNM
            return [("DS_ENH", "SRCCLNT100", "D", "ADD", "SD")]
        if "ROOSOURCE" in sql:  # 9.1 extractor delta method
            return [("ADD",)] if params and str(params[0]) == "DS_SRC" else []
        if "RSBKDTP" in sql:
            if "DISTINCT TGT" in sql:
                return [("FULL_DSO",)]
            if "TGT = ?" in sql:  # full-update source
                return [("DS_SRC", "RSDS")]
            return []
        if "RSAABAP" in sql:
            code = str(params[0]) if params else ""
            return [(line,) for line in _RSAABAP.get(code, [])]
        if "RSPCCHAINATTR" in sql:  # 9.7 schedule matrix (empty landscape)
            return [(0,)] if "TOTAL_COUNT" in sql else []
        if "RSPCLOGCHAIN" in sql or "RSPCPROCESSLOG" in sql:
            return []
        if "RSTRANSTEPROUT" in sql:
            return []
        if "RSTRANRULE" in sql:
            return _RULES.get(str(params[0]), []) if params else []
        if "RSTRANFIELD" in sql:
            return _FIELDS.get(str(params[0]), []) if params else []
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    @staticmethod
    def _rstran(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "OBJSTAT" in sql:  # get_transformation / get_routine_code header
            header = _HEADER.get(str(params[0])) if params else None
            return [header] if header else []
        if "COUNT(DISTINCT SOURCENAME)" in sql:  # 9.5 stream counts
            # Guard: an aggregate select is only valid with a GROUP BY (HANA rejects it otherwise);
            # returning [] when it is missing makes the offline test catch that regression.
            return _MERGED_GROUP if "GROUP BY" in sql else []
        if "SOURCENAME = TARGETNAME" in sql:  # write-back self-loops
            return _SELF_LOOPS
        if "SOURCENAME <> TARGETNAME" in sql:  # write-back cycle scan (all edges)
            return _ALL_EDGES
        if "STARTROUTINE <> ''" in sql:  # 9.1 routine-bearing transformations into DSOs
            return [("TR_FULL", "FULL_DSO"), ("TR_MANY", "FULL_DSO"), ("TR_NONE", "FULL_DSO")]
        if "SOURCETYPE IN ('ODSO', 'ADSO')" in sql:  # DSO->DSO edges (9.2 / layer)
            return _DSO_EDGES
        if "SOURCETYPE = ?" in sql:  # CP edges (9.3 / 9.4 / layer)
            if "TARGETTYPE = ?" in sql:  # CP -> InfoObject
                return [("TR_CP2", "CP_MAT", "MATERIAL")]
            return [("TR_CP1", "CP_SALES", "EDW_DSO")]  # CP -> DSO
        if "TARGETNAME = ?" in sql:  # 9.5 inbound streams
            return _INBOUND.get(str(params[0]), [])
        return []


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _analyzers(present: set[str] | None = None) -> Analyzers:
    return Analyzers(ScriptedConnection(), _capability(present))


def test_composite_provider_to_dso() -> None:
    report = _analyzers().composite_provider_to_dso()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.3"
    assert report.finding_count == 1
    finding = report.findings[0]
    assert finding.affected_objects == ["CP_SALES", "EDW_DSO"]
    assert "activation" in finding.recommendation.lower()
    assert finding.evidence[0].source_table == "RSTRAN"


def test_infoobject_from_composite_provider() -> None:
    report = _analyzers().infoobject_from_composite_provider()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.4"
    assert report.finding_count == 1
    assert report.findings[0].affected_objects == ["CP_MAT", "MATERIAL"]
    assert any("bw_get_load_closure" in c for c in report.caveats)


def test_merged_stream_dso_detects_collision() -> None:
    report = _analyzers().merged_stream_dso()
    assert not isinstance(report, UnsupportedResult)
    assert report.finding_count == 1
    finding = report.findings[0]
    assert finding.metrics["target_dso"] == "MERGE_DSO"
    assert finding.metrics["stream_count"] == 3
    assert finding.metrics["collision_fields"] == ["AMOUNT"]
    assert finding.severity == "high"  # a collision escalates severity
    assert set(finding.metrics["streams"]) == {"ORD_DSO", "BILL_DSO", "SHIP_DSO"}


def test_check_load_latency_finds_routine_lookup() -> None:
    report = _analyzers().check_load_latency()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.1"
    by_tran = {f.metrics["tran_id"]: f for f in report.findings}
    finding = by_tran["TR_FULL"]
    assert finding.metrics["target"] == "FULL_DSO"
    assert "LOOKUP" in finding.metrics["looked_up_objects"]
    assert finding.metrics["extractor_delta_method"] == "ADD"


def test_check_load_latency_excludes_loads_with_no_resolvable_lookup() -> None:
    """Regression: a full-update load whose routines resolve no lookup has no latency contract to
    check, but was still emitted as a finding — crowding real risks out of the page."""
    report = _analyzers().check_load_latency()
    assert not isinstance(report, UnsupportedResult)
    assert "TR_NONE" not in {f.metrics["tran_id"] for f in report.findings}
    assert report.analyzed_count == 3  # it was examined, not skipped silently
    assert report.finding_count == 2
    assert any("1 of 3 evaluated" in caveat for caveat in report.caveats)


def test_check_load_latency_severity_scales_with_lookup_count() -> None:
    report = _analyzers().check_load_latency()
    assert not isinstance(report, UnsupportedResult)
    by_tran = {f.metrics["tran_id"]: f for f in report.findings}
    assert by_tran["TR_FULL"].severity == "medium"  # one looked-up object
    assert by_tran["TR_MANY"].severity == "high"  # three looked-up objects
    assert len(by_tran["TR_MANY"].metrics["looked_up_objects"]) == 3
    # most-severe-first ordering means the riskiest finding leads the report
    assert report.findings[0].metrics["tran_id"] == "TR_MANY"


def test_check_load_latency_reports_page_truncation() -> None:
    report = _analyzers().check_load_latency(limit=1)
    assert not isinstance(report, UnsupportedResult)
    assert report.finding_count == 1
    assert report.truncated is True
    assert any("of 3 full-update candidates were parsed" in c for c in report.caveats)
    # The object->chain frequency gap must be documented, not guessed.
    assert any("loading-chain" in c or "cadence" in c for c in report.caveats)


def test_deep_layer_stacks() -> None:
    report = _analyzers().deep_layer_stacks()
    assert not isinstance(report, UnsupportedResult)
    assert report.finding_count == 1  # only the L1..L4 stack is deep
    finding = report.findings[0]
    assert finding.metrics["path"] == ["L1_DSO", "L2_DSO", "L3_DSO", "L4_DSO"]
    assert finding.metrics["depth"] == 3


def test_extractor_enhancements_are_evidence_backed_and_connector_gated() -> None:
    """Findings carry the appended fields plus delta/extractor detail, not just a count."""
    report = _analyzers().extractor_enhancements()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.6"
    assert report.connector_required == "ECC"
    finding = report.findings[0]
    assert finding.affected_objects == ["DS_ENH"]
    assert finding.metrics["customer_field_count"] == 5
    assert finding.metrics["customer_fields"]  # the actual appended field names
    # The exit logic still needs a source-system connector, and the finding says so.
    assert finding.unpopulated_reason is not None
    assert any("do not reveal what the exit code does" in c for c in report.caveats)


def test_schedule_risk_connector_gated() -> None:
    report = _analyzers().schedule_risk()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.7"
    assert report.connector_required == "Tableau/BOBJ"
    assert report.findings[0].unpopulated_reason is not None


def test_dashboards_on_calc_views_connector_gated() -> None:
    report = _analyzers().dashboards_on_calc_views()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.8"
    assert report.connector_required == "Tableau"
    # object_dependencies absent in this capability -> no BW-consuming count.
    assert report.findings[0].metrics["bw_consuming_calc_views"] == 0


def test_find_layer_violations_covers_all_three() -> None:
    report = _analyzers().find_layer_violations()
    assert not isinstance(report, UnsupportedResult)
    titles = " ".join(f.title for f in report.findings)
    assert "CompositeProvider -> DSO" in titles
    assert "CompositeProvider -> InfoObject" in titles
    assert "deep DSO stack" in titles
    deep = next(f for f in report.findings if "deep DSO stack" in f.title)
    assert deep.severity == "high"
    assert deep.metrics["depth"] == 3


def test_run_scenario_dispatch_and_unknown() -> None:
    analyzers = _analyzers()
    report = analyzers.run_scenario("9.3")
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.3"
    unknown = analyzers.run_scenario("9.99")
    assert isinstance(unknown, UnsupportedResult)


def test_unsupported_without_transformation() -> None:
    result = _analyzers(present={"dtp"}).composite_provider_to_dso()
    assert isinstance(result, UnsupportedResult)


# --- 9.6 with a configured source-system connector --------------------------------------------
# The BW landscape above says DS_ENH carries 5 appended fields. These tests add exit ABAP so the
# scenario can answer what BW cannot: which tables that enhancement reads, and at what cost.

_EXIT_WITH_LOOP = """\
FUNCTION EXIT_SAPLRSAP_001.
  CASE i_datasource.
    WHEN 'DS_ENH'.
      LOOP AT c_t_data INTO ls_data.
        SELECT single f FROM tbl_foreign INTO lv WHERE k = ls_data-k.
      ENDLOOP.
    WHEN 'DS_HIDDEN'.
      SELECT f FROM tbl_local INTO TABLE lt.
  ENDCASE.
ENDFUNCTION.
"""


def _ecc_profile() -> EccProfile:
    return EccProfile(
        name="src",
        host="src.example.invalid",
        port=44300,
        client="300",
        user="reader",
        password=SecretStr("pw"),  # pragma: allowlist secret
    )


def _ecc_analyzers(body: str = _EXIT_WITH_LOOP) -> Analyzers:
    """Analyzers with a scripted ADT connector registered for the 'ecc' kind."""

    class Fetcher:
        def get_text(self, path: str, params: Any) -> AdtResponse:
            # Only the transaction-data slot exists; the other three are absent.
            return AdtResponse(200, body) if "zxrsau01" in path else AdtResponse(404, "")

    registry = ConnectorRegistry([EccConnector(_ecc_profile(), Fetcher())])
    return Analyzers(ScriptedConnection(), _capability(), registry=registry)


def _finding_for(report: Any, datasource: str) -> Any:
    return next(f for f in report.findings if f.affected_objects == [datasource])


def test_configured_connector_drops_the_unpopulated_reason() -> None:
    report = _ecc_analyzers().extractor_enhancements()
    assert not isinstance(report, UnsupportedResult)
    assert report.connector_required is None
    assert _finding_for(report, "DS_ENH").unpopulated_reason is None


def test_exit_evidence_names_the_table_the_enhancement_reads() -> None:
    finding = _finding_for(_ecc_analyzers().extractor_enhancements(), "DS_ENH")
    assert finding.metrics["exit_confirmed"] is True
    assert finding.metrics["exit_includes"] == ["ZXRSAU01"]
    assert finding.metrics["exit_table_reads"] == ["tbl_foreign"]


def test_per_record_select_in_the_exit_escalates_severity() -> None:
    finding = _finding_for(_ecc_analyzers().extractor_enhancements(), "DS_ENH")
    assert finding.metrics["exit_per_record_selects"] == 1
    assert finding.severity == "high"
    assert "scales with extract volume" in finding.detail
    assert "set-based read" in finding.recommendation


def test_risk_is_not_borrowed_from_another_branch() -> None:
    """DS_ENH's LOOP must not escalate the DataSource handled by the *next* branch of that CASE."""
    report = _ecc_analyzers().extractor_enhancements()
    hidden = _finding_for(report, "DS_HIDDEN")
    assert hidden.severity == "medium"
    assert "scales with extract volume" not in hidden.detail
    assert "tbl_foreign" not in str(hidden.metrics)


def test_datasource_enhanced_only_in_the_exit_is_reported() -> None:
    """An enhancement that overwrites a standard field appends no field, so BW shows nothing."""
    report = _ecc_analyzers().extractor_enhancements()
    hidden = _finding_for(report, "DS_HIDDEN")
    assert hidden.title == "DataSource enhanced in the exit with no appended fields"
    assert hidden.metrics["detected_from"] == "exit_source"
    assert hidden.evidence[0].source_table == "ADT"
    assert hidden.evidence[0].source_key["INCLUDE"] == "ZXRSAU01"


def test_exit_summary_is_added_to_the_caveats() -> None:
    report = _ecc_analyzers().extractor_enhancements()
    assert not isinstance(report, UnsupportedResult)
    joined = " ".join(report.caveats)
    assert "1 of 4 slot(s) implemented" in joined
    assert "src.example.invalid" not in joined  # never the host


def test_exit_that_does_not_mention_the_datasource_says_so() -> None:
    other = "FUNCTION f.\n  CASE i_datasource.\n    WHEN 'DS_OTHER'.\n  ENDCASE.\nENDFUNCTION."
    finding = _finding_for(_ecc_analyzers(other).extractor_enhancements(), "DS_ENH")
    assert finding.metrics["exit_confirmed"] is False
    assert "implemented elsewhere" in finding.detail


def test_source_system_outage_degrades_to_bw_only_evidence() -> None:
    class Exploding:
        def get_text(self, path: str, params: Any) -> AdtResponse:
            raise RuntimeError("transport blew up")

    analyzers = Analyzers(
        ScriptedConnection(),
        _capability(),
        registry=ConnectorRegistry([EccConnector(_ecc_profile(), Exploding())]),
    )
    report = analyzers.extractor_enhancements()
    assert not isinstance(report, UnsupportedResult)
    # The BW-side finding still stands; it simply carries no exit evidence.
    assert _finding_for(report, "DS_ENH").metrics["customer_field_count"] == 5


# --- write-back loops in the layer-violation analyzer -----------------------------------------


def _violations() -> Any:
    report = _analyzers().find_layer_violations()
    assert not isinstance(report, UnsupportedResult)
    return report


def _by_kind(report: Any, kind: str) -> list[Any]:
    return [f for f in report.findings if f.metrics.get("kind") == kind]


def test_self_loop_is_reported_as_high_severity() -> None:
    """A transformation reading and writing the same object makes its own load non-repeatable."""
    findings = _by_kind(_violations(), "self_loop")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.affected_objects == ["SELF_DSO"]
    assert finding.severity == "high"
    assert "re-running" in finding.detail
    assert finding.evidence[0].source_key["TRANID"] == "TR_SELF"


def test_two_cycle_is_reported_once_with_both_transformations() -> None:
    findings = _by_kind(_violations(), "two_cycle")
    assert len(findings) == 1  # the pair, not one finding per direction
    finding = findings[0]
    assert finding.affected_objects == ["A_DSO", "B_DSO"]  # deterministic order
    assert finding.severity == "high"
    assert finding.metrics["tran_ids"] == ["TR_AB", "TR_BA"]
    assert len(finding.evidence) == 2


def test_one_way_edges_are_not_cycles() -> None:
    objects = {obj for f in _by_kind(_violations(), "two_cycle") for obj in f.affected_objects}
    assert "MERGE_DSO" not in objects
    assert "L2_DSO" not in objects


def test_cycle_scope_is_declared() -> None:
    assert any("Longer cycles" in c for c in _violations().caveats)


def test_existing_layer_violations_still_reported() -> None:
    """Adding write-back detection must not displace the CP->DSO / deep-stack findings."""
    report = _violations()
    titles = {f.title for f in report.findings}
    assert any("CompositeProvider -> DSO" in t for t in titles)
    assert any("DSO stack" in t or "stack" in t.lower() for t in titles)
