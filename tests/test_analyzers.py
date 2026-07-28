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


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "RSDSSEGFD" in sql:  # 9.6 group by DataSource
            return [("DS_ENH", 5)]
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


def test_extractor_enhancements_heuristic_and_connector_gated() -> None:
    report = _analyzers().extractor_enhancements()
    assert not isinstance(report, UnsupportedResult)
    assert report.scenario == "9.6"
    assert report.connector_required == "ECC"
    finding = report.findings[0]
    assert finding.affected_objects == ["DS_ENH"]
    assert finding.metrics["zy_field_count"] == 5
    assert finding.unpopulated_reason is not None
    assert any("HEURISTIC" in c for c in report.caveats)


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
