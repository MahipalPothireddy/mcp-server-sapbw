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

# A query element sitting on top of the cube, reached by a transformation whose target TLOGO is
# 'ELEM'. Present because a real BW 7.50 system has these and the walk reaching one used to discard
# the entire graph (see test_a_query_element_endpoint_does_not_fail_the_whole_graph).
_QUERY_ELEMENT = "SALES_QRY_ELEM"

# transformation edges: source -> (target, targettype, tranid, sourcetype)
_TRANS_BY_SOURCE = {
    "DS_SALES": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("SALES_CUBE", "CUBE", "TR2")],
    "SALES_CUBE": [(_QUERY_ELEMENT, "ELEM", "TR3")],
}
_TRANS_BY_TARGET = {
    "SALES_DSO": [("DS_SALES", "RSDS", "TR1")],
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR2")],
    _QUERY_ELEMENT: [("SALES_CUBE", "CUBE", "TR3")],
}
# TRANID -> header row (12 cols): OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME,
# START, END, EXPERT, GLBCODE, GLBCODE2
_HEADER = {
    "TR1": ("ACT", "RSDS", "", "DS_SALES", "ODSO", "", "SALES_DSO", "", "", "", "", ""),
    "TR2": ("ACT", "ODSO", "", "SALES_DSO", "CUBE", "", "SALES_CUBE", "CODE_TR2", "", "", "", ""),
    "TR3": ("ACT", "CUBE", "", "SALES_CUBE", "ELEM", "", _QUERY_ELEMENT, "", "", "", "", ""),
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
        # Batched frontier prefetch: keyed by the whole BFS level, so the key column is selected
        # too and every row says which requested name it belongs to.
        if "SRC IN (" in sql:
            return [
                (name, t, ty, um)
                for name in map(str, params)
                for t, ty, um in _DTP_BY_SRC.get(name, [])
            ]
        if "TGT IN (" in sql:
            return [
                (name, s, ty, um)
                for name in map(str, params)
                for s, ty, um in _DTP_BY_TGT.get(name, [])
            ]
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
_FAN_DTP_BY_SRC = {
    "HUB": [("A1", "ODSO", "F"), ("A1", "ODSO", "D"), ("A2", "ODSO", "D")],
    "A1": [("B1", "CUBE", "D")],
}
_FAN_DTP_BY_TGT = {
    "A1": [("HUB", "ODSO", "F"), ("HUB", "ODSO", "D")],
    "A2": [("HUB", "ODSO", "D")],
    "B1": [("A1", "ODSO", "D")],
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
        if "SRC IN (" in sql:
            return [
                (name, t, ty, um)
                for name in map(str, params)
                for t, ty, um in _FAN_DTP_BY_SRC.get(name, [])
            ]
        if "TGT IN (" in sql:
            return [
                (name, s, ty, um)
                for name in map(str, params)
                for s, ty, um in _FAN_DTP_BY_TGT.get(name, [])
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
