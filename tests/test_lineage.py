"""Tests for the lineage service (B6), offline against a scripted landscape.

Synthetic names only. The one /BIC/ table a routine reads is built by concatenation so this test
file stays clean for the customer-metadata scan.

Landscape:
    DS_SALES --TR1--> SALES_DSO --TR2 (start routine reads LOOKUP_DSO)--> SALES_CUBE
DTPs load each hop (TR1 full, TR2 delta). LOOKUP_DSO has NO declared edge to SALES_CUBE -- it is
only reachable via TR2's routine, so impact_analysis(LOOKUP_DSO) must surface SALES_CUBE.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, get_args

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.lineage import LineageNodeType
from mcp_server_sapbw.models.objects import TLOGO_TO_TYPE
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services import lineage as lineage_module
from mcp_server_sapbw.services.lineage import LineageService, _node_type

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_step_rout": "RSTRANSTEPROUT",
    # The typed rule-step tables. BW records a declared lookup here and nowhere else, and a
    # declared lookup has no SELECT to parse, so an ABAP-only reader cannot see it (D15).
    "transformation_step_master": "RSTRANSTEPMASTER",
    "transformation_step_dso": "RSTRANSTEPODSO",
    "transformation_step_adso": "RSTRANSTEPADSO",
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
# An *Advanced* DSO read by the same routine. '...2' is the ADSO active table, where the '...00'
# above is a classic DSO's. Both are routine-resolved, and the node type each produces is the point:
# the routine path used to emit "dso" for the first and "infoobject" for everything else, so an ADSO
# found inside ABAP was typed - and coloured in every rendered diagram - as master data.
_BIC_LOOKUP_ADSO = "/BIC/" + "ALOOKUP_ADSO2"
_ROUTINE_ADSO = "LOOKUP_ADSO"

# A query element sitting on top of the cube, reached by a transformation whose target TLOGO is
# 'ELEM'. Present because a real BW 7.50 system has these and the walk reaching one used to discard
# the entire graph (see test_a_query_element_endpoint_does_not_fail_the_whole_graph).
_QUERY_ELEMENT = "SALES_QRY_ELEM"

# transformation edges: source -> (target, targettype, tranid, sourcetype)
_TRANS_BY_SOURCE = {
    "DS_SALES": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("SALES_CUBE", "CUBE", "TR2")],
    "SALES_CUBE": [(_QUERY_ELEMENT, "ELEM", "TR3")],
    # BW stores TR4's source as the DTP id, so this is how the row reads from the source side too.
    "DTP_CUBE_SELF": [("SALES_CUBE", "CUBE", "TR4")],
}
_TRANS_BY_TARGET = {
    "SALES_DSO": [("DS_SALES", "RSDS", "TR1")],
    # TR4 is the self-transformation whose stored source is a DTP id, not an object (see _SELF_DTP).
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR2"), ("DTP_CUBE_SELF", "DTPA", "TR4")],
    _QUERY_ELEMENT: [("SALES_CUBE", "CUBE", "TR3")],
}
# TRANID -> header row (12 cols): OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME,
# START, END, EXPERT, GLBCODE, GLBCODE2
_HEADER = {
    "TR1": ("ACT", "RSDS", "", "DS_SALES", "ODSO", "", "SALES_DSO", "", "", "", "", ""),
    "TR2": ("ACT", "ODSO", "", "SALES_DSO", "CUBE", "", "SALES_CUBE", "CODE_TR2", "", "", "", ""),
    "TR3": ("ACT", "CUBE", "", "SALES_CUBE", "ELEM", "", _QUERY_ELEMENT, "", "", "", "", ""),
    "TR4": ("ACT", "DTPA", "", "DTP_CUBE_SELF", "CUBE", "", "SALES_CUBE", "", "", "", "", ""),
}
# --- DTPs (D18) ---------------------------------------------------------------------------------
#
# Tuples are (DTP id, other object, other TLOGO, UPDMODE). The id is carried because BW routinely
# runs several active DTPs between one pair - a repair/init full beside the regular delta - and
# collapsing them to one edge threw the ids away, so a load path could not be named. On the
# production ADSO that found D18, eight inbound DTPs were reported as one.
#
# SALES_DSO -> SALES_CUBE has two: the regular delta and a repair full.
_DTP_BY_SRC = {
    "DS_SALES": [("DTP_DS_TO_DSO", "SALES_DSO", "ODSO", "F")],
    "SALES_DSO": [
        ("DTP_DSO_TO_CUBE", "SALES_CUBE", "CUBE", "D"),
        ("DTP_DSO_TO_CUBE_REPAIR", "SALES_CUBE", "CUBE", "F"),
    ],
    "SALES_CUBE": [("DTP_CUBE_SELF", "SALES_CUBE", "CUBE", "F")],
}
_DTP_BY_TGT = {
    "SALES_DSO": [("DTP_DS_TO_DSO", "DS_SALES", "RSDS", "F")],
    "SALES_CUBE": [
        ("DTP_DSO_TO_CUBE", "SALES_DSO", "ODSO", "D"),
        ("DTP_DSO_TO_CUBE_REPAIR", "SALES_DSO", "ODSO", "F"),
        ("DTP_CUBE_SELF", "SALES_CUBE", "CUBE", "F"),
    ],
    # ERR_TARGET is loaded by a normal DTP and by an error DTP whose SRC is that DTP's name.
    "ERR_TARGET": [
        ("DTP_ERR_PARENT", "ERR_SOURCE", "ODSO", "D"),
        ("DTP_ERR_CHILD", "DTP_ERR_PARENT", "DTPA", "F"),
    ],
}

# A self-transformation, stored the way BW stores one: SOURCETYPE is 'DTPA' and SOURCENAME is the
# *DTP's* technical name, not an object. Resolving it needs RSBKDTP. Left unresolved the graph gains
# a node named after a DTP where the real source object belongs - which is what D18 reported on
# production, a DTP id sitting in the dependency list where the ADSO's own name should have been.
_SELF_DTP = "DTP_CUBE_SELF"

# An error DTP and the parent whose error stack it reads. BW stores the error DTP's SRC as the
# parent DTP's *name*, so this is the second place a DTP id lands where an object belongs - reached
# through the DTP reader, not the transformation reader, which is why fixing one did not fix both.
_PARENT_DTP = "DTP_ERR_PARENT"
_ERROR_DTP = "DTP_ERR_CHILD"

_DTP_HEADER = {  # DTP id -> (SRC, SRCTLOGO, TGT, TGTTLOGO, UPDMODE)
    "DTP_DS_TO_DSO": ("DS_SALES", "RSDS", "SALES_DSO", "ODSO", "F"),
    "DTP_DSO_TO_CUBE": ("SALES_DSO", "ODSO", "SALES_CUBE", "CUBE", "D"),
    "DTP_DSO_TO_CUBE_REPAIR": ("SALES_DSO", "ODSO", "SALES_CUBE", "CUBE", "F"),
    _SELF_DTP: ("SALES_CUBE", "CUBE", "SALES_CUBE", "CUBE", "F"),
    _PARENT_DTP: ("ERR_SOURCE", "ODSO", "ERR_TARGET", "ADSO", "D"),
    _ERROR_DTP: (_PARENT_DTP, "DTPA", "ERR_TARGET", "ADSO", "F"),
}
_RSAABAP = {
    "CODE_TR2": [
        "METHOD start.",
        "  SELECT * FROM " + _BIC_LOOKUP + " INTO TABLE lt.",
        "  SELECT * FROM " + _BIC_LOOKUP_ADSO + " INTO TABLE lt_adso.",
        "ENDMETHOD.",
    ]
}

# --- declared lookups (D15) -------------------------------------------------------------------
#
# TR2 (SALES_DSO -> SALES_CUBE) declares two lookups in BW's typed rule-step tables. Neither object
# is mentioned anywhere in _RSAABAP, and that is the whole point: BW states these reads outright, so
# a reader that only parses ABAP SELECTs is blind to them however good its parser is.
#
# RATE_ADSO is the analogue of the production case behind D15 - BW's own 'Used by:' list names the
# transformation, our consumer list did not.
_LOOKUP_ADSO = "RATE_ADSO"
_LOOKUP_IOBJ = "COST_CENTRE"

# RULEID, STEPID, ADSONM, BEHAVIOR, CONSTANT   ('C' = substitute a constant on a miss)
_ADSO_STEP_LOOKUPS: dict[str, list[tuple[Any, ...]]] = {"TR2": [(2, 1, _LOOKUP_ADSO, "C", "0.00")]}
# RULEID, STEPID, IOBJNM, MPER, DATEIOBJNM, CONSTANT
_MASTER_STEP_LOOKUPS: dict[str, list[tuple[Any, ...]]] = {
    "TR2": [(3, 1, _LOOKUP_IOBJ, "2", "POSTING_DATE", "")]
}
_DSO_STEP_LOOKUPS: dict[str, list[tuple[Any, ...]]] = {}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        if "RSTRANSTEPROUT" in sql:
            return []  # no field routines in this landscape
        # Checked before RSTRAN: every one of these names contains it as a substring.
        if "RSTRANSTEPADSO" in sql:
            return self._step_lookup(sql, params, _ADSO_STEP_LOOKUPS, "ADSONM")
        if "RSTRANSTEPODSO" in sql:
            return self._step_lookup(sql, params, _DSO_STEP_LOOKUPS, "ODSOBJECT")
        if "RSTRANSTEPMASTER" in sql:
            return self._step_lookup(sql, params, _MASTER_STEP_LOOKUPS, "IOBJNM")
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
        # Resolving a DTPA-typed transformation endpoint: DTP id -> its own source.
        if "DTP IN (" in sql:
            requested = {str(p) for p in params}
            return [
                (dtp, row[0], row[1], row[2], row[3])  # DTP, SRC, SRCTLOGO, TGT, TGTTLOGO
                for dtp, row in sorted(_DTP_HEADER.items())
                if dtp in requested
            ]
        # Batched frontier prefetch: keyed by the whole BFS level, so the key column is selected
        # too and every row says which requested name it belongs to.
        if "SRC IN (" in sql:
            return [
                (name, t, ty, um, d)
                for name in map(str, params)
                for d, t, ty, um in _DTP_BY_SRC.get(name, [])
            ]
        if "TGT IN (" in sql:
            return [
                (name, s, ty, um, d)
                for name in map(str, params)
                for d, s, ty, um in _DTP_BY_TGT.get(name, [])
            ]
        name = str(params[0])
        if "SRC = ?" in sql:
            return [(t, ty, um, d) for d, t, ty, um in _DTP_BY_SRC.get(name, [])]
        return [(s, ty, um, d) for d, s, ty, um in _DTP_BY_TGT.get(name, [])]

    @staticmethod
    def _step_lookup(
        sql: str,
        params: list[Any],
        rows_by_tran: dict[str, list[tuple[Any, ...]]],
        name_column: str,
    ) -> list[tuple[Any, ...]]:
        """Serve a typed rule-step table in its three query shapes.

        The stored tuple is ``(RULEID, STEPID, <object>, ...)`` - the order the per-transformation
        reader selects. The reverse and batched shapes select different columns, so each is
        projected explicitly rather than returned wholesale; a fixture that ignored the column list
        would let a wrong ``build_select`` pass.
        """
        if f"{name_column} = ?" in sql:  # reverse: who looks this object up?
            wanted = str(params[0])
            return [
                (tran_id, row[0], row[1])  # TRANID, RULEID, STEPID
                for tran_id, rows in sorted(rows_by_tran.items())
                for row in rows
                if str(row[2]) == wanted
            ]
        if "TRANID IN (" in sql:  # batched forward: what do these transformations look up?
            requested = {str(p) for p in params}
            return [
                (tran_id, row[2], row[0], row[1])  # TRANID, <object>, RULEID, STEPID
                for tran_id, rows in sorted(rows_by_tran.items())
                if tran_id in requested
                for row in rows
            ]
        return list(rows_by_tran.get(str(params[0]), []))  # per-transformation forward read

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
        # Batched frontier prefetch, one statement per BFS level per direction.
        if "SOURCENAME IN (" in sql:
            return [
                (name, t, ty, tr)
                for name in map(str, params)
                for t, ty, tr in _TRANS_BY_SOURCE.get(name, [])
            ]
        if "TARGETNAME IN (" in sql:
            return [
                (name, s, ty, tr)
                for name in map(str, params)
                for s, ty, tr in _TRANS_BY_TARGET.get(name, [])
            ]
        name = str(params[0])
        if "SOURCENAME = ?" in sql:  # downstream declared
            return [(t, ty, tr) for t, ty, tr in _TRANS_BY_SOURCE.get(name, [])]
        if "TARGETNAME = ?" in sql:  # upstream declared; also serves transformations_targeting
            return [(s, ty, tr) for s, ty, tr in _TRANS_BY_TARGET.get(name, [])]
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
    assert by_pair[("DS_SALES", "SALES_DSO")].update_modes == ["full"]
    assert by_pair[("DS_SALES", "SALES_DSO")].transformation_id == "TR1"
    # SALES_DSO -> SALES_CUBE carries a delta and a repair full (see the DTP fixture), so the scalar
    # is 'full': it is the more operationally significant of the two, which is the D13 rule. This
    # asserted 'delta' while the landscape had one DTP on the pair; the second one was added for
    # D18, and reading 'delta' now would mean the repair load had been dropped.
    both = by_pair[("SALES_DSO", "SALES_CUBE")]
    assert set(both.update_modes) == {"full", "delta"}
    assert both.update_mode == "full"


def test_upstream_lineage_includes_routine_lookup() -> None:
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "LOOKUP_DSO" in names  # discovered only via TR2's routine
    routine_edges = [e for e in graph.edges if e.kind == "routine_lookup"]
    assert any(e.src == "LOOKUP_DSO" and e.dst == "SALES_CUBE" for e in routine_edges)
    assert all(e.confidence == "advisory" for e in routine_edges)


def test_a_routine_resolved_adso_is_typed_adso_not_master_data() -> None:
    """The routine path must decode the kind it resolved, not collapse it to two possibilities.

    It used to read ``"dso" if kind == "dso" else "infoobject"``, so an ADSO or an InfoCube reached
    through routine ABAP arrived typed as an InfoObject. Nothing downstream re-derived the type, so
    the mislabel reached impact analysis, the generated documentation and every diagram - where it
    also picked up the master-data colour, making a transactional store look like master data.
    """
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    by_name = {n.name: n for n in graph.nodes}

    assert _ROUTINE_ADSO in by_name, "the ADSO the routine reads must reach the graph"
    assert by_name[_ROUTINE_ADSO].object_type == "adso"
    # The classic DSO beside it still decodes as before: this is a widening, not a re-mapping.
    assert by_name["LOOKUP_DSO"].object_type == "dso"

    # Both arrive as advisory routine edges - correcting the type must not promote the evidence.
    kinds = {e.src: (e.kind, e.confidence) for e in graph.edges if e.dst == "SALES_CUBE"}
    assert kinds[_ROUTINE_ADSO] == ("routine_lookup", "advisory")


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


# --- declared lookups (D15) -------------------------------------------------------------------
#
# The routine tests above cover the heuristic half of lookup discovery. These cover the exact half:
# a lookup BW *declares* in RSTRANSTEPADSO/ODSO/MASTER, with no SELECT anywhere for a parser to
# find. Round 2 of the S01 human verification found BW's own 'Used by:' list naming a transformation
# that our consumer list omitted entirely, because lineage only ever asked the ABAP parser.


def test_impact_analysis_finds_declared_lookup_consumer() -> None:
    """D15: a transformation declaring a lookup against the root is a consumer of it.

    RATE_ADSO has no declared data-flow edge and appears in no ABAP, so the routine parser cannot
    reach it. Only RSTRANSTEPADSO records that TR2 reads it, and TR2's target is SALES_CUBE.
    """
    impact = _service().impact_analysis(_LOOKUP_ADSO, depth=3)
    assert not isinstance(impact, UnsupportedResult)

    names = {n.name for n in impact.graph.nodes}
    assert "SALES_CUBE" in names, "the transformation declaring the lookup was not reported"

    edges = [
        e
        for e in impact.graph.edges
        if e.kind == "declared_lookup" and e.src == _LOOKUP_ADSO and e.dst == "SALES_CUBE"
    ]
    assert edges, "expected a declared_lookup edge to the looking-up transformation's target"
    edge = edges[0]
    # The distinction that matters: BW states this, so it is exact, not a lower bound.
    assert edge.derivation == "declared"
    assert edge.confidence == "exact"
    assert edge.transformation_id == "TR2"
    assert edge.evidence is not None
    assert edge.evidence.basis == "observed"
    assert edge.evidence.method == "declared_lookup_rule"
    assert edge.evidence.completeness == "complete"
    # It must not be filed under the advisory routine bucket, which is what a caller filters on to
    # decide how far to trust an edge.
    assert _LOOKUP_ADSO not in impact.routine_lookup_consumers
    assert "SALES_CUBE" not in impact.routine_lookup_consumers


def test_declared_lookup_consumers_cover_master_data_reads() -> None:
    """A master-data read is the same relation against an InfoObject, and must behave the same."""
    impact = _service().impact_analysis(_LOOKUP_IOBJ, depth=3)
    assert not isinstance(impact, UnsupportedResult)
    edges = [e for e in impact.graph.edges if e.kind == "declared_lookup"]
    assert any(e.src == _LOOKUP_IOBJ and e.dst == "SALES_CUBE" for e in edges)
    assert all(e.confidence == "exact" for e in edges)


def test_upstream_lineage_reports_declared_lookups_as_exact() -> None:
    """The same relation upstream: what SALES_CUBE's inbound transformation reads.

    Both halves must appear and stay distinguishable - the ABAP-derived LOOKUP_DSO as advisory, the
    BW-declared RATE_ADSO as exact. Reporting a declared read as advisory understates what is known.
    """
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)

    names = {n.name for n in graph.nodes}
    assert _LOOKUP_ADSO in names
    assert "LOOKUP_DSO" in names  # the routine-parsed one still works

    by_kind = {(e.kind, e.src): e for e in graph.edges if e.dst == "SALES_CUBE"}
    declared = by_kind.get(("declared_lookup", _LOOKUP_ADSO))
    assert declared is not None
    assert declared.confidence == "exact"
    assert declared.derivation == "declared"

    routine = by_kind.get(("routine_lookup", "LOOKUP_DSO"))
    assert routine is not None
    assert routine.confidence == "advisory"


def test_declared_lookups_are_absent_when_the_release_lacks_the_step_tables() -> None:
    """An empty result must mean 'none declared', never 'this release could not be asked'."""
    present = set(_TABLES) - {
        "transformation_step_master",
        "transformation_step_dso",
        "transformation_step_adso",
    }
    impact = _service(present).impact_analysis(_LOOKUP_ADSO, depth=3)
    assert not isinstance(impact, UnsupportedResult)
    assert not [e for e in impact.graph.edges if e.kind == "declared_lookup"]
    assert any("declared lookup" in c.lower() for c in impact.caveats), (
        "a release that cannot be asked about declared lookups must say so"
    )


# --- DTP reporting (D18) --------------------------------------------------------------------------


def test_a_dtp_sourced_self_transformation_names_the_object_not_the_dtp() -> None:
    """D18: BW stores a self-transformation's source as a DTP id; the graph reported it verbatim.

    On the production ADSO that found this, the dependency list held a DTP technical name where the
    ADSO's own name belonged - a DTP is not a data source, so the answer named the wrong class of
    thing. RSBKDTP resolves it: the DTP's SRC is the real source object.
    """
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert _SELF_DTP not in names, "a DTP id is standing in for a source object"
    # TR4 is SALES_CUBE -> SALES_CUBE, so resolving it must yield the self-loop.
    self_edges = [e for e in graph.edges if e.transformation_id == "TR4"]
    assert self_edges, "the self-transformation disappeared instead of being resolved"
    assert all(e.src == "SALES_CUBE" for e in self_edges)


def test_the_resolved_endpoint_still_names_the_dtp_it_came_from() -> None:
    """Resolving must not lose the DTP: it is how an operator finds the load to re-run."""
    graph = _service().get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    edge = next(e for e in graph.edges if e.transformation_id == "TR4")
    assert _SELF_DTP in edge.dtp_ids


def test_every_dtp_between_a_pair_is_named_not_just_one() -> None:
    """D18's other half: SALES_DSO -> SALES_CUBE runs a delta and a repair full.

    The pair is deliberately one edge - that is the D13 fix, which accumulates update modes rather
    than letting row order pick one - but the DTP ids were discarded, so no load could be named.
    """
    graph = _service().get_lineage("SALES_DSO", direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    edge = next(e for e in graph.edges if e.src == "SALES_DSO" and e.dst == "SALES_CUBE")
    assert set(edge.dtp_ids) == {"DTP_DSO_TO_CUBE", "DTP_DSO_TO_CUBE_REPAIR"}
    assert edge.dtp_ids == sorted(edge.dtp_ids), "capped/compared reads need a stated order"
    # The D13 behaviour must survive: both modes present, full reported as the significant one.
    assert set(edge.update_modes) == {"full", "delta"}
    assert edge.update_mode == "full"


def test_an_error_dtp_endpoint_is_dereferenced_like_a_transformation_one() -> None:
    """The other path that puts a DTP name where an object belongs.

    An error DTP's own endpoint is the *parent* DTP's error stack, so RSBKDTP hands back a DTP name.
    Fixing only the transformation case left this one reporting a load as a data source - which is
    what the first re-seal after the D18 fix still showed on production.
    """
    graph = _service().get_lineage("ERR_TARGET", direction="upstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert _PARENT_DTP not in names, "a DTP id is standing in for a source object"
    assert "ERR_SOURCE" in names, "the parent DTP's own source was not resolved through"
    edge = next(e for e in graph.edges if e.dst == "ERR_TARGET" and e.src == "ERR_SOURCE")
    # Both the error DTP and the parent it reads through stay nameable.
    assert _ERROR_DTP in edge.dtp_ids
    assert _PARENT_DTP in edge.dtp_ids


def test_dtp_ids_are_empty_rather_than_wrong_when_the_release_has_no_dtp_table() -> None:
    present = set(_TABLES) - {"dtp"}
    graph = _service(present).get_lineage("DS_SALES", direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    assert all(e.dtp_ids == [] for e in graph.edges)


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


# --- D3: a decodable TLOGO code must never fail the whole graph -------------------------------


def test_a_query_element_endpoint_does_not_fail_the_whole_graph() -> None:
    """Regression for D3, found by running bw_analyze_object against a live BW 7.50 system.

    ``RSTRAN`` endpoint code ``ELEM`` - a query element used as a transformation endpoint - decodes
    through the canonical table to ``query_element``, which ``LineageNodeType`` did not contain. The
    consequence was out of all proportion to the cause: constructing the node raised a Pydantic
    validation error, so ``bw_get_lineage`` returned **no graph at all** rather than a graph with
    one oddly-typed node. Depths 1 and 2 happened to stay clear of it on the subject under test;
    depth 3 reached one and the call died.

    So this asserts the shape of the failure, not just the type: a graph comes back, it contains the
    other objects, and the query element is typed rather than dropped or renamed.
    """
    graph = _service().get_lineage("SALES_DSO", direction="downstream", depth=3)

    assert not isinstance(graph, UnsupportedResult), (
        "the walk returned no graph, which is the D3 failure: one unmodelled endpoint type must "
        "not cost the caller the entire answer"
    )
    names = {n.name for n in graph.nodes}
    assert _QUERY_ELEMENT in names, "the query element was dropped from the graph"
    # The rest of the graph must still be intact - a partial answer would be its own defect.
    assert {"SALES_DSO", "SALES_CUBE"} <= names

    node = next(n for n in graph.nodes if n.name == _QUERY_ELEMENT)
    assert node.object_type == "query_element", (
        "the query element must carry its canonical type, not be flattened to 'unknown' - it is a "
        "known legitimate BW object, and hiding it would trade a crash for a quiet inaccuracy"
    )
    assert node.ref is not None
    assert node.ref.id == f"query_element:{_QUERY_ELEMENT}"

    edge = next(e for e in graph.edges if e.dst == _QUERY_ELEMENT)
    assert edge.kind == "transformation"
    assert edge.derivation == "declared"
    assert edge.confidence == "exact"
    assert edge.transformation_id == "TR3"


def test_every_decodable_tlogo_type_is_a_valid_lineage_node_type() -> None:
    """The invariant that makes ``_node_type``'s cast safe, rather than merely quiet.

    ``_node_type`` casts the canonical ``BwObjectType`` returned by ``normalise_object_type`` to
    ``LineageNodeType``. mypy cannot relate two unconnected ``Literal`` types, so the cast silences
    the checker without proving anything - and any TLOGO code decoding to a type outside
    ``LineageNodeType`` then fails at runtime, discarding the whole graph.

    ``query_element`` was the one a live system hit. It was not the only gap: ``ISTS``, ``ISIP``,
    ``UPDR`` and ``RSPC`` were all decodable and all absent, so four more landscapes could have
    produced the identical failure. Checking the whole decode table rather than the one code that
    bit is the difference between fixing an instance and closing the class.
    """
    allowed = set(get_args(LineageNodeType))
    missing = {code: decoded for code, decoded in TLOGO_TO_TYPE.items() if decoded not in allowed}
    assert not missing, (
        "these TLOGO codes decode to types LineageNodeType cannot hold, so a lineage walk reaching "
        f"one returns no graph at all: {missing}"
    )

    # And the function really does return a member for every code, not just in principle.
    for code in TLOGO_TO_TYPE:
        assert _node_type(code) in allowed, f"_node_type({code!r}) escaped LineageNodeType"

    # An unrecognised code must still fail soft, which is what keeps the invariant maintainable:
    # a future TLOGO nobody has mapped becomes 'unknown' instead of a new crash.
    assert _node_type("NOPE") == "unknown"
    assert _node_type("") == "unknown"
    assert _node_type(None) == "unknown"


# --- frontier prefetch: batched declared-edge reads -------------------------------------------
#
# The landscape above is a single chain, so every BFS level holds exactly one node and the prefetch
# never fires on it - which is why the whole suite passed before these tests existed. A fan-out is
# needed to reach the batched path at all.
#
#   U1, U2 --> HUB --> A1 --> B1, B2
#                  --> A2 --> B3
#                  --> A3 --> B4
#
# Levels 1 and 2 of a both-directions walk are 5 and 4 nodes wide respectively.

_FAN_BY_SOURCE = {
    "HUB": [("A1", "ODSO", "TA1"), ("A2", "ODSO", "TA2"), ("A3", "ODSO", "TA3")],
    "A1": [("B1", "CUBE", "TB1"), ("B2", "CUBE", "TB2")],
    "A2": [("B3", "CUBE", "TB3")],
    "A3": [("B4", "CUBE", "TB4")],
    "U1": [("HUB", "ODSO", "TU1")],
    "U2": [("HUB", "ODSO", "TU2")],
}
_FAN_BY_TARGET = {
    "HUB": [("U1", "RSDS", "TU1"), ("U2", "RSDS", "TU2")],
    "A1": [("HUB", "ODSO", "TA1")],
    "A2": [("HUB", "ODSO", "TA2")],
    "A3": [("HUB", "ODSO", "TA3")],
    "B1": [("A1", "ODSO", "TB1")],
    "B2": [("A1", "ODSO", "TB2")],
    "B3": [("A2", "ODSO", "TB3")],
    "B4": [("A3", "ODSO", "TB4")],
}
# HUB -> A1 deliberately carries two active DTPs with different modes, which is the ordinary BW
# shape (a repair/init full DTP beside the regular delta) and was measured on 208 of 1043 active
# pairs on a production system. Full is listed *first* so that a last-row-wins implementation - the
# D13 behaviour - reports 'delta' and fails the assertions below, rather than arriving at 'full' by
# luck of the ordering.
# Tuples are (other, TLOGO, UPDMODE, DTP id) here - the shape the reader returns - because these
# fixtures are consumed positionally by the statement-count tests below.
_FAN_DTP_BY_SRC = {
    "HUB": [
        ("A1", "ODSO", "F", "DTP_HUB_A1_REPAIR"),
        ("A1", "ODSO", "D", "DTP_HUB_A1"),
        ("A2", "ODSO", "D", "DTP_HUB_A2"),
    ],
    "A1": [("B1", "CUBE", "D", "DTP_A1_B1")],
}
_FAN_DTP_BY_TGT = {
    "A1": [("HUB", "ODSO", "F", "DTP_HUB_A1_REPAIR"), ("HUB", "ODSO", "D", "DTP_HUB_A1")],
    "A2": [("HUB", "ODSO", "D", "DTP_HUB_A2")],
    "B1": [("A1", "ODSO", "D", "DTP_A1_B1")],
}


class FanOutConnection:
    """Wide BFS levels, and a count of the statements the walk actually issued."""

    def __init__(self) -> None:
        self.statements = 0

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements += 1
        params = list(parameters or [])
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        if "RSTRANSTEPROUT" in sql or "RSAABAP" in sql:
            return []  # no routines in this landscape
        if "RSDCUBE" in sql:
            return []
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "OBJSTAT" in sql or "STARTROUTINE IN" in sql or "TRANID IN" in sql:
            return []
        if "SOURCENAME IN (" in sql:
            return [
                (name, t, ty, tr)
                for name in map(str, params)
                for t, ty, tr in _FAN_BY_SOURCE.get(name, [])
            ]
        if "TARGETNAME IN (" in sql:
            return [
                (name, s, ty, tr)
                for name in map(str, params)
                for s, ty, tr in _FAN_BY_TARGET.get(name, [])
            ]
        name = str(params[0]) if params else ""
        if "SOURCENAME = ?" in sql:
            return list(_FAN_BY_SOURCE.get(name, []))
        if "TARGETNAME = ?" in sql:
            return list(_FAN_BY_TARGET.get(name, []))
        return []

    @staticmethod
    def _dtp(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "DTP IN (" in sql:
            return []  # no DTP-sourced transformations in this landscape
        if "SRC IN (" in sql:
            return [
                (name, t, ty, um, d)
                for name in map(str, params)
                for t, ty, um, d in _FAN_DTP_BY_SRC.get(name, [])
            ]
        if "TGT IN (" in sql:
            return [
                (name, s, ty, um, d)
                for name in map(str, params)
                for s, ty, um, d in _FAN_DTP_BY_TGT.get(name, [])
            ]
        name = str(params[0]) if params else ""
        if "SRC = ?" in sql:
            return list(_FAN_DTP_BY_SRC.get(name, []))
        return list(_FAN_DTP_BY_TGT.get(name, []))


def _shape(graph: Any) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """The whole answer, in order: batching must not reorder the graph either."""
    return (
        [(n.name, n.object_type) for n in graph.nodes],
        [
            (e.src, e.dst, e.kind, e.transformation_id, e.update_mode, tuple(e.update_modes))
            for e in graph.edges
        ],
    )


def _fan_walk(connection: FanOutConnection) -> Any:
    graph = LineageService(connection, _capability()).get_lineage("HUB", direction="both", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    return graph


@contextmanager
def _prefetch_disabled() -> Iterator[None]:
    """Raise the frontier threshold past any real level, leaving only the per-node reads."""
    previous = lineage_module._PREFETCH_MIN_FRONTIER
    lineage_module._PREFETCH_MIN_FRONTIER = 10**6
    try:
        yield
    finally:
        lineage_module._PREFETCH_MIN_FRONTIER = previous


def test_the_frontier_prefetch_actually_fires_on_a_wide_level() -> None:
    """Guards the guard: without a wide level, none of the tests below prove anything.

    The pre-existing landscape is a chain, so every level is one node and the prefetch is skipped
    by ``_PREFETCH_MIN_FRONTIER``. That is how the batched path came to be fully green and fully
    untested at the same time, so this asserts the batches are issued rather than assuming it.
    """
    batches: list[tuple[tuple[str, ...], bool]] = []
    original = LineageService._prefetch_transformations

    def spy(self: LineageService, names: list[str], *, downstream: bool) -> None:
        if names:
            batches.append((tuple(names), downstream))
        original(self, names, downstream=downstream)

    LineageService._prefetch_transformations = spy  # type: ignore[method-assign]
    try:
        _fan_walk(FanOutConnection())
    finally:
        LineageService._prefetch_transformations = original  # type: ignore[method-assign]

    assert batches, "no batched read was issued, so the batched path is not under test"
    assert any(len(names) > 1 for names, _ in batches), (
        "every batch held one name, which is the per-node read wearing a different SQL shape"
    )


def test_frontier_prefetch_returns_the_identical_graph() -> None:
    """The load-bearing test: batching is only allowed to change cost, never the answer.

    Also the reason a *silently empty* batched read cannot ship unnoticed. If the IN-list predicate
    were wrong, the caches would fill with empty lists, the fallback would never fire, and edges
    would vanish - and that shows up here as a graph that differs from the per-node one.
    """
    batched = _shape(_fan_walk(FanOutConnection()))
    with _prefetch_disabled():
        per_node = _shape(_fan_walk(FanOutConnection()))
    assert batched == per_node


def test_frontier_prefetch_costs_fewer_statements() -> None:
    """The point of the change, asserted rather than asserted-about in a commit message."""
    batched_connection = FanOutConnection()
    _fan_walk(batched_connection)

    per_node_connection = FanOutConnection()
    with _prefetch_disabled():
        _fan_walk(per_node_connection)

    assert batched_connection.statements < per_node_connection.statements, (
        f"batched={batched_connection.statements} per_node={per_node_connection.statements}"
    )


def test_an_unattributable_batched_row_falls_back_instead_of_dropping_edges() -> None:
    """A wrong key-matching assumption must cost speed, not correctness.

    The prefetch attributes each returned row to a requested name by its key column. If that
    mapping fails - padding, case, or a column that does not hold what we think - the tempting
    behaviour is to skip the row, which caches an empty list and quietly deletes edges from the
    graph. Instead the whole chunk is abandoned and every node in it reads for itself, so the
    answer is the per-node answer.
    """

    class Misattributing(FanOutConnection):
        def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
            rows = super()._rstran(sql, params)
            if " IN (" in sql and "TRANID IN" not in sql:
                return [("NO_SUCH_OBJECT", *row[1:]) for row in rows]
            return rows

    degraded = _shape(_fan_walk(Misattributing()))
    healthy = _shape(_fan_walk(FanOutConnection()))
    assert degraded == healthy


def test_a_pair_with_several_dtp_modes_reports_all_of_them() -> None:
    """D13: the edge carried whichever update mode the database happened to return last.

    Found by the batching work rather than by review: the batched and per-node walks over the same
    production ADSO produced identical topology but disagreed on ``update_mode`` for 12 edges, which
    is only possible if the value was order-dependent. A direct read then showed 208 of 1043 active
    object pairs on that system carry more than one active UPDMODE - one pair had five DTPs, three
    full and two delta - so this was a wrong fact on a fifth of the DTP-bearing pairs, not a rare
    tie. It matters because a full load is what makes a re-run destructive and what creates the
    stale-master-data hazard, so silently reporting 'delta' hides exactly the risk worth seeing.
    """
    graph = _fan_walk(FanOutConnection())
    edge = next(e for e in graph.edges if e.src == "HUB" and e.dst == "A1")

    assert list(edge.update_modes) == ["full", "delta"], (
        "both declared modes must survive; the fixture orders the rows so that a last-row-wins "
        "implementation reports 'delta' alone and fails here"
    )
    assert edge.update_mode == "full", "the scalar summary must be the significant mode, not a race"

    # And a pair with one mode still reads as one mode, so the fix did not turn every edge into a
    # list of possibilities.
    single = next(e for e in graph.edges if e.src == "HUB" and e.dst == "A2")
    assert list(single.update_modes) == ["delta"]
    assert single.update_mode == "delta"


def test_the_reported_update_mode_does_not_depend_on_row_order() -> None:
    """The property behind D13, asserted directly: reverse the rows, get the same answer."""

    class Reversed(FanOutConnection):
        def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
            return list(reversed(super()._rstran(sql, params)))

        def _dtp(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:  # type: ignore[override]
            return list(reversed(FanOutConnection._dtp(sql, params)))

    forward = {
        (e.src, e.dst): (e.update_mode, tuple(e.update_modes))
        for e in _fan_walk(FanOutConnection()).edges
    }
    backward = {
        (e.src, e.dst): (e.update_mode, tuple(e.update_modes)) for e in _fan_walk(Reversed()).edges
    }
    assert forward == backward
