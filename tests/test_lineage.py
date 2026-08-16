"""Tests for the lineage service (B6), offline against a scripted landscape.

Synthetic names only. The one /BIC/ table a routine reads is built by concatenation so this test
file stays clean for the customer-metadata scan.

Landscape:
    DS_SALES --TR1--> SALES_DSO --TR2 (start routine reads LOOKUP_DSO)--> SALES_CUBE
DTPs load each hop (TR1 full, TR2 delta). LOOKUP_DSO has NO declared edge to SALES_CUBE -- it is
only reachable via TR2's routine, so impact_analysis(LOOKUP_DSO) must surface SALES_CUBE.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.lineage import LineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    # The provider catalogue, so an object no transformation touches can still be typed.
    "cube_header": "RSDCUBE",
    "cube_field": "RSDCUBEIOBJ",
}

# An InfoCube that is the endpoint of nothing: no transformation, no DTP. RSDCUBE knows exactly what
# it is, and before the provider-catalogue fallback the lineage graph typed it 'unknown'.
_ISOLATED_CUBE = "RETIRED_CUBE"

_BIC_LOOKUP = "/BIC/" + "ALOOKUP_DSO00"  # concatenated so the scan does not match this .py file

# transformation edges: source -> (target, targettype, tranid, sourcetype)
_TRANS_BY_SOURCE = {
    "DS_SALES": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("SALES_CUBE", "CUBE", "TR2")],
}
_TRANS_BY_TARGET = {
    "SALES_DSO": [("DS_SALES", "RSDS", "TR1")],
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR2")],
}
# TRANID -> header row (12 cols): OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME,
# START, END, EXPERT, GLBCODE, GLBCODE2
_HEADER = {
    "TR1": ("ACT", "RSDS", "", "DS_SALES", "ODSO", "", "SALES_DSO", "", "", "", "", ""),
    "TR2": ("ACT", "ODSO", "", "SALES_DSO", "CUBE", "", "SALES_CUBE", "CODE_TR2", "", "", "", ""),
}
_DTP_BY_SRC = {
    "DS_SALES": [("SALES_DSO", "ODSO", "F")],
    "SALES_DSO": [("SALES_CUBE", "CUBE", "D")],
}
_DTP_BY_TGT = {
    "SALES_DSO": [("DS_SALES", "RSDS", "F")],
    "SALES_CUBE": [("SALES_DSO", "ODSO", "D")],
}
_RSAABAP = {
    "CODE_TR2": [
        "METHOD start.",
        "  SELECT * FROM " + _BIC_LOOKUP + " INTO TABLE lt.",
        "ENDMETHOD.",
    ]
}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        if "RSTRANSTEPROUT" in sql:
            return []  # no field routines in this landscape
        if "RSAABAP" in sql:
            return self._rsaabap(sql, params)
        if "RSDCUBEIOBJ" in sql:
            return []
        if "RSDCUBE" in sql:
            if "= ?" not in sql:  # catalogue listing
                return [(_ISOLATED_CUBE,)]
            # CUBETYPE, OBJSTAT, INFOAREA, OWNER, BWAPPL
            return (
                [("B", "ACT", "SALES", "DEVUSER", "SD")] if str(params[0]) == _ISOLATED_CUBE else []
            )
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    @staticmethod
    def _dtp(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        name = str(params[0])
        if "SRC = ?" in sql:
            return [(t, ty, um) for t, ty, um in _DTP_BY_SRC.get(name, [])]
        return [(s, ty, um) for s, ty, um in _DTP_BY_TGT.get(name, [])]

    @staticmethod
    def _rsaabap(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "LIKE" in sql:  # reverse search: CODEIDs whose source contains the term
            term = str(params[0]).strip("%").upper()
            return [
                (code_id,) for code_id, lines in _RSAABAP.items() if term in " ".join(lines).upper()
            ]
        code_id = str(params[0])  # by CODEID = ?
        return [(line,) for line in _RSAABAP.get(code_id, [])]

    def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "OBJSTAT" in sql:  # get_routine_code header (by TRANID)
            row = _HEADER.get(str(params[0]))
            return [row] if row else []
        if "STARTROUTINE IN" in sql:  # reverse: header-routine code-ids -> tranids
            wanted = {str(p) for p in params}
            return [(t,) for t, h in _HEADER.items() if h[7] in wanted]
        if "TRANID IN" in sql:  # reverse: targets for tranids
            wanted = {str(p) for p in params}
            return [(t, _HEADER[t][6], _HEADER[t][4]) for t in _HEADER if t in wanted]
        name = str(params[0])
        if "SOURCENAME = ?" in sql:  # downstream declared
            return [(t, ty, tr) for t, ty, tr in _TRANS_BY_SOURCE.get(name, [])]
        if "TARGETNAME = ?" in sql:
            if "SOURCENAME" in sql:  # upstream declared (selects SOURCENAME)
                return [(s, ty, tr) for s, ty, tr in _TRANS_BY_TARGET.get(name, [])]
            return [
                (tr,) for _, _, tr in _TRANS_BY_TARGET.get(name, [])
            ]  # transformations_targeting
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


def _service(present: set[str] | None = None) -> LineageService:
    return LineageService(ScriptedConnection(), _capability(present))


def test_downstream_lineage_declared_edges() -> None:
    graph = _service().get_lineage("DS_SALES", direction="downstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert {"DS_SALES", "SALES_DSO", "SALES_CUBE"} <= names
    by_pair = {(e.src, e.dst): e for e in graph.edges}
    assert by_pair[("DS_SALES", "SALES_DSO")].update_mode == "full"
    assert by_pair[("SALES_DSO", "SALES_CUBE")].update_mode == "delta"
    assert by_pair[("DS_SALES", "SALES_DSO")].transformation_id == "TR1"


def test_upstream_lineage_includes_routine_lookup() -> None:
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "LOOKUP_DSO" in names  # discovered only via TR2's routine
    routine_edges = [e for e in graph.edges if e.kind == "routine_lookup"]
    assert any(e.src == "LOOKUP_DSO" and e.dst == "SALES_CUBE" for e in routine_edges)
    assert all(e.confidence == "advisory" for e in routine_edges)


def test_trace_to_source_reaches_datasource() -> None:
    trace = _service().trace_to_source("SALES_CUBE", depth=8)
    assert not isinstance(trace, UnsupportedResult)
    assert "DS_SALES" in trace.datasources_reached
    ds_node = next(n for n in trace.graph.nodes if n.name == "DS_SALES")
    assert ds_node.object_type == "datasource"
    assert ds_node.upstream_resolved is False  # BW-only boundary


def test_impact_analysis_finds_routine_embedded_consumer() -> None:
    # LOOKUP_DSO has NO declared downstream edge; only TR2's routine reads it.
    impact = _service().impact_analysis("LOOKUP_DSO", depth=3)
    assert not isinstance(impact, UnsupportedResult)
    assert "SALES_CUBE" in impact.routine_lookup_consumers
    consumer_edges = [
        e for e in impact.graph.edges if e.kind == "routine_lookup" and e.dst == "SALES_CUBE"
    ]
    assert consumer_edges and consumer_edges[0].confidence == "advisory"
    assert impact.affected_object_count >= 1


def test_unsupported_without_transformation() -> None:
    result = _service(present={"dtp"}).get_lineage("X", direction="both", depth=2)
    assert isinstance(result, UnsupportedResult)


# --- canonical object typing ------------------------------------------------------------------


def test_isolated_provider_is_typed_from_the_catalogue_not_left_unknown() -> None:
    """An object no transformation touches was typed 'unknown' while RSDCUBE knew what it was.

    Found by comparing the canonical id from bw_describe_object against the one from
    bw_get_lineage for the same InfoCube: they disagreed, which is exactly what the canonical
    object model exists to make visible.
    """
    graph = _service().get_lineage(_ISOLATED_CUBE, direction="both", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    node = next(n for n in graph.nodes if n.name == _ISOLATED_CUBE)
    assert node.object_type == "infocube"
    assert node.ref is not None
    assert node.ref.id == f"infocube:{_ISOLATED_CUBE}"


def test_lineage_nodes_carry_a_canonical_ref() -> None:
    graph = _service().get_lineage("SALES_CUBE", direction="both", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    for node in graph.nodes:
        assert node.ref is not None, f"{node.name} has no canonical ref"
        assert node.ref.id.startswith(f"{node.object_type}:")
    # The basic InfoCube is 'infocube' here, matching every other surface (it was 'cube').
    cube = next(n for n in graph.nodes if n.name == "SALES_CUBE")
    assert cube.object_type == "infocube"
