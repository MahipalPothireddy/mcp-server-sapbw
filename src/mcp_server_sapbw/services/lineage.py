"""Lineage service (B6).

Builds a directed data-flow graph over BW objects by expanding, one hop at a time, the declared
edges from transformations (RSTRAN source->target) and DTPs (RSBKDTP, with update mode), plus
routine-derived edges from the B5 parser. Three entry points:

- ``get_lineage``     - BFS upstream / downstream / both to a depth, as a graph.
- ``trace_to_source`` - upstream BFS to the DataSource boundary, hop by hop.
- ``impact_analysis`` - downstream blast radius PLUS the objects whose *routines* read the root
                        (a reverse RSAABAP scan) - dependencies invisible to BW's where-used lists.

Declared edges are exact; routine-derived edges are advisory (heuristic lower bound). The DataSource
is a boundary node (``upstream_resolved=False`` in the BW-only build). Node ids are the object's
technical name (BW names are unique enough for lineage); the object type is a separate field.
"""

from __future__ import annotations

from collections import deque
from typing import Any, cast

from ..core.budget import current_budget
from ..models.evidence import evidence_for, summarise
from ..models.lineage import (
    ImpactAnalysis,
    LineageCompleteness,
    LineageDirection,
    LineageEdge,
    LineageGraph,
    LineageNode,
    LineageNodeType,
    SourceSystemRef,
    TraceToSource,
    UpdateMode,
)
from ..models.objects import BwObjectRef, normalise_object_type
from ..models.provenance import Provenance, UnsupportedResult
from ..models.providers import Provider
from ..repositories.base import Repository
from ..repositories.providers import ProvidersRepository
from ..repositories.transformations import TransformationsRepository
from .table_resolver import (
    CALC_VIEW_HIER_MARKER,
    CALC_VIEW_PACKAGE,
    candidate_tables,
    provider_from_calc_view,
    query_from_calc_view,
    split_datasource_endpoint,
)


# RSTLOGO type code -> lineage node type. Decoded through the canonical table in models.objects
# rather than a local copy: three copies of this map existed and had already drifted (this one said
# "cube" where the provider vocabulary said "infocube").
def _node_type(code: object) -> LineageNodeType:
    """Canonical node type for a raw TLOGO code or a provider-type string.

    The ``cast`` is checked, not assumed. ``normalise_object_type`` returns the wider
    ``BwObjectType``, and mypy cannot relate two unconnected ``Literal`` types, so this cast is the
    only thing bridging them - which means it can be wrong at runtime while type-checking cleanly.
    It was: ``ELEM`` decoded to ``query_element``, which ``LineageNodeType`` did not contain, and
    the resulting Pydantic failure discarded the whole graph instead of one node. A test now asserts
    that every type this function can return is a member of ``LineageNodeType``, so the cast rests
    on a verified invariant rather than on a promise.
    """
    return cast("LineageNodeType", normalise_object_type(code))


_UPDMODE_MAP: dict[str, UpdateMode] = {"F": "full", "D": "delta", "I": "init"}

# Most operationally significant first. A full load is what makes a re-run destructive and what
# creates the stale-lookup hazard scenario 9.1 looks for, so where a pair carries several modes the
# scalar `update_mode` reports the full one rather than whichever row the database returned last.
_UPDMODE_PRECEDENCE: tuple[UpdateMode, ...] = ("full", "init", "delta")


def _sorted_modes(modes: set[UpdateMode]) -> list[UpdateMode]:
    """Distinct update modes in significance order, so the scalar summary is reproducible."""
    return [mode for mode in _UPDMODE_PRECEDENCE if mode in modes]


# HANA schema holding generated BW calc views. Part-provider edges need TRANSITIVE dependencies
# (type 2): BW layers a CompositeProvider's calc view over intermediate views, so a part provider's
# active table is never a *direct* dependency of it (verified live). Bounded by the table filter.
_CALC_SCHEMA = "_SYS_BIC"
_TRANSITIVE_DEPENDENCY = 2

# How many *distinct resolved consuming objects* one node may contribute - CompositeProviders and
# BEx queries together, since both are discovered from the same read. This is a semantic cap,
# and the distinction is the whole of defect D7: it used to bound raw dependency rows instead.
# One provider's tables carry thousands of generated dependent views - measured on a QA system,
# 8,692 rows for one provider, of which 7,040 were hierarchy runtime views that can never be a
# provider - so an unordered cap over raw rows sampled roughly 0.2 usable providers per execution
# against a true answer of 35, and drew a different arbitrary subset every time it ran.
_MAX_COMPOSITE_CONSUMERS = 50
# BEx queries BW *declares* against one provider (RSZCOMPIC, filtered to query roots). Bounded by
# construction rather than by expectation: the widest provider measured on a production system
# declares 73, so this leaves headroom while still capping. Note the filter is load-bearing, not a
# tidy-up - RSZCOMPIC holds a row per query *component*, and the widest CompositeProvider measured
# had 267 of them for 48 actual queries; without the root-element filter a provider would contribute
# its structures and calculated key figures to the graph as though they were reports.
_MAX_DECLARED_QUERY_CONSUMERS = 250
# Rows per deterministic page. Comfortably larger than the pruned population measured on a real
# system (36 rows for the widest provider seen), so the common case stays a single statement.
_CONSUMER_PAGE_ROWS = 500
# Hard bound on paging, so discovery is bounded by construction rather than by the query budget.
_MAX_CONSUMER_PAGES = 20
# Headroom left for the rest of the walk before another page is started. Discovery reports that it
# stopped instead of consuming the last of the allowance and failing the whole call.
_BUDGET_QUERY_HEADROOM = 5
_BUDGET_TIME_HEADROOM = 5.0

# Most-to-least severe. A walk that hit several bounds reports the one that most limits the answer.
_COMPLETENESS_PRECEDENCE: tuple[LineageCompleteness, ...] = (
    "error_degraded",
    "time_budget",
    "query_budget",
    "node_limit",
    "semantic_limit",
    "unsupported_branch",
)

_MAX_NODES = 400
_MAX_DEPTH = 12
_ROUTINE_TRANS_CAP = 25  # transformations-per-node whose routines we parse for lookups
_REVERSE_CODEID_CAP = 150  # RSAABAP code-ids scanned in the reverse (impact) routine search

# Frontier prefetch. Names per batched IN-list: RSTRAN and RSBKDTP are small (~1.3k rows on a
# measured production system) so the predicate is cheap, and the chunk exists to keep the bind
# parameter count well inside any driver's limit rather than to bound the scan.
_PREFETCH_CHUNK = 200
# Below this a batch is just the single-node query plus bookkeeping, so it is not worth issuing.
_PREFETCH_MIN_FRONTIER = 2

# Generated table names per batched OBJECT_DEPENDENCIES read. Each provider contributes several
# candidate tables, so this bounds the bind-parameter count rather than the number of nodes.
_PREFETCH_TABLE_CHUNK = 600
# Whole-batch row bound for that read. Reaching it means the batch cannot be attributed to nodes
# without possibly truncating one of them, so the batch is abandoned and every node in it falls back
# to its own bounded, self-reporting read. Ten times the widest single-provider population measured.
_PREFETCH_CONSUMER_MAX_ROWS = _CONSUMER_PAGE_ROWS * _MAX_CONSUMER_PAGES

# (key column, other-endpoint name column, other-endpoint type column) per direction. Shared by the
# single-node reads and their batched prefetch so the two cannot drift into reading different rows.
_TRAN_COLUMNS: dict[bool, tuple[str, str, str]] = {
    True: ("SOURCENAME", "TARGETNAME", "TARGETTYPE"),
    False: ("TARGETNAME", "SOURCENAME", "SOURCETYPE"),
}
_DTP_COLUMNS: dict[bool, tuple[str, str, str]] = {
    True: ("SRC", "TGT", "TGTTLOGO"),
    False: ("TGT", "SRC", "SRCTLOGO"),
}


def _prefetch_directions(direction: LineageDirection) -> tuple[bool, ...]:
    """Which declared-edge branches a walk in ``direction`` will ask for."""
    if direction == "downstream":
        return (True,)
    if direction == "upstream":
        return (False,)
    return (True, False)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _consumer_tables(name: str) -> list[str]:
    """Generated tables that would carry ``name``'s data, across the persisting provider kinds.

    One place, because the batched prefetch has to ask exactly the question the single-node read
    asks; a candidate present in one and not the other would silently change the answer.
    """
    tables: list[str] = []
    for kind in ("dso", "adso", "infocube"):
        tables.extend(candidate_tables(name, kind))
    return tables


def _owner_of_tables(chunk: list[tuple[str, list[str]]]) -> dict[str, str] | None:
    """Generated table -> the provider it belongs to, or ``None`` if any table has two claimants.

    Two providers claiming one generated table would make row attribution a guess. It is not
    expected on a well-formed system, and it is not assumed either.
    """
    owner_of: dict[str, str] = {}
    for name, candidates in chunk:
        for table in candidates:
            if owner_of.setdefault(table.strip().upper(), name) != name:
                return None
    return owner_of


def _table_chunks(
    eligible: list[tuple[str, list[str]]], size: int
) -> list[list[tuple[str, list[str]]]]:
    """Group ``(name, tables)`` pairs so no batch exceeds ``size`` bind parameters for tables.

    Chunked on tables rather than on names because each provider contributes several, so a
    name-based chunk would vary in width by provider kind. A single name whose candidate list
    already exceeds the bound still gets its own batch: the alternative is to drop it.
    """
    batches: list[list[tuple[str, list[str]]]] = []
    current: list[tuple[str, list[str]]] = []
    width = 0
    for pair in eligible:
        count = len(pair[1])
        if current and width + count > size:
            batches.append(current)
            current, width = [], 0
        current.append(pair)
        width += count
    if current:
        batches.append(current)
    return batches


_ROUTINE_CAVEAT = (
    "routine-derived edges are advisory (heuristic lower bound): dynamic SQL, function-module and "
    "class-method calls are not followed"
)

# What each bound means for the answer. A caller reading only the graph needs to know which part of
# it to distrust, not merely that something stopped.
_COMPLETENESS_CAVEATS: dict[LineageCompleteness, str] = {
    "complete": "",
    "semantic_limit": (
        f"at least one node reached the {_MAX_COMPOSITE_CONSUMERS}-consumer discovery cap, so this "
        "graph is a bounded reading: further CompositeProvider consumers may exist. The subset "
        "returned is deterministic (ordered), not sampled."
    ),
    "query_budget": (
        "the per-call statement allowance ran low, so consumer discovery stopped early and further "
        "relationships may exist. Narrow the request or raise SAPBW_MAX_QUERIES_PER_CALL."
    ),
    "time_budget": (
        "the per-call time allowance ran low, so consumer discovery stopped early and further "
        "relationships may exist. Narrow the request or raise SAPBW_MAX_SECONDS_PER_CALL."
    ),
    "node_limit": (
        f"expansion stopped at the {_MAX_NODES}-node cap, so objects beyond it are absent. Lower "
        "the depth for a complete reading of a smaller neighbourhood."
    ),
    "error_degraded": (
        "at least one expansion failed and this graph is what survived, so absence of an edge here "
        "is not evidence that it does not exist."
    ),
    "unsupported_branch": (
        "this release lacks the metadata one expansion branch needs, so that class of relationship "
        "is missing entirely rather than absent."
    ),
}


def _evidence_caveats(edges: list[LineageEdge]) -> list[str]:
    """State how much of the graph rests on inference, in the graph's own terms.

    A graph of 200 edges is a different object depending on whether 2 or 150 of them come from a
    routine parse. Both cases already carried the standing routine caveat, which says the class of
    risk but not its extent - and extent is what decides whether the shape can be trusted.
    """
    summary = summarise([e.evidence for e in edges if e.evidence])
    if not summary.total or not summary.advisory_count:
        return []
    share = round(100 * summary.advisory_count / summary.total)
    return [
        f"{summary.advisory_count} of {summary.total} edges ({share}%) are inferred rather than "
        "declared, so that share of this shape should be confirmed before it is acted on. Each "
        "edge's `evidence` says which mechanism produced it and why. Mechanisms present: "
        + ", ".join(summary.methods)
    ]


def _drain_level(queue: deque[tuple[str, int]], visited: set[str]) -> tuple[int, list[str]]:
    """Take every unvisited entry of the queue's lowest level, as ``(level, names)``.

    A whole level is drained before any of it is expanded, so the level's declared edges can be read
    in a few batched statements instead of about five per node. The queue is non-decreasing in level
    - expanding level N only ever appends level N or N+1 - so every entry of the lowest level is
    contiguous at the front. Expansion order within the level, and therefore edge order, is
    identical to popping one at a time.
    """
    level = queue[0][1]
    frontier: list[str] = []
    queued: set[str] = set()
    while queue and queue[0][1] == level:
        candidate, _ = queue.popleft()
        if candidate in visited or candidate in queued:
            continue
        queued.add(candidate)
        frontier.append(candidate)
    return level, frontier


class _Hop:
    """One resolved neighbour: the other object's name/type and the edge connecting it.

    ``free`` marks a hop that does **not** consume a depth level. ``depth`` is a count of *load
    layers* - how many times data is moved and reshaped between the root and here - and two hops in
    this graph cross a boundary without any load happening:

    * a BEx query resolving to the InfoProvider it reads (a reporting-layer hop), and
    * a DataSource resolving to the extract structure the source system fills (the boundary hop).

    Charging them a level made the same ``depth`` mean different things depending on what the caller
    named. From a query root, ``depth=3`` reached 12 of 65 DataSources where the same request on
    its provider reached far more, purely because a level went on establishing which provider the
    query reads - and the extract structure was then reached only for whichever DataSources happened
    to have a level left over, so one graph showed the boundary for some and not for others.

    Deliberately *not* applied to the downstream provider -> query fan-out. That hop is also a
    reporting-layer hop, but one provider declares up to 250 queries, so making it free would
    multiply a downstream walk's node count against a fixed node cap - trading a consistent depth
    for a truncated graph. The asymmetry is a bound, not a principle.
    """

    __slots__ = ("edge", "free", "name", "node_type")

    def __init__(
        self, name: str, node_type: LineageNodeType, edge: LineageEdge, *, free: bool = False
    ) -> None:
        self.name = name
        self.node_type = node_type
        self.edge = edge
        self.free = free


class LineageService(Repository):
    """Directed lineage graph, impact analysis, and trace-to-source over transformations + DTPs."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._transformations = TransformationsRepository(connection, capability, cache)
        self._providers = ProvidersRepository(connection, capability, cache)
        # Built lazily in _query_repo(): QueriesRepository imports this module, so importing it
        # here would be circular. Typed Any because the concrete class cannot be named yet.
        self._queries: Any = None
        # Per-instance memos over read-only metadata; see _expand for why these matter.
        # The memo carries each expansion's incompleteness reasons alongside its hops, so a memo hit
        # cannot silently drop the fact that the cached expansion was itself bounded.
        self._expand_memo: dict[
            tuple[str, LineageDirection, bool], tuple[list[_Hop], frozenset[LineageCompleteness]]
        ] = {}
        #: Reasons discovery was bounded during the walk in progress. Reset per BFS.
        self._walk_incomplete: set[LineageCompleteness] = set()
        self._node_type_memo: dict[str, LineageNodeType] = {}
        self._consumers_memo: dict[str, list[tuple[str, LineageNodeType, str]]] = {}
        self._trace_memo: dict[tuple[str, int], TraceToSource] = {}
        # Declared-edge rows prefetched a BFS level at a time, keyed by (name, downstream) and
        # holding (other_name, other_type_code, tran_id_or_updmode). A present key with an empty
        # list means "read, no rows"; an absent key means "not read", and the reader then issues
        # its own single-node query. That distinction is what makes the prefetch optional.
        self._pf_tran: dict[tuple[str, bool], list[tuple[str, str, str]]] = {}
        self._pf_dtp: dict[tuple[str, bool], list[tuple[str, str, str]]] = {}
        #: name -> (provider, compuid) or None. ``None`` is a cached answer, not a miss, so the
        #: `in` test rather than `.get` is what makes "this is not a query" cost one lookup.
        self._query_provider_memo: dict[str, tuple[str, str] | None] = {}
        #: provider -> [(query technical name, COMPUID)] from RSZCOMPIC, prefetched per BFS level.
        self._pf_queries: dict[str, list[tuple[str, str]]] = {}
        #: provider -> dependent generated view names from OBJECT_DEPENDENCIES, per BFS level. Only
        #: ever set from a batch that read its nodes to exhaustion, so a present entry is complete.
        self._pf_consumers: dict[str, list[str]] = {}
        #: (datasource, logsys) -> (extract structure, type, delta) or None. ``None`` is a cached
        #: answer - "RSDS has no row for this" - not a miss, so a flat-file source costs one read.
        self._extract_memo: dict[tuple[str, str], tuple[str, str, str] | None] = {}
        #: datasource -> customer-namespace field count, or None when it could not be established.
        self._enhancement_memo: dict[str, int | None] = {}
        #: calc view -> the BW objects under it. Per-instance rather than on disk: it is one
        #: statement plus a catalogue lookup, and a stale entry would outlive a re-activated view.
        self._calc_base_memo: dict[str, list[Any]] = {}

    # --- public API ----------------------------------------------------------------------

    def get_lineage(
        self, name: str, *, direction: LineageDirection = "both", depth: int = 3
    ) -> LineageGraph | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        depth = max(1, min(depth, _MAX_DEPTH))
        nodes, edges, completeness = self._bfs(name, direction, depth, include_routine=True)
        return self._graph(name, direction, depth, nodes, edges, completeness=completeness)

    def trace_to_source(self, name: str, *, depth: int = 8) -> TraceToSource | UnsupportedResult:
        """Walk upstream to the DataSource boundary. Memoised, like :meth:`_expand`.

        Query field lineage calls this once per InfoObject in the query, and the same InfoObjects
        recur across hundreds of queries, so without a memo a full-system generation re-walks the
        same upstream graphs repeatedly.
        """
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        depth = max(1, min(depth, _MAX_DEPTH))
        cached = self._trace_memo.get((name, depth))
        if cached is not None:
            return cached
        traced = self._trace_uncached(name, depth)
        self._trace_memo[(name, depth)] = traced
        return traced

    def _trace_uncached(self, name: str, depth: int) -> TraceToSource:
        nodes, edges, completeness = self._bfs(name, "upstream", depth, include_routine=True)
        graph = self._graph(name, "upstream", depth, nodes, edges, completeness=completeness)
        boundaries = [n for n in nodes.values() if n.object_type == "datasource"]
        datasources = [n.name for n in boundaries]
        # A DataSource whose extract structure was resolved is no longer an unresolved boundary. The
        # list used to be every DataSource unconditionally, on the assumption that a BW-only build
        # can never see past one - true until the source-extract hop, and now simply wrong for the
        # ones it resolves. Reporting a resolved parent *and* calling the node unresolved leaves a
        # caller no way to tell which boundaries are actually still open.
        unresolved = [n.name for n in boundaries if not n.upstream_resolved]
        resolved = len(boundaries) - len(unresolved)
        caveats = [_ROUTINE_CAVEAT]
        if completeness != "complete":
            caveats.append(_COMPLETENESS_CAVEATS[completeness])
        if not datasources:
            caveats.append("no DataSource boundary reached within the depth limit")
        elif resolved:
            caveats.append(
                f"{resolved} of {len(boundaries)} DataSource boundaries resolved one hop further, "
                "to the extract structure the source system fills (RSDS.EXSTRUCTURE), reported as "
                "source_object nodes. The extractor's own ABAP is in the source system, not in BW: "
                "an edge whose note reports customer-namespace fields is evidence that an "
                "enhancement exists, and bw_get_extractor_exit_code reads the code itself when an "
                "ecc_systems profile is configured."
            )
        return TraceToSource(
            root_id=name,
            graph=graph,
            datasources_reached=sorted(datasources),
            unresolved_boundaries=sorted(unresolved),
            caveats=caveats,
        )

    def impact_analysis(self, name: str, *, depth: int = 3) -> ImpactAnalysis | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        depth = max(1, min(depth, _MAX_DEPTH))
        nodes, edges, completeness = self._bfs(name, "downstream", depth, include_routine=False)

        # Reverse routine detection: objects whose routines READ the root (invisible to where-used).
        consumers = self._routine_consumers_of(name)
        for consumer_name, consumer_type, tran_id in consumers:
            if consumer_name == name:
                continue
            self._ensure_node(nodes, name, "unknown", self.provenance("transformation", {}))
            self._ensure_node(
                nodes,
                consumer_name,
                consumer_type,
                self.provenance("transformation", {"TRANID": tran_id}),
            )
            edges.append(
                LineageEdge(
                    src=name,
                    dst=consumer_name,
                    kind="routine_lookup",
                    derivation="routine",
                    confidence="advisory",
                    transformation_id=tran_id,
                    note="root is read by this object's inbound routine (invisible to where-used)",
                    provenance=self.provenance("transformation", {"TRANID": tran_id}),
                )
            )

        graph = self._graph(name, "downstream", depth, nodes, edges, completeness=completeness)
        affected = [n for n in nodes.values() if n.name != name]
        by_type: dict[str, int] = {}
        for node in affected:
            by_type[node.object_type] = by_type.get(node.object_type, 0) + 1
        caveats = [_ROUTINE_CAVEAT]
        if completeness != "complete":
            caveats.append(_COMPLETENESS_CAVEATS[completeness])
        return ImpactAnalysis(
            root_id=name,
            graph=graph,
            affected_object_count=len(affected),
            affected_by_type=dict(sorted(by_type.items())),
            routine_lookup_consumers=sorted({c[0] for c in consumers if c[0] != name}),
            caveats=caveats,
        )

    # --- BFS -----------------------------------------------------------------------------

    def _bfs(
        self, root: str, direction: LineageDirection, depth: int, *, include_routine: bool
    ) -> tuple[dict[str, LineageNode], list[LineageEdge], LineageCompleteness]:
        nodes: dict[str, LineageNode] = {}
        edges: list[LineageEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()
        self._ensure_node(
            nodes, root, self._node_type_of(root), self.provenance("transformation", {})
        )
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        # Bounds hit by individual expansions during *this* walk, not a previous one.
        self._walk_incomplete = set()
        hit_node_cap = False

        while queue:
            level, frontier = _drain_level(queue, visited)
            at_limit = level >= depth
            if at_limit and direction == "downstream":
                break  # no free hop exists downstream, so there is nothing left to add
            if not at_limit:
                self._prefetch_frontier(frontier, direction, include_routine=include_routine)
            for current in frontier:
                visited.add(current)
                # At the limit the node is not expanded - only its free hops are taken, and only
                # the branch that can produce one is asked. Without this a DataSource reached
                # *exactly* at the limit kept its extractor hidden, so one graph showed the source
                # boundary for some of its DataSources and not for others: whether the annotation
                # appeared depended on how many layers happened to sit above each one.
                expansion = (
                    self._boundary_expand(current, direction)
                    if at_limit
                    else self._expand(current, direction, include_routine=include_routine)
                )
                for hop in expansion:
                    self._ensure_node(nodes, hop.name, hop.node_type, hop.edge.provenance)
                    # Remember what the hop taught us about the neighbour's type, before that
                    # neighbour is expanded. Its own expansion then knows what it is without asking
                    # the database - which is what lets the DataSource branch fire on a DataSource
                    # whose name carries no logical-system suffix, rather than only on one whose
                    # name happens to look like an endpoint.
                    if hop.node_type != "unknown":
                        self._node_type_memo.setdefault(hop.name, hop.node_type)
                    if hop.edge.kind == "source_extract":
                        self._resolve_boundary(nodes, hop)
                    key = (hop.edge.src, hop.edge.dst, hop.edge.kind)
                    if key not in edge_keys:
                        edge_keys.add(key)
                        edges.append(hop.edge)
                    if len(nodes) >= _MAX_NODES:
                        hit_node_cap = True
                        break
                    if hop.name not in visited:
                        # A free hop re-enters the *same* level, so it is expanded in a further pass
                        # over this level rather than costing one. Termination is unaffected: the
                        # visited set still grows by one per expansion, and every free hop's own
                        # neighbours are charged normally.
                        queue.append((hop.name, level if hop.free else level + 1))
                if hit_node_cap:
                    break
            if hit_node_cap:
                break
        return nodes, edges, self._completeness(node_cap=hit_node_cap)

    # --- frontier prefetch ---------------------------------------------------------------

    def _prefetch_frontier(
        self, frontier: list[str], direction: LineageDirection, *, include_routine: bool
    ) -> None:
        """Fill the declared-edge caches for one BFS level in a few batched statements.

        Expanding a node costs ~5 single-row statements against RSTRAN and RSBKDTP, and a deep
        walk's cost is dominated by per-statement round-trip latency rather than by the work each
        statement does: a depth-3 both-directions walk re-run warm dropped 21% of its statements
        but only 14% of its wall time. Reading a level's worth of the same rows through one IN-list
        per branch collapses the count without changing which rows are read.

        Deliberately best-effort. Every reader falls back to its own single-node query when a name
        is absent from the cache, so a prefetch that is skipped, chunked short, or declined for
        lack of budget costs speed and never correctness - a bug here degrades to today's
        behaviour rather than producing a different graph.
        """
        # Nodes whose expansion is already memoised issue no statements at all, so prefetching
        # them would be pure cost.
        pending = [
            name for name in frontier if (name, direction, include_routine) not in self._expand_memo
        ]
        if len(pending) < _PREFETCH_MIN_FRONTIER:
            return
        # Checked, not caught: with the allowance nearly spent, the expansions themselves need
        # what is left more than the prefetch does, and they fall back cleanly.
        if self._budget_stop() is not None:
            return
        for downstream in _prefetch_directions(direction):
            self._prefetch_transformations(
                [n for n in pending if (n, downstream) not in self._pf_tran], downstream=downstream
            )
            if self.capability.is_available("dtp"):
                self._prefetch_dtps(
                    [n for n in pending if (n, downstream) not in self._pf_dtp],
                    downstream=downstream,
                )
        if direction in ("downstream", "both") and self.capability.is_available("query_provider"):
            self._prefetch_declared_queries([n for n in pending if n not in self._pf_queries])
        if (
            direction in ("downstream", "both")
            and self.capability.is_available("object_dependencies")
            and self.capability.is_available("composite_header")
        ):
            self._prefetch_consumers([n for n in pending if n not in self._pf_consumers])

    def _prefetch_transformations(self, names: list[str], *, downstream: bool) -> None:
        key_col, other_name_col, other_type_col = _TRAN_COLUMNS[downstream]
        for chunk in _chunks(names, _PREFETCH_CHUNK):
            owner_of = {name.strip().upper(): name for name in chunk}
            buckets: dict[str, list[tuple[str, str, str]]] = {name: [] for name in chunk}
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=[key_col, other_name_col, other_type_col, "TRANID"],
                    from_logical="transformation",
                    where=[f"{key_col} IN ({placeholders})"],
                    params=list(chunk),
                    order_by=[key_col, "TRANID"],
                )
            )
            attributed = True
            for key_value, other_name, other_type, tran_id in rows:
                owner = owner_of.get(str(key_value).strip().upper())
                if owner is None:
                    attributed = False
                    break
                buckets[owner].append(
                    (str(other_name).strip(), str(other_type).strip(), str(tran_id).strip())
                )
            if not attributed:
                # A returned key matching no requested name means the key-matching assumption is
                # wrong on this system. Caching these buckets would turn that into *missing edges*,
                # so the chunk is abandoned and every node in it falls back to its own read.
                continue
            for name, collected in buckets.items():
                self._pf_tran[(name, downstream)] = collected

    def _prefetch_dtps(self, names: list[str], *, downstream: bool) -> None:
        key_col, other_name_col, other_type_col = _DTP_COLUMNS[downstream]
        for chunk in _chunks(names, _PREFETCH_CHUNK):
            owner_of = {name.strip().upper(): name for name in chunk}
            buckets: dict[str, list[tuple[str, str, str]]] = {name: [] for name in chunk}
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=[key_col, other_name_col, other_type_col, "UPDMODE"],
                    from_logical="dtp",
                    # RSBK* prefix: no OBJVERS auto-inject, so it is stated here as in the
                    # single-node read.
                    where=[f"{key_col} IN ({placeholders})", "OBJVERS = 'A'"],
                    params=list(chunk),
                    order_by=[key_col, other_name_col],
                )
            )
            attributed = True
            for key_value, other_name, other_type, updmode in rows:
                owner = owner_of.get(str(key_value).strip().upper())
                if owner is None:
                    attributed = False
                    break
                buckets[owner].append(
                    (str(other_name).strip(), str(other_type).strip(), str(updmode).strip())
                )
            if not attributed:
                continue  # abandon the chunk rather than cache it - see _prefetch_transformations
            for name, collected in buckets.items():
                self._pf_dtp[(name, downstream)] = collected

    def _completeness(self, *, node_cap: bool) -> LineageCompleteness:
        """The single most limiting bound this walk hit, or ``complete``."""
        reasons = set(self._walk_incomplete)
        if node_cap:
            reasons.add("node_limit")
        for candidate in _COMPLETENESS_PRECEDENCE:
            if candidate in reasons:
                return candidate
        return "complete"

    def _expand(
        self, name: str, direction: LineageDirection, *, include_routine: bool
    ) -> list[_Hop]:
        """Every hop out of one node. Memoised: this is where the walk's cost lives.

        Expanding a node means several metadata queries plus, upstream, analysing the routines of
        every transformation targeting it — measured at seconds per node. A single walk never
        revisits a node, but consecutive walks overlap heavily (a DSO stack's middle layers appear
        in every graph through them), so without a memo a full-system generation re-derives the
        same nodes hundreds of times. Keyed by the arguments that change the answer.
        """
        memo_key = (name, direction, include_routine)
        cached = self._expand_memo.get(memo_key)
        if cached is not None:
            hops, reasons = cached
            # Replay the cached expansion's bounds: this walk is no more complete than the read
            # that produced the hops it is reusing.
            self._walk_incomplete.update(reasons)
            return hops
        before = frozenset(self._walk_incomplete)
        hops = self._expand_uncached(name, direction, include_routine=include_routine)
        self._expand_memo[memo_key] = (hops, frozenset(self._walk_incomplete) - before)
        return hops

    def _foreign_expand(self, name: str, direction: LineageDirection) -> list[_Hop] | None:
        """Expansion for a node that is not a BW object, or ``None`` when the node is one.

        The BW branches ask BW questions - is this a transformation endpoint, does a query read it,
        does a generated view depend on its table - and two node types in this graph cannot answer
        any of them:

        ``source_object``
            an extract structure in the source system's ABAP dictionary. It is terminal here by
            construction: what fills it is source-system code, which is the gap the
            ``source_extract`` edge names rather than a hop this server can take.
        ``calcview``
            a ``_SYS_BIC`` runtime name, not a BW object name, so every BW branch matches nothing.
            Its one meaningful upstream hop is the objects it reads; downstream, the provider that
            consumes it is already the ``composite_part`` edge that brought the walk here.

        Asking anyway was not merely wasteful, it was measurable: the boundary hops added 33 such
        nodes to a production depth-3 walk and each was expanded like a provider, which is around
        200 statements spent to conclude nothing. This is also why they cannot simply be left out of
        the graph - they are real objects and the answer needs them; it is their *expansion* that is
        meaningless.
        """
        node_type = self._node_type_memo.get(name)
        if node_type == "source_object":
            return []
        if node_type == "calcview":
            return self._calc_view_base_hops(name) if direction != "downstream" else []
        return None

    def _boundary_expand(self, name: str, direction: LineageDirection) -> list[_Hop]:
        """Only the hops that do not consume a depth level, for a node sitting at the limit.

        Deliberately narrow rather than "expand and filter". A full expansion costs several
        statements per node plus a routine parse, and at the limit every one of those results would
        be discarded - so on a wide frontier filtering would pay the whole cost of the walk again to
        add nothing. Asking only the branch that can produce a free hop keeps the limit cheap.

        Not written to ``_expand_memo``: that memo's key does not record *which* expansion produced
        its hops, so caching a partial answer under it would let a later full walk read this narrow
        result as complete.
        """
        if direction == "downstream":
            return []
        if not self._is_datasource(name):
            return []
        return self._source_extract_hops(name)

    def _expand_uncached(
        self, name: str, direction: LineageDirection, *, include_routine: bool
    ) -> list[_Hop]:
        foreign = self._foreign_expand(name, direction)
        if foreign is not None:
            return foreign
        hops: list[_Hop] = []
        if direction in ("downstream", "both"):
            hops.extend(self._declared_hops(name, downstream=True))
            # Before the calc-view branch deliberately: both can produce the same provider->query
            # edge, the walk keeps the first of a duplicate, and this one is declared metadata where
            # the other is a naming convention. The order decides which evidence a caller sees.
            hops.extend(self._declared_query_hops(name))
            hops.extend(self._composite_consumer_hops(name))
            hops.extend(self._query_provider_hops(name, downstream=True))
        if direction in ("upstream", "both"):
            hops.extend(self._declared_hops(name, downstream=False))
            hops.extend(self._composite_part_hops(name))
            if include_routine:
                hops.extend(self._routine_lookup_hops(name))
            hops.extend(self._query_provider_hops(name, downstream=False))
            # Last, and type-gated: only a DataSource has one, and this is the hop that takes the
            # walk past what BW itself treats as the edge of the warehouse.
            if self._is_datasource(name):
                hops.extend(self._source_extract_hops(name))
        return hops

    def _is_datasource(self, name: str) -> bool:
        """Whether a node is a DataSource, without paying for a type lookup on every node.

        Answered from the type the walk already knows: the hop that discovered the node recorded it,
        and in a breadth-first walk that always happens before the node is expanded. Only a *root*
        arrives untyped, and for that the endpoint's shape decides whether the lookup is worth a
        statement - deliberately not the answer, since a plainly named DataSource is legitimate and
        is covered by the memo above.
        """
        cached = self._node_type_memo.get(name)
        if cached is not None:
            return cached == "datasource"
        if " " not in name.strip():
            return False
        return self._node_type_of(name) == "datasource"

    def _node_type_of(self, name: str) -> LineageNodeType:
        """The root's own object type, so the graph's centre is never labelled 'unknown'.

        Neighbour types come free with each hop (RSTRAN carries the *other* endpoint's RSTLOGO
        code), but the root has no inbound hop to learn from. One bounded lookup asks RSTRAN for a
        row where the object is an endpoint and reads its own type code from that side. Memoised
        because a full-system generation asks for the same objects repeatedly.
        """
        cached = self._node_type_memo.get(name)
        if cached is not None:
            return cached
        resolved = self._node_type_uncached(name)
        self._node_type_memo[name] = resolved
        return resolved

    def _node_type_uncached(self, name: str) -> LineageNodeType:
        for own_type_col, key_col in (("SOURCETYPE", "SOURCENAME"), ("TARGETTYPE", "TARGETNAME")):
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[own_type_col],
                        from_logical="transformation",
                        where=[f"{key_col} = ?"],
                        params=[name],
                        # An object is an endpoint of many transformations; LIMIT 1 without an order
                        # reads its type off an arbitrary row (D8).
                        order_by=[own_type_col],
                    ),
                    limit=1,
                )
            )
            if rows:
                resolved = _node_type(str(rows[0][0]).strip())
                if resolved != "unknown":
                    return resolved
        # A CompositeProvider is often an endpoint of nothing (it has no transformations at all).
        if self.capability.is_available("composite_header"):
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["HCPRNM"],
                        from_logical="composite_header",
                        where=["HCPRNM = ?"],
                        params=[name],
                        order_by=["HCPRNM"],  # capped at one row; see above
                    ),
                    limit=1,
                )
            )
            if rows:
                return "compositeprovider"
        if self.capability.is_available("query_dir"):
            header = self._query_repo()._header(name)
            if header is not None:
                return "query"
        # Last: ask the provider catalogue. An InfoCube that no transformation touches - an isolated
        # or fully retired one - was reaching the caller as 'unknown' even though RSDCUBE says
        # exactly what it is. Found by comparing the canonical id from bw_describe_object against
        # the one from bw_get_lineage for the same object: they disagreed.
        described = self._providers.describe(name)
        if isinstance(described, Provider):
            return _node_type(described.object_type)
        return "unknown"

    def _query_repo(self) -> Any:
        if self._queries is None:
            # Deliberately deferred: repositories.queries imports LineageService at module level,
            # so a top-level import here is a genuine cycle rather than a style slip.
            from ..repositories.queries import QueriesRepository  # noqa: PLC0415

            self._queries = QueriesRepository(self._connection, self.capability, self._cache)
        return self._queries

    def _query_provider_hops(self, name: str, *, downstream: bool) -> list[_Hop]:
        """Resolve a BEx query to its provider, then continue the normal lineage walk.

        The generic lineage service handles BW objects and transformations, but BEx queries are a
        separate metadata layer. If the supplied name is a query, attach a synthetic edge to its
        provider and let downstream/upstream expansion continue from there.
        """
        if not self.capability.is_available("query_dir"):
            return []
        resolved = self._query_provider_of(name)
        if resolved is None:
            return []
        provider, compuid = resolved
        edge = LineageEdge(
            src=name if downstream else provider,
            dst=provider if downstream else name,
            kind="query_provider",
            derivation="declared",
            confidence="exact",
            note="provider resolved from the BEx query metadata",
            provenance=self.provenance("query_provider", {"COMPUID": compuid}),
        )
        # Free: establishing which provider a query reads is not a load layer, so it must not spend
        # one. See _Hop for what charging it did to `depth` from a query root.
        #
        # The type is resolved rather than left ``unknown``. Every other node learns its type from
        # the hop that found it, because RSTRAN carries the *other* endpoint's type code - but a
        # query's provider is named by RSZCOMPIC, which carries no type, and it is the target of its
        # inbound transformations rather than their source, so no hop ever types it either. It
        # therefore stayed ``unknown``: on a production query's diagram the provider the whole graph
        # exists to explain was the one grey box labelled "object". One memoised lookup, and only
        # for a query root, since nothing else takes this branch.
        return [_Hop(provider, self._node_type_of(provider), edge, free=True)]

    def _query_provider_of(self, name: str) -> tuple[str, str] | None:
        """``(provider, compuid)`` if ``name`` is a BEx query, else ``None``. Memoised per name.

        A both-directions walk asks this once per direction for every node, and the two calls do
        identical work - only the resulting edge's orientation differs. Measured on a production
        depth-3 walk, this branch spent 48.3s over 456 statements, almost exactly two per node,
        which is the duplicate. The memo also covers the common case worth avoiding: most nodes are
        not queries at all, and each was paying the lookup twice to find that out.
        """
        if name in self._query_provider_memo:
            return self._query_provider_memo[name]
        resolved: tuple[str, str] | None = None
        query_repo = self._query_repo()
        header = query_repo._header(name)
        if header is not None:
            compuid = str(header[0])
            providers = query_repo._providers_list(compuid)
            if providers:
                resolved = (providers[0], compuid)
        self._query_provider_memo[name] = resolved
        return resolved

    # --- declared BEx query consumers (RSZCOMPIC) ----------------------------------------

    def _declared_query_hops(self, name: str) -> list[_Hop]:
        """BEx queries BW declares against ``name``, from RSZCOMPIC. Defect D12.

        This is the authoritative answer to "which reports read this provider", and it was missing
        entirely. Query consumers were discovered only by noticing that the calc view BW generates
        for a query depends on an object's active table - a real fact, but a different one. The two
        diverge exactly where it matters: measured on a production ADSO, the calc-view route found
        23 queries reading the object's own table and none of the 48 queries defined on the
        CompositeProvider above it, because nothing ever asked a CompositeProvider what reports it
        carries. The walk then reported ``completeness="complete"``.

        The root-element filter is a correctness requirement rather than tidiness: RSZCOMPIC carries
        a row per query *component*, so a structure or a calculated key figure is assigned to the
        provider too. Without ``DEFTP='REP'`` the widest measured CompositeProvider would contribute
        267 nodes for its 48 reports. When the filter cannot be built the branch reports itself
        unsupported instead of returning components as though they were reports.
        """
        if not (
            self.capability.is_available("query_provider")
            and self.capability.is_available("query_dir")
        ):
            self._walk_incomplete.add("unsupported_branch")
            return []
        if self._root_element_filter() is None:
            # element_dir absent: queries cannot be told apart from their components here.
            self._walk_incomplete.add("unsupported_branch")
            return []
        rows = self._declared_query_rows(name)
        if len(rows) > _MAX_DECLARED_QUERY_CONSUMERS:
            self._walk_incomplete.add("semantic_limit")
        hops: list[_Hop] = []
        for compid, compuid in rows[:_MAX_DECLARED_QUERY_CONSUMERS]:
            hops.append(
                _Hop(
                    compid,
                    "query",
                    LineageEdge(
                        src=name,
                        dst=compid,
                        kind="query_provider",
                        derivation="declared",
                        confidence="exact",
                        note="BEx query defined on this InfoProvider (declared in RSZCOMPIC)",
                        evidence=evidence_for("declared_query_provider", "rszcompic"),
                        provenance=self.provenance(
                            "query_provider", {"INFOCUBE": name, "COMPUID": compuid}
                        ),
                    ),
                )
            )
        return hops

    def _root_element_filter(self) -> str | None:
        """``COMPUID IN (<query roots>)``, or ``None`` when this release cannot express it."""
        return cast("str | None", self._query_repo()._query_only_filter())

    def _declared_query_rows(self, name: str) -> list[tuple[str, str]]:
        """``(compid, compuid)`` per declared query, prefetched for the level or read now."""
        cached = self._pf_queries.get(name)
        if cached is not None:
            return cached
        root_filter = self._root_element_filter()
        if root_filter is None:
            return []
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["COMPUID"],
                    from_logical="query_provider",
                    where=["INFOCUBE = ?", root_filter],
                    params=[name],
                    order_by=["COMPUID"],
                ),
                limit=_MAX_DECLARED_QUERY_CONSUMERS + 1,
            )
        )
        compuids = [str(r[0]).strip() for r in rows if str(r[0]).strip()]
        names_by_uid = self._compids_for(compuids)
        return [(names_by_uid[uid], uid) for uid in compuids if uid in names_by_uid]

    def _compids_for(self, compuids: list[str]) -> dict[str, str]:
        """COMPUID -> technical query name, so a graph node carries the name a user recognises."""
        resolved: dict[str, str] = {}
        for chunk in _chunks(sorted(set(compuids)), _PREFETCH_CHUNK):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=["COMPUID", "COMPID"],
                    from_logical="query_dir",
                    where=[f"COMPUID IN ({placeholders})"],
                    params=list(chunk),
                )
            )
            for compuid, compid in rows:
                uid, name = str(compuid).strip(), str(compid).strip()
                if uid and name:
                    resolved[uid] = name
        return resolved

    def _prefetch_declared_queries(self, names: list[str]) -> None:
        """One RSZCOMPIC read per BFS level instead of two statements per provider node."""
        root_filter = self._root_element_filter()
        if root_filter is None:
            return
        for chunk in _chunks(names, _PREFETCH_CHUNK):
            owner_of = {name.strip().upper(): name for name in chunk}
            buckets: dict[str, list[tuple[str, str]]] = {name: [] for name in chunk}
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=["INFOCUBE", "COMPUID"],
                    from_logical="query_provider",
                    where=[f"INFOCUBE IN ({placeholders})", root_filter],
                    params=list(chunk),
                    order_by=["INFOCUBE", "COMPUID"],
                )
            )
            names_by_uid = self._compids_for([str(r[1]).strip() for r in rows])
            attributed = True
            for infocube, compuid in rows:
                owner = owner_of.get(str(infocube).strip().upper())
                if owner is None:
                    attributed = False
                    break
                uid = str(compuid).strip()
                compid = names_by_uid.get(uid)
                if compid is not None:
                    buckets[owner].append((compid, uid))
            if not attributed:
                continue  # abandon the chunk - see _prefetch_transformations
            for name, collected in buckets.items():
                self._pf_queries[name] = collected

    # --- CompositeProvider part edges (via the generated HANA calc view) ------------------

    def _composite_part_hops(self, name: str) -> list[_Hop]:
        """If ``name`` is a CompositeProvider, the part providers that feed it.

        A CompositeProvider persists nothing and has no inbound transformation, so without this its
        upstream lineage is a dead end. Parts come from the model BW stores on ``RSOHCPR``, which
        declares them; where that cannot be read they fall back to the base tables of the generated
        calc view, which is a naming-convention reading and is labelled advisory.
        """
        if not self.capability.is_available("composite_header"):
            return []
        declared = self._declared_part_hops(name)
        if declared is not None:
            return declared
        parts = self._providers.composite_parts(name)[0]
        root_key = name.strip().upper()
        hops: list[_Hop] = []
        for part in parts:
            # A CompositeProvider is not its own part: its generated calc view reads its own tables,
            # so this row is a resolver artefact. Scoped to composite_part deliberately - a declared
            # transformation whose source and target are one object is a real BW modelling choice
            # and must stay represented.
            if part.name.strip().upper() == root_key:
                continue
            hops.append(
                _Hop(
                    part.name,
                    _node_type(part.part_type),
                    LineageEdge(
                        src=part.name,
                        dst=name,
                        kind="composite_part",
                        derivation="declared",
                        confidence="exact" if part.confidence == "confirmed" else "advisory",
                        note=(
                            "part provider resolved from the generated calc view's base table "
                            f"{part.via_table}"
                            if part.via_table
                            else None
                        ),
                        # The part_provider vocabulary, because that is how this fact was obtained:
                        # a generated table name resolved by convention, confirmed against the
                        # provider catalogue or not. Left to the default, a confirmed part claimed a
                        # metadata row "states the source and target directly", and an unconfirmed
                        # one claimed it came from parsing ABAP. Neither happened (D10).
                        evidence=evidence_for("part_provider", part.confidence),
                        provenance=part.provenance,
                    ),
                )
            )
        return hops

    # --- a calculation view's own base objects --------------------------------------------

    def _calc_view_base_hops(self, name: str) -> list[_Hop]:
        """The BW objects behind a calculation view the walk has reached.

        Needed because reading the CompositeProvider's *declared* model made the graph shorter as
        well as more accurate. 44 of 108 CompositeProviders on the measured system declare a
        calc-view input, and the model names the view - correctly, that is what BW declares - where
        the previous route named the ``/BIC/`` tables the view reads and resolved those back to
        providers. So the declared route gained 81 objects the old one never saw and lost 87 that
        sat *under* a view, and neither route alone is the answer.

        Both are kept, distinguished by mechanism rather than blended: the CompositeProvider -> view
        edge is declared metadata, and the view -> BW object edge below it is a ``/BIC/`` name
        resolved by convention, so it is advisory and says so. Blending them would let a convention
        reading inherit the declaration's confidence.
        """
        if not (
            self.capability.is_available("object_dependencies")
            and self.capability.is_available("hana_views")
        ):
            self._walk_incomplete.add("unsupported_branch")
            return []
        parts: list[Any] | None = self._calc_base_memo.get(name)
        if parts is None:
            parts = self._providers.providers_under_calc_view(name)
            self._calc_base_memo[name] = parts
        hops: list[_Hop] = []
        for part in parts:
            if part.name == name:
                continue
            hops.append(
                _Hop(
                    part.name,
                    _node_type(part.part_type),
                    LineageEdge(
                        src=part.name,
                        dst=name,
                        kind="calcview_base",
                        derivation="declared",
                        # The *dependency* is declared in the HANA catalogue; the BW object behind
                        # the generated table is not recorded anywhere, so the reading is advisory.
                        confidence="advisory",
                        note=(
                            f"the calculation view reads generated table {part.via_table}, whose "
                            "name resolves to this BW object by convention"
                        ),
                        evidence=evidence_for("table_resolution", part.confidence),
                        provenance=part.provenance,
                    ),
                )
            )
        return hops

    # --- the source-system boundary (DataSource -> its extractor) --------------------------

    def _source_extract_hops(self, name: str) -> list[_Hop]:
        """The extract structure behind a DataSource - one hop past what BW usually calls the edge.

        The DataSource was a hard stop: ``source_extract`` and ``source_object`` were defined in the
        model and never produced, so an upstream walk ended saying "this came from a DataSource" and
        left the obvious next question - *extracted by what?* - unanswered.

        The answer is on the BW side. ``RSDS.EXSTRUCTURE`` names the extract structure the source
        system fills, per DataSource and logical system, and it is populated for essentially every
        DataSource: measured, 53 of 54 reached from one production CompositeProvider. ``ROOSOURCE``
        looks like the better source and is not - it holds only the *replicated* extractor
        definitions, which on the same measurement covered 1 of those 54, because an ECC
        DataSource's definition normally stays in ECC. So RSDS is the route, not ROOSOURCE.

        The node is the extract structure, not a guess at the exit. What the extractor's ABAP *does*
        lives in the source system and needs the ECC connector; where customer-namespace fields
        prove an enhancement exists, the edge says so and names the tool that reads it. That keeps
        the boundary honest: the structure is declared metadata, the logic is a stated gap.
        """
        if not self.capability.is_available("datasource"):
            return []
        datasource, logsys = split_datasource_endpoint(name)
        if not datasource:
            return []
        detail = self._datasource_extract(datasource, logsys)
        if detail is None:
            return []
        structure, ds_type, delta = detail
        if not structure:
            # No extract structure recorded. Reported by its absence rather than by a synthesized
            # node: an invented extractor name is worse than an unresolved boundary.
            return []
        enhanced = self._extract_enhancement_count(datasource)
        note = self._extract_note(datasource, logsys, ds_type, delta, enhanced)
        return [
            _Hop(
                structure,
                "source_object",
                LineageEdge(
                    src=structure,
                    dst=name,
                    kind="source_extract",
                    derivation="declared",
                    confidence="exact",
                    note=note,
                    provenance=self.provenance(
                        "datasource",
                        {"DATASOURCE": datasource, "LOGSYS": logsys or "", "OBJVERS": "A"},
                    ),
                ),
                # Free: crossing the warehouse boundary is not a load layer. Charged, the extractor
                # appeared only for whichever DataSources had a level left, so one graph showed the
                # boundary for some of its DataSources and not for others.
                free=True,
            )
        ]

    def _datasource_extract(
        self, datasource: str, logsys: str | None
    ) -> tuple[str, str, str] | None:
        """``(extract structure, DataSource type, delta method)`` from RSDS. Memoised per name.

        Keyed on the DataSource alone rather than on the pair: the same DataSource replicated from
        two logical systems fills the same extract structure, and keying on the pair would re-read
        for each. Where a system was named it is still used to pick the row, so the answer is the
        one for that endpoint.
        """
        cache_key = (datasource, logsys or "")
        if cache_key in self._extract_memo:
            return self._extract_memo[cache_key]
        where = ["DATASOURCE = ?"]
        params: list[Any] = [datasource]
        if logsys:
            where.append("LOGSYS = ?")
            params.append(logsys)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["EXSTRUCTURE", "TYPE", "DELTA"],
                        from_logical="datasource",
                        where=where,
                        params=params,
                        # Several rows can match when no logical system was given; ordered so the
                        # chosen one is reproducible rather than whichever the database returned.
                        order_by=["EXSTRUCTURE", "TYPE"],
                    ),
                    limit=1,
                )
            )
        except Exception:
            # Column names on RSDS vary across releases. Degrade to "no extractor resolved" rather
            # than failing the whole walk over one branch.
            self._walk_incomplete.add("unsupported_branch")
            self._extract_memo[cache_key] = None
            return None
        resolved: tuple[str, str, str] | None = None
        if rows:
            resolved = (
                str(rows[0][0] or "").strip(),
                str(rows[0][1] or "").strip(),
                str(rows[0][2] or "").strip(),
            )
        self._extract_memo[cache_key] = resolved
        return resolved

    def _extract_enhancement_count(self, datasource: str) -> int | None:
        """Customer-namespace fields on the extract structure: that an enhancement exists.

        ``None`` when it could not be established, which is not the same as zero. The count is
        metadata-confirmed evidence; what the enhancement *does* is source-system ABAP.
        """
        if not self.capability.is_available("datasource_field"):
            return None
        if datasource in self._enhancement_memo:
            return self._enhancement_memo[datasource]
        try:
            rows = self.select(
                self.dialect.build_select(
                    columns=["COUNT(*)"],
                    from_logical="datasource_field",
                    where=[
                        "DATASOURCE = ?",
                        "OBJVERS = 'A'",
                        "(FIELDNM LIKE 'Z%' OR FIELDNM LIKE 'Y%' OR FIELDNM LIKE '/BIC/%')",
                    ],
                    params=[datasource],
                )
            )
        except Exception:
            self._enhancement_memo[datasource] = None
            return None
        count = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        self._enhancement_memo[datasource] = count
        return count

    @staticmethod
    def _extract_note(
        datasource: str, logsys: str | None, ds_type: str, delta: str, enhanced: int | None
    ) -> str:
        """State what was read, and name the gap where the logic itself is out of reach."""
        parts = [f"extract structure filled by the source system for DataSource {datasource}"]
        if logsys:
            parts.append(f"in logical system {logsys}")
        if ds_type:
            parts.append(f"DataSource type {ds_type}")
        if delta:
            parts.append(f"delta method {delta}")
        head = ", ".join(parts) + "."
        if enhanced:
            return (
                f"{head} The structure carries {enhanced} customer-namespace field(s), which is "
                "metadata-confirmed evidence that the extractor was enhanced. What the enhancement "
                "code does is ABAP in the source system, not in BW: bw_get_extractor_exit_code "
                "reads it when an ecc_systems profile is configured."
            )
        if enhanced == 0:
            return (
                f"{head} No customer-namespace fields on the structure, so BW holds no evidence of "
                "an extractor enhancement. That is evidence of absence for *appended fields* only: "
                "an exit that changes existing values leaves no trace in BW metadata."
            )
        return (
            f"{head} Whether the extractor is enhanced could not be established here, so treat it "
            "as unknown rather than as unenhanced."
        )

    def _declared_part_hops(self, name: str) -> list[_Hop] | None:
        """Part hops from the stored model, or ``None`` when it could not be read.

        ``None`` rather than an empty list, because the two mean opposite things: no model is a
        reason to fall back to the calc-view route, while a model naming no part is an answer.

        A calc-view part is emitted under its ``_SYS_BIC`` runtime name, not the model's own form.
        That is the name the HANA catalogue holds, and it is what lets the walk continue through the
        view to the objects beneath it - joined on the model's form it would find nothing and the
        graph would stop at the view.
        """
        model = self._providers.composite_model(name)
        if model is None or not model.parsed or not model.inputs:
            return None
        root_key = name.strip().upper()
        hops: list[_Hop] = []
        seen: set[str] = set()
        for part in model.part_inputs:
            part_name = (part.runtime_view_name or part.part_name).strip()
            # A CompositeProvider is not its own part; see the calc-view route below for why that
            # exclusion is scoped to this edge kind rather than applied to declared transformations.
            if not part_name or part_name.upper() == root_key or part_name in seen:
                continue
            seen.add(part_name)
            hops.append(
                _Hop(
                    part_name,
                    _node_type(part.part_kind),
                    LineageEdge(
                        src=part_name,
                        dst=name,
                        kind="composite_part",
                        derivation="declared",
                        confidence="exact",
                        note=(
                            f"declared as input {part.alias!r} of the CompositeProvider's stored "
                            f"model ({part.entity_ref})"
                        ),
                        evidence=evidence_for("composite_part", "declared_model"),
                        provenance=self.provenance(
                            "composite_header", {"HCPRNM": name, "OBJVERS": "A"}
                        ),
                    ),
                )
            )
        return hops

    def _composite_consumer_hops(self, name: str) -> list[_Hop]:
        """CompositeProviders that consume ``name`` as a part provider (the reverse direction).

        Rows are pruned to provider-view candidates *in SQL*, read in a deterministic order, and
        paged until the source is exhausted or a stated bound stops discovery. The cap applies to
        distinct resolved providers, never to raw rows - see ``_MAX_COMPOSITE_CONSUMERS`` for why
        that distinction was a correctness defect rather than a tuning choice.

        Both SQL predicates are authoritative rather than heuristic: a dependent outside the
        generated package, and a hierarchy runtime view inside it, are both rejected by
        ``provider_from_calc_view`` regardless, so excluding them in the database changes only how
        many rows cross the wire.
        """
        if not (
            self.capability.is_available("object_dependencies")
            and self.capability.is_available("composite_header")
        ):
            return []
        tables = _consumer_tables(name)
        if not tables:
            return []

        root_key = name.strip().upper()
        cached = self._pf_consumers.get(name)
        if cached is not None:
            return self._consumer_hops_from(name, cached, root_key=root_key)

        base_query = self.dialect.build_select(
            columns=["DISTINCT DEPENDENT_OBJECT_NAME"],
            from_logical="object_dependencies",
            where=self._consumer_where(tables),
            params=self._consumer_params(tables),
            order_by=["DEPENDENT_OBJECT_NAME"],
        )

        hops: list[_Hop] = []
        # Keyed by (type, name): a provider and a query may legitimately share a name.
        seen: set[tuple[str, str]] = set()
        for page in range(_MAX_CONSUMER_PAGES):
            stopped = self._budget_stop()
            if stopped is not None:
                self._walk_incomplete.add(stopped)
                break
            rows = self.select(
                self.dialect.paginate(
                    base_query, limit=_CONSUMER_PAGE_ROWS, offset=page * _CONSUMER_PAGE_ROWS
                )
            )
            capped = self._absorb_consumers(
                name, [str(r[0]).strip() for r in rows], root_key=root_key, hops=hops, seen=seen
            )
            if capped or len(rows) < _CONSUMER_PAGE_ROWS:
                break  # a short page means the source is exhausted: a complete reading
        else:
            # Ran out of pages with rows still coming, so this is a bounded reading.
            self._walk_incomplete.add("semantic_limit")
        return hops

    def _consumer_where(self, tables: list[str]) -> list[str]:
        """Predicates shared by the single-node read and its batched prefetch.

        Built in one place because the two paths must prune identically: a predicate added to one
        and not the other would make the graph depend on whether a level happened to be batched.
        """
        placeholders = ", ".join("?" for _ in tables)
        return [
            "DEPENDENT_SCHEMA_NAME = ?",
            f"BASE_OBJECT_NAME IN ({placeholders})",
            "DEPENDENCY_TYPE = ?",
            "DEPENDENT_OBJECT_NAME LIKE ?",
            # Hierarchy views are excluded as a path *segment*, not as a bare substring, so a
            # provider genuinely named '/HIERARCHY_X' is not caught by its own name.
            "DEPENDENT_OBJECT_NAME NOT LIKE ?",
            "DEPENDENT_OBJECT_NAME NOT LIKE ?",
        ]

    def _consumer_params(self, tables: list[str]) -> list[Any]:
        hier_segment = CALC_VIEW_HIER_MARKER.rstrip("/")
        return [
            _CALC_SCHEMA,
            *tables,
            _TRANSITIVE_DEPENDENCY,
            f"{CALC_VIEW_PACKAGE}%",
            f"%{hier_segment}/%",
            f"%{hier_segment}",
        ]

    def _consumer_hops_from(self, name: str, view_names: list[str], *, root_key: str) -> list[_Hop]:
        """Resolve prefetched dependent view names, applying the same cap as the paged read."""
        hops: list[_Hop] = []
        seen: set[tuple[str, str]] = set()
        self._absorb_consumers(name, view_names, root_key=root_key, hops=hops, seen=seen)
        return hops

    def _absorb_consumers(
        self,
        name: str,
        view_names: list[str],
        *,
        root_key: str,
        hops: list[_Hop],
        seen: set[tuple[str, str]],
    ) -> bool:
        """Resolve view names into ``hops``, returning whether the consumer cap stopped it.

        One implementation, used by both the paged read and the prefetched list, so a cap that
        binds selects the same subset and reports the same bound either way.
        """
        for raw in view_names:
            hop = self._consumer_hop(name, raw, root_key=root_key)
            if hop is None:
                continue
            key = (hop.node_type, hop.name.strip().upper())
            if key in seen:
                continue
            seen.add(key)
            hops.append(hop)
            if len(seen) >= _MAX_COMPOSITE_CONSUMERS:
                # Stopped on the stated cap. Whether more existed is unknown, so the walk is
                # reported as bounded rather than complete.
                self._walk_incomplete.add("semantic_limit")
                return True
        return False

    def _prefetch_consumers(self, names: list[str]) -> None:
        """One OBJECT_DEPENDENCIES read per BFS level instead of one paged read per node.

        The costliest branch of the walk by a wide margin: measured on a production system this
        read answered in 667ms against a ~110ms floor for every other statement, and 166 of them
        accounted for 34% of a depth-3 both-directions walk. The rows are the same either way -
        ``BASE_OBJECT_NAME`` is added to the projection so each row can be attributed back to the
        provider whose generated table it names.

        Abandoned rather than truncated. If the whole-batch row bound binds, or a table cannot be
        attributed to exactly one node, nothing is cached and every node falls back to its own
        paged read, which reports its own bound. A prefetch may cost speed, never correctness.
        """
        eligible = [(name, _consumer_tables(name)) for name in names]
        eligible = [(name, tables) for name, tables in eligible if tables]
        for chunk in _table_chunks(eligible, _PREFETCH_TABLE_CHUNK):
            owner_of = _owner_of_tables(chunk)
            if owner_of is None:
                continue  # ambiguous attribution - abandon the chunk
            buckets = self._read_consumer_batch(chunk, owner_of)
            if buckets is None:
                continue  # abandon the chunk - see the docstring
            for name, collected in buckets.items():
                # Re-sorted on the dependent name alone. The batched read groups by base table
                # first, and a provider owns several, so without this a node's cached order would
                # differ from the order its own paged read produces - and where the consumer cap
                # binds, a different order is a different subset.
                self._pf_consumers[name] = sorted(set(collected))

    def _read_consumer_batch(
        self, chunk: list[tuple[str, list[str]]], owner_of: dict[str, str]
    ) -> dict[str, list[str]] | None:
        """Read one batch to exhaustion, or ``None`` if it must not be cached.

        ``None`` covers three cases that all mean the same thing for the caller: a row whose base
        table belongs to no node in the batch, the whole-batch row bound binding, and the call
        budget running low. In each, what was read cannot be relied on as a node's complete list.
        """
        tables = [table for _, candidates in chunk for table in candidates]
        buckets: dict[str, list[str]] = {name: [] for name, _ in chunk}
        base_query = self.dialect.build_select(
            columns=["DISTINCT BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME"],
            from_logical="object_dependencies",
            where=self._consumer_where(tables),
            params=self._consumer_params(tables),
            order_by=["BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME"],
        )
        fetched = 0
        while fetched < _PREFETCH_CONSUMER_MAX_ROWS:
            if self._budget_stop() is not None:
                return None  # leave the allowance to the expansions themselves
            rows = self.select(
                self.dialect.paginate(base_query, limit=_CONSUMER_PAGE_ROWS, offset=fetched)
            )
            for base_object, view_name in rows:
                owner = owner_of.get(str(base_object).strip().upper())
                if owner is None:
                    return None
                buckets[owner].append(str(view_name).strip())
            fetched += len(rows)
            if len(rows) < _CONSUMER_PAGE_ROWS:
                return buckets  # a short page means the batch is exhausted
        return None

    def _consumer_hop(self, name: str, view_name: str, *, root_key: str) -> _Hop | None:
        """One dependent calc view read as the object that owns it, or ``None`` if it owns nothing.

        Two kinds of generated view reach here and they are **not** the same object. A provider view
        belongs to a CompositeProvider; a ``query.<provider>`` view belongs to a BEx query. Both
        previously read as CompositeProviders, so a query arrived under a synthesized name like
        ``/QUERY.<PROVIDER>/<QUERY>`` and was typed ``compositeprovider`` - defect D9. A query
        consuming this object is a real relationship, so it is kept and labelled, not discarded.
        """
        provenance = self.provenance("object_dependencies", {"DEPENDENT_OBJECT_NAME": view_name})

        provider = provider_from_calc_view(view_name)
        if provider:
            # A provider is not its own consumer: its generated view legitimately depends on its own
            # tables, so that row is a resolver artefact rather than a lineage relationship.
            if provider.strip().upper() == root_key:
                return None
            return _Hop(
                provider,
                "compositeprovider",
                LineageEdge(
                    src=name,
                    dst=provider,
                    kind="composite_part",
                    derivation="declared",
                    confidence="advisory",
                    note="CompositeProvider resolved via its generated calc view",
                    evidence=evidence_for("calc_view_consumer", "provider"),
                    provenance=provenance,
                ),
            )

        resolved = query_from_calc_view(view_name)
        if resolved is None:
            return None
        query_name, owning_provider = resolved
        if query_name.strip().upper() == root_key:
            return None
        return _Hop(
            query_name,
            "query",
            LineageEdge(
                src=name,
                dst=query_name,
                kind="query_provider",
                derivation="declared",
                confidence="advisory",
                note=(
                    f"BEx query reading this object, resolved via its generated calc view "
                    f"(query provider: {owning_provider})"
                ),
                evidence=evidence_for("calc_view_consumer", "query"),
                provenance=provenance,
            ),
        )

    def _budget_stop(self) -> LineageCompleteness | None:
        """Whether the active per-call budget leaves room for another page.

        Checked rather than caught. Letting :class:`BudgetExceeded` fly from here would convert a
        clean, reported budget stop into a degraded branch on every later node, so discovery leaves
        headroom and says it stopped; the budget itself still governs the call.
        """
        budget = current_budget()
        if budget is None:
            return None
        if (
            budget.max_seconds > 0
            and budget.elapsed_seconds >= budget.max_seconds - _BUDGET_TIME_HEADROOM
        ):
            return "time_budget"
        if budget.max_queries > 0 and budget.queries >= budget.max_queries - _BUDGET_QUERY_HEADROOM:
            return "query_budget"
        return None

    # --- declared edges (transformations + DTPs) -----------------------------------------

    def _declared_hops(self, name: str, *, downstream: bool) -> list[_Hop]:
        merged: dict[str, dict[str, Any]] = {}
        self._collect_transformation_hops(name, downstream=downstream, merged=merged)
        self._collect_dtp_hops(name, downstream=downstream, merged=merged)

        hops: list[_Hop] = []
        for other_name, info in merged.items():
            other_type = _node_type(info["type_code"])
            src, dst = (name, other_name) if downstream else (other_name, name)
            modes = _sorted_modes(cast("set[UpdateMode]", info.get("update_modes") or set()))
            hops.append(
                _Hop(
                    other_name,
                    other_type,
                    LineageEdge(
                        src=src,
                        dst=dst,
                        kind="dtp" if info.get("dtp_only") else "transformation",
                        derivation="declared",
                        confidence="exact",
                        transformation_id=info.get("tran_id"),
                        update_modes=modes,
                        update_mode=modes[0] if modes else None,
                        provenance=info["provenance"],
                    ),
                )
            )
        return hops

    def _collect_transformation_hops(
        self, name: str, *, downstream: bool, merged: dict[str, dict[str, Any]]
    ) -> None:
        for other, type_code, tran_id in self._transformation_rows(name, downstream=downstream):
            if not other:
                continue
            merged[other] = {
                "type_code": type_code,
                "tran_id": tran_id or None,
                "provenance": self.provenance("transformation", {"TRANID": tran_id}),
            }

    def _transformation_rows(self, name: str, *, downstream: bool) -> list[tuple[str, str, str]]:
        """``(other_name, other_type_code, tran_id)`` for one endpoint, prefetched or read now.

        Ordered by TRANID in both paths. Two transformations can connect the same pair of objects,
        and the caller keeps the last row per neighbour, so an unordered read let the surviving
        TRANID depend on whatever order the database happened to return - and the routine cap in
        :meth:`_transformations_targeting` truncates the same list. Stating the order makes the
        batched and single-node paths agree and makes either one reproducible.
        """
        cached = self._pf_tran.get((name, downstream))
        if cached is not None:
            return cached
        key_col, other_name_col, other_type_col = _TRAN_COLUMNS[downstream]
        rows = self.select(
            self.dialect.build_select(
                columns=[other_name_col, other_type_col, "TRANID"],
                from_logical="transformation",
                where=[f"{key_col} = ?"],
                params=[name],
                order_by=["TRANID"],
            )
        )
        return [
            (str(other_name).strip(), str(other_type).strip(), str(tran_id).strip())
            for other_name, other_type, tran_id in rows
        ]

    def _collect_dtp_hops(
        self, name: str, *, downstream: bool, merged: dict[str, dict[str, Any]]
    ) -> None:
        if not self.capability.is_available("dtp"):
            return
        for other, type_code, updmode in self._dtp_rows(name, downstream=downstream):
            if not other:
                continue
            update_mode = _UPDMODE_MAP.get(updmode)
            existing = merged.get(other)
            if existing is None:
                prov_key = {"SRC": name} if downstream else {"TGT": name}
                existing = merged[other] = {
                    "type_code": type_code,
                    "dtp_only": True,
                    "provenance": self.provenance("dtp", prov_key),
                }
            # Accumulated, not assigned. Several active DTPs routinely connect one pair with
            # different modes - a repair/init full alongside the regular delta - so the last row
            # read is not "the" update mode. Overwriting here meant the answer depended on row
            # order, and the same edge came back 'full' or 'delta' on different runs (D13).
            if update_mode is not None:
                modes = cast("set[UpdateMode]", existing.setdefault("update_modes", set()))
                modes.add(update_mode)

    def _dtp_rows(self, name: str, *, downstream: bool) -> list[tuple[str, str, str]]:
        """``(other_name, other_type_code, updmode)`` for one endpoint, prefetched or read now.

        Ordered by the neighbour's name in both paths, for the same reason as
        :meth:`_transformation_rows`: several DTPs can connect one pair of objects and the caller
        keeps the last update mode it sees, so an unstated order let the database decide.
        """
        cached = self._pf_dtp.get((name, downstream))
        if cached is not None:
            return cached
        key_col, other_name_col, other_type_col = _DTP_COLUMNS[downstream]
        rows = self.select(
            self.dialect.build_select(
                columns=[other_name_col, other_type_col, "UPDMODE"],
                from_logical="dtp",
                where=[f"{key_col} = ?", "OBJVERS = 'A'"],  # RSBK* prefix: no OBJVERS auto-inject
                params=[name],
                order_by=[other_name_col],
            )
        )
        return [
            (str(other_name).strip(), str(other_type).strip(), str(updmode).strip())
            for other_name, other_type, updmode in rows
        ]

    # --- routine-derived edges -----------------------------------------------------------

    def _routine_lookup_hops(self, name: str) -> list[_Hop]:
        """Upstream lookups: tables a transformation *targeting* ``name`` reads in its routines."""
        if not self.capability.is_available("routine_source"):
            return []
        hops: list[_Hop] = []
        seen: set[str] = set()
        for tran_id in self._transformations_targeting(name)[:_ROUTINE_TRANS_CAP]:
            analyses = self._transformations.analyze_routines(tran_id)
            if isinstance(analyses, UnsupportedResult):
                continue
            for analysis in analyses:
                for dep in analysis.table_dependencies:
                    obj = dep.resolved_object
                    if not obj or obj in seen:
                        continue
                    seen.add(obj)
                    node_type: LineageNodeType = (
                        "dso" if dep.resolved_kind == "dso" else "infoobject"
                    )
                    hops.append(
                        _Hop(
                            obj,
                            node_type,
                            LineageEdge(
                                src=obj,
                                dst=name,
                                kind="routine_lookup",
                                derivation="routine",
                                confidence="advisory",
                                transformation_id=tran_id,
                                note="read by an inbound transformation routine (advisory)",
                                provenance=self.provenance(
                                    "routine_source", {"CODEID": analysis.code_id, "OBJVERS": "A"}
                                ),
                            ),
                        )
                    )
        return hops

    def _transformations_targeting(self, name: str) -> list[str]:
        """Transformations whose target is ``name``, ordered by TRANID.

        Reads the upstream declared-edge rows, which already carry TRANID per target, so during a
        walk this is free rather than a statement of its own. ``_ROUTINE_TRANS_CAP`` truncates the
        result, which is why the order is stated: an unordered read decided which routines got
        parsed - and therefore which advisory edges existed - by whatever the database returned.
        """
        return [
            tran_id
            for _other, _type, tran_id in self._transformation_rows(name, downstream=False)
            if tran_id
        ]

    def _routine_consumers_of(self, name: str) -> list[tuple[str, LineageNodeType, str]]:
        """Reverse: (target, type, tran_id) for transformations whose routines reference name.

        Memoised for the same reason as :meth:`_expand`: this scans the ABAP source table and then
        resolves the owning transformations' targets, and impact analysis asks for the same objects
        repeatedly across a full-system generation.
        """
        cached = self._consumers_memo.get(name)
        if cached is not None:
            return cached
        resolved = self._routine_consumers_uncached(name)
        self._consumers_memo[name] = resolved
        return resolved

    def _routine_consumers_uncached(self, name: str) -> list[tuple[str, LineageNodeType, str]]:
        if not self.capability.is_available("routine_source"):
            return []
        code_ids = self._codeids_referencing(name)
        if not code_ids:
            return []
        tran_ids = self._tranids_for_codeids(code_ids)
        return self._targets_for_tranids(tran_ids)

    def _codeids_referencing(self, name: str) -> list[str]:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["CODEID"],
                    from_logical="routine_source",
                    where=["OBJVERS = 'A'", "UPPER(LINE) LIKE ?"],
                    params=[f"%{name.upper()}%"],
                    # This is the reverse routine scan behind impact analysis, capped and then
                    # deduped and sliced to _REVERSE_CODEID_CAP. Unordered, the surviving code-ids
                    # were an arbitrary subset, so the routine-embedded consumers that make impact
                    # analysis worth running differed between identical calls (D8).
                    order_by=["CODEID"],
                ),
                limit=_REVERSE_CODEID_CAP * 20,  # many lines share a code-id; dedupe below
            )
        )
        return sorted({str(r[0]).strip() for r in rows if str(r[0]).strip()})[:_REVERSE_CODEID_CAP]

    def _tranids_for_codeids(self, code_ids: list[str]) -> set[str]:
        placeholders = ", ".join("?" for _ in code_ids)
        tran_ids: set[str] = set()
        # field/formula/unit routines
        if self.capability.is_available("transformation_step_rout"):
            rows = self.select(
                self.dialect.build_select(
                    columns=["TRANID"],
                    from_logical="transformation_step_rout",
                    where=[f"CODEID IN ({placeholders})"],
                    params=list(code_ids),
                )
            )
            tran_ids.update(str(r[0]).strip() for r in rows if str(r[0]).strip())
        # header routines (start/end/expert/global)
        cols = ("STARTROUTINE", "ENDROUTINE", "EXPERT", "GLBCODE", "GLBCODE2")
        clause = " OR ".join(f"{c} IN ({placeholders})" for c in cols)
        rows = self.select(
            self.dialect.build_select(
                columns=["TRANID"],
                from_logical="transformation",
                where=[f"({clause})"],
                params=list(code_ids) * len(cols),
            )
        )
        tran_ids.update(str(r[0]).strip() for r in rows if str(r[0]).strip())
        return tran_ids

    def _targets_for_tranids(self, tran_ids: set[str]) -> list[tuple[str, LineageNodeType, str]]:
        if not tran_ids:
            return []
        ordered = sorted(tran_ids)
        placeholders = ", ".join("?" for _ in ordered)
        rows = self.select(
            self.dialect.build_select(
                columns=["TRANID", "TARGETNAME", "TARGETTYPE"],
                from_logical="transformation",
                where=[f"TRANID IN ({placeholders})"],
                params=ordered,
            )
        )
        result: list[tuple[str, LineageNodeType, str]] = []
        for tran_id, target_name, target_type in rows:
            target = str(target_name).strip()
            if not target:
                continue
            node_type = _node_type(target_type)
            result.append((target, node_type, str(tran_id).strip()))
        return result

    # --- graph assembly ------------------------------------------------------------------

    def _ensure_node(
        self,
        nodes: dict[str, LineageNode],
        name: str,
        node_type: LineageNodeType,
        provenance: Provenance | list[Provenance],
    ) -> None:
        existing = nodes.get(name)
        if existing is None:
            nodes[name] = LineageNode(
                id=name,
                object_type=node_type,
                name=name,
                upstream_resolved=node_type != "datasource",
                provenance=provenance,
            )
        elif existing.object_type == "unknown" and node_type != "unknown":
            # A node can be created before its type is known (a neighbour hop names it first) and
            # upgraded when a later hop or the provider catalogue resolves it. The canonical ref was
            # derived at construction, so it has to be rebuilt here or it keeps the stale type - and
            # then two tools disagree about the same object, which is the whole thing being fixed.
            existing.object_type = node_type
            existing.ref = BwObjectRef(object_type=normalise_object_type(node_type), name=name)
            existing.upstream_resolved = node_type != "datasource"

    def _resolve_boundary(self, nodes: dict[str, LineageNode], hop: _Hop) -> None:
        """Record on the DataSource node that its source-system parent was resolved.

        ``upstream_resolved`` is what a caller reads to decide whether the graph really ends there.
        Leaving it ``False`` on a DataSource that now *has* a parent in the same graph would put the
        node and the edge in contradiction, and the flag is the one a client is documented to trust.
        """
        boundary = nodes.get(hop.edge.dst)
        if boundary is None or boundary.object_type != "datasource":
            return
        _datasource, logsys = split_datasource_endpoint(boundary.name)
        boundary.upstream_resolved = True
        boundary.source_system = SourceSystemRef(
            # The *kind* of source system is a separate decode (bw_get_source_systems owns it); this
            # names the system and the object without claiming to have classified it.
            system_type="unknown",
            system_id=logsys,
            object_name=hop.name,
        )

    def _graph(
        self,
        root: str,
        direction: LineageDirection,
        depth: int,
        nodes: dict[str, LineageNode],
        edges: list[LineageEdge],
        *,
        completeness: LineageCompleteness = "complete",
    ) -> LineageGraph:
        caveats = [_ROUTINE_CAVEAT]
        if completeness != "complete":
            caveats.append(_COMPLETENESS_CAVEATS[completeness])
        caveats.extend(_evidence_caveats(edges))
        return LineageGraph(
            root_id=root,
            direction=direction,
            depth=depth,
            nodes=list(nodes.values()),
            edges=edges,
            node_count=len(nodes),
            edge_count=len(edges),
            truncated=completeness != "complete",
            completeness=completeness,
            caveats=caveats,
        )
