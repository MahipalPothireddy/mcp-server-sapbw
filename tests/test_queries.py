"""Tests for the BEx query repository (B7), offline against scripted fixtures.

Synthetic names only. Landscape:
    QUERY_SALES (COMPUID Q1UID) on provider SALES_CUBE
      elements: root(REP) -> E_RKF(SEL, restricts CURRENCY via customer-exit var USD_VAR) [COL]
                          -> E_CHAR(SEL, restricts MATERIAL = 'M100' literal)             [ROW]
    provider trace: SALES_CUBE <- SALES_DSO <- DS_SALES (datasource)
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.queries import (
    _PROPERTY_COLUMNS,
    QueriesRepository,
    classify_origin,
)
from mcp_server_sapbw.services.lineage import LineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "element_range": "RSZRANGE",
    "element_select": "RSZSELECT",
    "element_calc": "RSZCALC",
    "element_prop": "RSZELTPROP",
    "global_variable": "RSZGLOBV",
    "transformation": "RSTRAN",
    "dtp": "RSBKDTP",
    # The source-system boundary: RSDS names the extract structure, RSDSSEGFD proves an enhancement.
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
}

# COMPUID, COMPID, OWNER, TSTPNM, LASTUSED, OBJSTAT
_HEADER = ("Q1UID", "QUERY_SALES", "DEV", "DEVUSER", "20260101000000", "ACT")
_XREF = {"Q1UID": [("E_RKF", "COL", 1), ("E_CHAR", "ROW", 2)], "E_RKF": [], "E_CHAR": []}
_DIR = {  # ELTUID -> (DEFTP, MAPNAME, REUSABLE)
    "Q1UID": ("REP", "QUERY_SALES", "X"),
    "E_RKF": ("SEL", "RKF_AMOUNT", "X"),
    "E_CHAR": ("SEL", "", ""),
}
_TXT = {  # ELTUID -> (TXTSH, TXTLG)
    "Q1UID": ("Sales Qry", "Sales query by material"),
    "E_RKF": ("Net amt", "Net amount RKF"),
}
_RANGE = {  # ELTUID -> [(IOBJNM, SIGN, OPT, LOW, HIGH, LOWFLAG, HIGHFLAG)]
    "E_RKF": [("CURRENCY", "I", "EQ", "USD_VAR", "", "3", "0")],  # LOW is a variable ref (flag 3)
    "E_CHAR": [("MATERIAL", "I", "EQ", "M100", "", "1", "0")],  # literal (flag 1)
}
_SELECT = {"E_RKF": ["AMOUNT"], "E_CHAR": ["MATERIAL"]}
# RSZCALC per element, ordered by STEPNR:
#   (AGGRGEN, AGGREXC, AGGRCHA, AGGRCHA2, AGGRCHA3, AGGRCHA4, AGGRCHA5, AGGREXCLUDE)
# E_RKF counts distinct materials, so its value is NOT the sum of the underlying rows.
_CALC: dict[str, list[tuple[Any, ...]]] = {
    "E_RKF": [
        ("SUM", "", "", "", "", "", "", ""),
        ("", "CNT", "MATERIAL", "PLANT", "", "", "", ""),
    ],
}
# RSZELTPROP per element, written by column name and projected into _PROPERTY_COLUMNS order below.
# Naming the columns matters here: the row is 24 wide and a positional fixture silently tests the
# wrong column the moment the SELECT list changes.
#
# E_RKF translates to USD and inverts its sign; E_CHAR displays along a hierarchy chosen by a
# variable, aggregates locally as a last value, is hidden, and carries its own key date. Between
# them they cover every code family: a literal source, a runtime-resolved source, a non-summation
# local aggregation, a three-valued boolean, and BW's own defaults.
_PROP_BY_NAME: dict[str, dict[str, str]] = {
    "E_RKF": {
        "TCUR": "USD",
        "TCURFLAG": "1",
        "CTTNM": "STD_RATE",
        "NOSUMS": "U",
        "SIGNINV": "X",
    },
    "E_CHAR": {
        "HIENM": "HIER_VAR",
        "HIENMFLAG": "3",
        "STRT_LVL": "02",
        "HRY_ACTIVE": "X",
        "STRMEM_LAGGR": "12",
        "LAGGR_DIR": "1",
        "HIDDEN": "X",
        "KEYDATE": "20260101",
        "KEYDATEFLAG": "1",
    },
}
# Unset columns default the way BW does: a NUMC flag holds '0'/'00', a CHAR column holds blank.
_PROP_DEFAULTS = {"TCURFLAG": "0", "TCURDATEFLAG": "0", "TUOMFLAG": "0", "HIENMFLAG": "0"}
_PROP_NUMC = {"STRT_LVL": "00", "STRMEM_LAGGR": "00", "LAGGR_DIR": "0", "KEYDATEFLAG": "0"}


def _prop_row(values: dict[str, str]) -> tuple[Any, ...]:
    """Project a by-name fixture onto the repository's SELECT list, minus the leading ELTUID."""
    return tuple(
        values.get(column, _PROP_DEFAULTS.get(column, _PROP_NUMC.get(column, "")))
        for column in _PROPERTY_COLUMNS[1:]
    )


_PROP: dict[str, tuple[Any, ...]] = {
    eltuid: _prop_row(values) for eltuid, values in _PROP_BY_NAME.items()
}

_GLOBV = {"USD_VAR": ("1", "3", "CURRENCY", "")}  # VPROCTP 3 = customer exit
_TRANS_BY_TARGET = {
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("DS_SALES", "RSDS", "TR0")],
}
#: Each object's own RSTLOGO code, for the type probe. SALES_CUBE appears only as a *target*, which
#: is exactly why a query's provider was reaching callers untyped.
_TYPE_CODE = {"SALES_CUBE": "CUBE", "SALES_DSO": "ODSO", "DS_SALES": "RSDS"}
# RSDS, keyed by DataSource: (EXSTRUCTURE, TYPE, DELTA). The extract structure the source system
# fills is what takes the walk one hop past the DataSource.
_RSDS = {"DS_SALES": ("EXTSTRU_SALES", "D", "ABR")}
#: Customer-namespace fields on the extract structure: metadata-confirmed enhancement evidence.
_RSDS_CUSTOM_FIELDS = {"DS_SALES": 4}


def _in_params(sql: str, params: list[Any]) -> set[str]:
    return {str(p) for p in params}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "RSZCOMPDIR" in sql:
            return self._compdir(sql, params)
        if "RSZCOMPIC" in sql:
            return self._compic(sql, params)
        if "RSZELTXREF" in sql:
            return [(c, lay, pos) for c, lay, pos in _XREF.get(str(params[-1]), [])]
        if "RSZELTDIR" in sql:
            ids = _in_params(sql, params)
            return [(k, *v) for k, v in _DIR.items() if k in ids]
        if "RSZELTTXT" in sql:
            ids = _in_params(sql, params[1:])  # first param is LANGU
            return [(k, sh, lg) for k, (sh, lg) in _TXT.items() if k in ids]
        if "RSZRANGE" in sql:
            ids = _in_params(sql, params)
            return [(k, *r) for k, rs in _RANGE.items() if k in ids for r in rs]
        if "RSZSELECT" in sql:
            ids = _in_params(sql, params)
            return [(o,) for k, objs in _SELECT.items() if k in ids for o in objs]
        if "RSZCALC" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, rows in _CALC.items() if k in ids for row in rows]
        if "RSZELTPROP" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, row in _PROP.items() if k in ids]
        if "RSZGLOBV" in sql:
            ids = _in_params(sql, params)
            return [(k, *v) for k, v in _GLOBV.items() if k in ids]
        if "RSBKDTP" in sql:
            return []
        if "RSDSSEGFD" in sql:  # customer-namespace field count for one DataSource
            return [(_RSDS_CUSTOM_FIELDS.get(str(params[-1]), 0),)]
        if "RSDS" in sql:  # the DataSource header: extract structure, type, delta
            row = _RSDS.get(str(params[0]))
            return [row] if row else []
        if "RSTRAN" in sql:
            # Two different reads hit RSTRAN and they return different shapes. The type probe asks
            # for one column and no TRANID; the lineage walk asks for the other endpoint plus the
            # TRANID. Serving one shape for both is how a fixture passes while the real system
            # behaves differently - the type probe would read a *name* out of the type column.
            if "TRANID" not in sql:
                return self._own_type(sql, params)
            return [(s, ty, tr) for s, ty, tr in _TRANS_BY_TARGET.get(str(params[-1]), [])]
        return []

    @staticmethod
    def _own_type(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        """``SELECT SOURCETYPE WHERE SOURCENAME = ?`` / the TARGET pair: an object's own TLOGO code.

        Answers only for the side the object really appears on, so an object that is never a source
        returns nothing for the SOURCETYPE probe - which is what makes the fallback order in
        ``_node_type_uncached`` meaningful rather than incidental.
        """
        # params[0], not params[-1]: this read is paginated, so the trailing bound values are the
        # LIMIT and OFFSET. Keying on the last one silently probed for the object named "0".
        name = str(params[0]).strip()
        code = _TYPE_CODE.get(name)
        if code is None:
            return []
        as_source = any(name == s for rows in _TRANS_BY_TARGET.values() for s, _t, _tr in rows)
        as_target = name in _TRANS_BY_TARGET
        if "SOURCETYPE" in sql:
            return [(code,)] if as_source else []
        if "TARGETTYPE" in sql:
            return [(code,)] if as_target else []
        return []

    @staticmethod
    def _compdir(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "TSTPNM" in sql:  # header (6 cols)
            ident = str(params[-1])
            return [_HEADER] if ident in (_HEADER[0], _HEADER[1]) else []
        # list (4 cols): COMPUID, COMPID, OWNER, LASTUSED. Both a Query-Designer query and an
        # ad-hoc one, so origin classification and the SQL-level filter are both exercised.
        rows = [
            (_HEADER[0], _HEADER[1], _HEADER[2], _HEADER[4]),
            ("Q2UID", "!!1ADHOC", "ANALYST", _HEADER[4]),
        ]
        pattern = next((str(p) for p in params if str(p).startswith("!!")), None)
        if pattern is None:
            return rows
        wants_designed = "NOT LIKE" in sql
        return [r for r in rows if str(r[1]).startswith("!!") is not wants_designed]

    @staticmethod
    def _compic(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "INFOCUBE = ?" in sql:  # compuids for provider
            return [("Q1UID",)] if str(params[-1]) == "SALES_CUBE" else []
        if "COMPUID IN" in sql:  # providers_for (list)
            return [("Q1UID", "SALES_CUBE", "X")]
        return [("SALES_CUBE", "X")]  # providers_list (COMPUID = ?)


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


def _repo(present: set[str] | None = None) -> QueriesRepository:
    return QueriesRepository(ScriptedConnection(), _capability(present))


def test_get_query_header_and_description_via_compuid_join() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert query.compid == "QUERY_SALES"
    # description comes from RSZELTTXT where ELTUID == COMPUID (the query is itself an element)
    assert query.description == "Sales query by material"
    assert query.provider == "SALES_CUBE"
    assert query.active is True


def test_get_query_element_tree_and_types() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    by_uid = {e.eltuid: e for e in query.elements}
    assert by_uid["Q1UID"].element_type == "query"
    assert by_uid["E_RKF"].element_type == "restricted_key_figure"
    roles = {(e.parent_uid, e.child_uid): e.role for e in query.edges}
    assert roles[("Q1UID", "E_RKF")] == "columns"
    assert roles[("Q1UID", "E_CHAR")] == "rows"


def test_get_query_restrictions_variable_vs_literal() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    rkf = next(e for e in query.elements if e.eltuid == "E_RKF")
    curr = next(r for r in rkf.restrictions if r.iobjnm == "CURRENCY")
    assert curr.low_is_variable is True  # LOWFLAG = 3
    assert curr.low == "USD_VAR"
    char = next(e for e in query.elements if e.eltuid == "E_CHAR")
    mat = next(r for r in char.restrictions if r.iobjnm == "MATERIAL")
    assert mat.low_is_variable is False
    assert mat.low == "M100"


def test_get_query_customer_exit_variable_flagged() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    var = next(v for v in query.variables if v.name == "USD_VAR")
    assert var.processing_type == "customer_exit"
    assert var.is_customer_exit is True
    assert var.iobjnm == "CURRENCY"


def test_get_query_usage() -> None:
    usage = _repo().get_query_usage("QUERY_SALES")
    assert not isinstance(usage, UnsupportedResult)
    assert usage.last_used is not None
    assert usage.last_used.year == 2026


def test_get_query_lineage_reaches_datasource_and_flags_exit_var() -> None:
    lineage = _repo().get_query_lineage("QUERY_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    assert "SALES_CUBE" in lineage.providers
    assert "USD_VAR" in lineage.customer_exit_variables
    iobjs = {p.iobjnm for p in lineage.paths}
    assert {"MATERIAL", "AMOUNT", "CURRENCY"} <= iobjs
    material = next(p for p in lineage.paths if p.iobjnm == "MATERIAL")
    assert material.reaches_datasource is True
    assert [h.via for h in material.hops][-1] == "datasource"
    # The provider's boundary set is carried once on the result rather than restated per field.
    assert "DS_SALES" in lineage.provider_datasources


def test_the_provider_boundary_is_named_once_not_repeated_per_field() -> None:
    """The 6,500-hop defect: the fallback used to append every DataSource to every field's path.

    Two properties together are the fix. A fallback path carries *one* boundary hop rather than one
    per DataSource, and the names live on the result. Asserting only the first would pass on a
    version that dropped the names entirely, which is a different kind of wrong answer.
    """
    lineage = _repo().get_query_lineage("QUERY_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    fallbacks = [p for p in lineage.paths if p.resolution == "provider"]
    assert fallbacks, "the fixture must exercise the fallback for this test to mean anything"
    for path in fallbacks:
        boundary = [h for h in path.hops if h.via == "datasource"]
        assert len(boundary) == 1, (
            "the provider's DataSources are alternatives, not a chain: one boundary hop, not one "
            f"hop each. {path.iobjnm} carried {len(boundary)}"
        )
        assert boundary[0].advisory is True, (
            "the provider's boundary is not this field's derivation"
        )
    assert lineage.provider_datasources, "the boundary set must still be reachable"


def test_lineage_service_resolves_query_to_provider_and_datasource() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="both", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "QUERY_SALES" in names
    assert "SALES_CUBE" in names
    assert "DS_SALES" in names
    assert any(e.kind == "query_provider" for e in graph.edges)


# --- past the DataSource: the source-system boundary --------------------------------------------
#
# The DataSource used to be a hard stop. ``source_extract`` and ``source_object`` were defined in
# the model and never produced, so an upstream walk answered "this came from a DataSource" and left
# the next question - extracted by what? - unanswered.


def test_the_querys_own_provider_is_typed_not_left_unknown() -> None:
    """RSZCOMPIC names the provider but carries no type, and no hop types it either.

    Every other node learns its type from the hop that discovered it, because RSTRAN carries the
    other endpoint's type code. A query's provider is the *target* of its inbound transformations,
    so it is never on the naming side - and it was reaching callers as ``unknown``, which on a
    diagram is the one grey box labelled "object" sitting where the subject of the whole graph
    should be.
    """
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    provider = next(n for n in graph.nodes if n.name == "SALES_CUBE")
    assert provider.object_type != "unknown"
    assert provider.object_type == "infocube"
    # And the canonical ref agrees, so a caller can join it against bw_describe_object.
    assert provider.ref is not None and provider.ref.object_type == "infocube"


def test_the_walk_continues_past_the_datasource_to_its_extract_structure() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    by_type = {n.name: n.object_type for n in graph.nodes}
    assert by_type.get("EXTSTRU_SALES") == "source_object"
    edge = next(e for e in graph.edges if e.kind == "source_extract")
    assert (edge.src, edge.dst) == ("EXTSTRU_SALES", "DS_SALES")
    assert edge.confidence == "exact", "RSDS declares this; it is not a naming-convention reading"


def test_the_boundary_node_stops_claiming_its_upstream_is_unresolved() -> None:
    """A node with a resolved parent in the same graph must not also say the graph ends there."""
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    boundary = next(n for n in graph.nodes if n.name == "DS_SALES")
    assert boundary.upstream_resolved is True
    assert boundary.source_system is not None
    assert boundary.source_system.object_name == "EXTSTRU_SALES"


def test_trace_to_source_reports_a_resolved_boundary_as_resolved() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    trace = service.trace_to_source("QUERY_SALES", depth=4)
    assert not isinstance(trace, UnsupportedResult)
    assert "DS_SALES" in trace.datasources_reached
    assert "DS_SALES" not in trace.unresolved_boundaries, (
        "the extract structure was resolved, so listing the DataSource as an open boundary would "
        "leave a caller no way to tell which boundaries really are still open"
    )
    assert any("extract structure" in c for c in trace.caveats)


def test_the_boundary_edge_reports_enhancement_evidence_and_names_the_gap() -> None:
    """Customer-namespace fields prove an enhancement exists; the code itself is not in BW."""
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    edge = next(e for e in graph.edges if e.kind == "source_extract")
    assert edge.note is not None
    assert "4 customer-namespace field(s)" in edge.note
    assert "bw_get_extractor_exit_code" in edge.note, "the gap must name the tool that closes it"
    assert "delta method ABR" in edge.note


def test_no_extract_structure_means_no_invented_boundary_node() -> None:
    """An unresolved boundary is a better answer than a synthesized extractor name."""

    class NoRsds(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDSSEGFD" not in sql and "RSDS" in sql:
                return []
            return super().execute_select(sql, parameters)

    graph = LineageService(NoRsds(), _capability()).get_lineage(
        "QUERY_SALES", direction="upstream", depth=4
    )
    assert not isinstance(graph, UnsupportedResult)
    assert not any(e.kind == "source_extract" for e in graph.edges)
    assert not any(n.object_type == "source_object" for n in graph.nodes)
    boundary = next(n for n in graph.nodes if n.name == "DS_SALES")
    assert boundary.upstream_resolved is False


def test_an_absent_datasource_table_leaves_the_boundary_where_it_was() -> None:
    service = LineageService(
        ScriptedConnection(), _capability(present=set(_TABLES) - {"datasource"})
    )
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    assert not any(e.kind == "source_extract" for e in graph.edges)


# --- a node that is not a BW object is not asked BW questions ------------------------------------


def test_an_extract_structure_is_terminal_rather_than_expanded_as_a_provider() -> None:
    """It is source-system ABAP: no transformation targets it and no query reads it.

    Asked anyway, each boundary node cost a full provider expansion to conclude nothing - measured,
    about 200 statements on a production walk. It still belongs in the graph; only its expansion is
    meaningless.
    """

    class Counting(ScriptedConnection):
        def __init__(self) -> None:
            self.seen: list[str] = []

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            self.seen.append(str(list(parameters or [])))
            return super().execute_select(sql, parameters)

    connection = Counting()
    graph = LineageService(connection, _capability()).get_lineage(
        "QUERY_SALES", direction="upstream", depth=6
    )
    assert not isinstance(graph, UnsupportedResult)
    assert any(n.name == "EXTSTRU_SALES" for n in graph.nodes), "the node must still be present"
    asked_about_it = [s for s in connection.seen if "EXTSTRU_SALES" in s]
    assert asked_about_it == [], (
        f"the extract structure was expanded as a BW object: {asked_about_it}"
    )


# --- depth counts load layers, not hops ---------------------------------------------------------


def test_resolving_the_query_to_its_provider_does_not_spend_a_depth_level() -> None:
    """From a query root, ``depth`` must mean what it means from the provider it reads.

    Charged a level, ``depth=2`` from the query reached only the DSO where the same request on the
    provider reached the DataSource - so the same number meant different things depending on which
    object the caller happened to name.
    """
    service = LineageService(ScriptedConnection(), _capability())
    from_query = service.get_lineage("QUERY_SALES", direction="upstream", depth=2)
    from_provider = service.get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(from_query, UnsupportedResult)
    assert not isinstance(from_provider, UnsupportedResult)
    reached_from_query = {n.name for n in from_query.nodes} - {"QUERY_SALES"}
    assert reached_from_query >= {n.name for n in from_provider.nodes}


def test_crossing_the_source_boundary_does_not_spend_a_depth_level_either() -> None:
    """Charged, the extractor appeared only for DataSources that had a level left over."""
    service = LineageService(ScriptedConnection(), _capability())
    # Exactly enough depth to reach the DataSource: the extract structure must come with it.
    graph = service.get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "DS_SALES" in names
    assert "EXTSTRU_SALES" in names


def test_list_queries_and_provider_filter() -> None:
    repo = _repo()
    result = repo.list_queries()
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert summaries[0].compid == "QUERY_SALES"
    filtered = repo.list_queries(provider="SALES_CUBE")
    assert not isinstance(filtered, UnsupportedResult)
    assert filtered[1] == 1


# --- origin: designed report vs ad-hoc BEx-Analyzer navigation ---------------------------------


def _summaries(**kwargs: object) -> list[Any]:
    result = _repo().list_queries(**kwargs)  # type: ignore[arg-type]
    assert not isinstance(result, UnsupportedResult)
    return result[0]


def test_designed_query_is_classified_as_designed() -> None:
    designed = next(s for s in _summaries() if s.compid == "QUERY_SALES")
    assert designed.origin == "designed"


def test_double_bang_prefix_is_classified_as_ad_hoc() -> None:
    """SAP generates the '!!' name for a query created straight in the BEx Analyzer."""
    ad_hoc = next(s for s in _summaries() if s.compid == "!!1ADHOC")
    assert ad_hoc.origin == "ad_hoc"


def test_origin_defaults_to_unfiltered() -> None:
    assert len(_summaries()) == 2


def test_designed_filter_excludes_ad_hoc() -> None:
    names = {s.compid for s in _summaries(origin="designed")}
    assert names == {"QUERY_SALES"}


def test_ad_hoc_filter_returns_only_ad_hoc() -> None:
    names = {s.compid for s in _summaries(origin="ad_hoc")}
    assert names == {"!!1ADHOC"}


def test_classify_origin_is_a_pure_name_reading() -> None:
    assert classify_origin("!!ANY") == "ad_hoc"
    assert classify_origin("NORMAL") == "designed"
    # A single "!" is not the marker, and a missing name is not evidence of ad-hoc creation.
    assert classify_origin("!ONE") == "designed"
    assert classify_origin(None) == "designed"


def test_unsupported_without_query_dir() -> None:
    result = _repo(present={"element_dir"}).get_query("QUERY_SALES")
    assert isinstance(result, UnsupportedResult)


# --- aggregation on query elements (RSZCALC) ---------------------------------------------------


def _element(eltuid: str) -> Any:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    return next(e for e in query.elements if e.eltuid == eltuid)


def test_calc_step_count_is_populated() -> None:
    """The field existed but nothing filled it, because RSZCALC was never read."""
    assert _element("E_RKF").calc_step_count == 2
    assert _element("E_CHAR").calc_step_count == 0


def test_exception_aggregation_is_attached_to_the_element() -> None:
    exc = _element("E_RKF").exception_aggregation
    assert exc is not None
    assert exc.behaviour.code == "CNT"
    assert exc.behaviour.label == "Counter (all values)"
    assert [r.name for r in exc.reference_characteristics] == ["MATERIAL", "PLANT"]
    assert exc.reproducible_by_summation is False


def test_standard_aggregation_comes_from_the_first_step() -> None:
    standard = _element("E_RKF").standard_aggregation
    assert standard is not None
    assert standard.code == "SUM"
    assert standard.label == "Summation"


def test_element_without_calc_rows_has_no_aggregation() -> None:
    assert _element("E_CHAR").exception_aggregation is None
    assert _element("E_CHAR").standard_aggregation is None


def test_query_caveat_warns_that_totals_will_not_match() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    joined = " ".join(query.caveats)
    assert "exception aggregation" in joined
    assert "CNT" in joined
    assert "not reproduced by adding the underlying rows up" in joined


# --- element properties (RSZELTPROP) -----------------------------------------------------------


@contextmanager
def _property_override(eltuid: str, column: str, value: str) -> Iterator[None]:
    """Replace one RSZELTPROP column by name, so a test never depends on a tuple index."""
    index = _PROPERTY_COLUMNS.index(column) - 1  # the fixture rows omit the leading ELTUID
    original = dict(_PROP)
    _PROP[eltuid] = tuple(value if i == index else v for i, v in enumerate(original[eltuid]))
    try:
        yield
    finally:
        _PROP.clear()
        _PROP.update(original)


def test_currency_translation_is_read_with_its_translation_type() -> None:
    props = _element("E_RKF").properties
    assert props is not None
    currency = props.currency_translation
    assert currency is not None
    assert (currency.target_currency, currency.translation_type) == ("USD", "STD_RATE")
    assert currency.target_source is not None
    assert currency.target_source.value_holds == "literal"
    assert currency.target_source.runtime_resolved is False


def test_sign_inversion_and_total_suppression_are_decoded() -> None:
    props = _element("E_RKF").properties
    assert props is not None
    assert props.sign_inverted is True
    assert props.total_suppressed is True
    assert props.total_suppression == "Suppress the total unconditionally"


def test_local_aggregation_uses_the_numeric_domain() -> None:
    """STRMEM_LAGGR is domain RRLAGGR ('00'-'13'), not the three-letter exception codes."""
    props = _element("E_CHAR").properties
    assert props is not None
    aggregation = props.local_aggregation
    assert aggregation is not None
    assert (aggregation.code, aggregation.label) == ("12", "Last value")
    assert aggregation.is_summation is False
    assert props.local_aggregation_direction == "Calculate along the rows"


def test_summation_local_aggregation_is_not_reported_as_altering_the_value() -> None:
    """'01' is summation, so it changes nothing about how the figure relates to its rows."""
    with _property_override("E_CHAR", "STRMEM_LAGGR", "01"):
        props = _element("E_CHAR").properties
        assert props is not None
        assert props.local_aggregation is not None
        assert props.local_aggregation.is_summation is True
        assert not any("aggregates locally" in r for r in props.changes_the_number)


def test_runtime_resolved_hierarchy_is_flagged_not_reported_as_a_name() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    hierarchy = props.display_hierarchy
    assert hierarchy is not None
    assert hierarchy.source is not None
    assert hierarchy.source.runtime_resolved is True
    assert hierarchy.source.value_holds == "variable_name"
    assert hierarchy.start_level == 2
    assert hierarchy.active is True


def test_hidden_element_is_decoded_from_its_own_domain() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    assert props.hidden is True
    assert props.display == "Hide"


def test_own_key_date_is_reported_as_changing_the_number() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    assert props.key_date == "20260101"
    assert props.key_date_source is not None
    assert any("key date of its own" in r for r in props.changes_the_number)


def test_changes_the_number_names_only_the_settings_that_do() -> None:
    rkf = _element("E_RKF").properties
    assert rkf is not None
    reasons = " ".join(rkf.changes_the_number)
    assert "translated to USD" in reasons
    assert "sign is inverted" in reasons
    # Total suppression hides a figure; it does not change the ones that are shown.
    assert "suppress" not in reasons.lower()


def test_query_caveat_names_the_elements_that_alter_their_value() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    caveat = next(c for c in query.caveats if "alter their own value" in c)
    assert "translated to USD" in caveat
    assert "RKF_AMOUNT" in caveat  # named by MAPNAME where it has one


def test_flag_set_without_a_stored_value_is_stated_honestly() -> None:
    """852 elements declare a currency target on the reference system; only 817 store one."""
    with _property_override("E_RKF", "TCUR", ""):
        props = _element("E_RKF").properties
        assert props is not None
        reasons = " ".join(props.changes_the_number)
        assert "declares" in reasons and "does not record" in reasons
        assert "runtime" not in reasons  # a fixed value is not resolved at runtime


def test_absent_property_table_is_stated_not_treated_as_nothing_configured() -> None:
    query = _repo(present=set(_TABLES) - {"element_prop"}).get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert all(e.properties is None for e in query.elements)
    assert any("were not read" in c and "unknown rather than absent" in c for c in query.caveats)


def test_no_aggregation_caveat_when_everything_sums() -> None:
    """A query whose elements all sum normally must not carry a scary caveat."""
    original = dict(_CALC)
    _CALC.clear()
    _CALC["E_RKF"] = [("SUM", "SUM", "MATERIAL", "", "", "", "", "")]
    try:
        query = _repo().get_query("QUERY_SALES")
        assert not isinstance(query, UnsupportedResult)
        assert not any("exception aggregation" in c for c in query.caveats)
        exc = next(e for e in query.elements if e.eltuid == "E_RKF").exception_aggregation
        assert exc is not None
        assert exc.reproducible_by_summation is True
    finally:
        _CALC.clear()
        _CALC.update(original)


def test_disagreeing_steps_are_reported_not_silently_resolved() -> None:
    original = dict(_CALC)
    _CALC.clear()
    _CALC["E_RKF"] = [
        ("", "CNT", "MATERIAL", "", "", "", "", ""),
        ("", "LAS", "CALDAY", "", "", "", "", ""),
    ]
    try:
        exc = _element("E_RKF").exception_aggregation
        assert exc is not None
        assert exc.behaviour.code == "CNT"  # first step wins
        assert "different exception aggregations" in (exc.note or "")
    finally:
        _CALC.clear()
        _CALC.update(original)


def test_missing_calc_table_degrades_quietly() -> None:
    repo = QueriesRepository(
        ScriptedConnection(), _capability(present=set(_TABLES) - {"element_calc"})
    )
    query = repo.get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert all(e.exception_aggregation is None for e in query.elements)
    assert all(e.calc_step_count == 0 for e in query.elements)
