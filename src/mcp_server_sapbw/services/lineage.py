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

from ..models.evidence import summarise
from ..models.lineage import (
    ImpactAnalysis,
    LineageDirection,
    LineageEdge,
    LineageGraph,
    LineageNode,
    LineageNodeType,
    TraceToSource,
    UpdateMode,
)
from ..models.objects import BwObjectRef, normalise_object_type
from ..models.provenance import Provenance, UnsupportedResult
from ..models.providers import Provider
from ..repositories.base import Repository
from ..repositories.providers import ProvidersRepository
from ..repositories.transformations import TransformationsRepository
from .table_resolver import candidate_tables, provider_from_calc_view


# RSTLOGO type code -> lineage node type. Decoded through the canonical table in models.objects
# rather than a local copy: three copies of this map existed and had already drifted (this one said
# "cube" where the provider vocabulary said "infocube").
def _node_type(code: object) -> LineageNodeType:
    """Canonical node type for a raw TLOGO code or a provider-type string."""
    return cast("LineageNodeType", normalise_object_type(code))


_UPDMODE_MAP: dict[str, UpdateMode] = {"F": "full", "D": "delta", "I": "init"}

# HANA schema holding generated BW calc views. Part-provider edges need TRANSITIVE dependencies
# (type 2): BW layers a CompositeProvider's calc view over intermediate views, so a part provider's
# active table is never a *direct* dependency of it (verified live). Bounded by the table filter.
_CALC_SCHEMA = "_SYS_BIC"
_TRANSITIVE_DEPENDENCY = 2
_MAX_COMPOSITE_CONSUMERS = 50

_MAX_NODES = 400
_MAX_DEPTH = 12
_ROUTINE_TRANS_CAP = 25  # transformations-per-node whose routines we parse for lookups
_REVERSE_CODEID_CAP = 150  # RSAABAP code-ids scanned in the reverse (impact) routine search

_ROUTINE_CAVEAT = (
    "routine-derived edges are advisory (heuristic lower bound): dynamic SQL, function-module and "
    "class-method calls are not followed"
)


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


class _Hop:
    """One resolved neighbour: the other object's name/type and the edge connecting it."""

    __slots__ = ("edge", "name", "node_type")

    def __init__(self, name: str, node_type: LineageNodeType, edge: LineageEdge) -> None:
        self.name = name
        self.node_type = node_type
        self.edge = edge


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
        self._expand_memo: dict[tuple[str, LineageDirection, bool], list[_Hop]] = {}
        self._node_type_memo: dict[str, LineageNodeType] = {}
        self._consumers_memo: dict[str, list[tuple[str, LineageNodeType, str]]] = {}
        self._trace_memo: dict[tuple[str, int], TraceToSource] = {}

    # --- public API ----------------------------------------------------------------------

    def get_lineage(
        self, name: str, *, direction: LineageDirection = "both", depth: int = 3
    ) -> LineageGraph | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        depth = max(1, min(depth, _MAX_DEPTH))
        nodes, edges, truncated = self._bfs(name, direction, depth, include_routine=True)
        return self._graph(name, direction, depth, nodes, edges, truncated=truncated)

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
        nodes, edges, truncated = self._bfs(name, "upstream", depth, include_routine=True)
        graph = self._graph(name, "upstream", depth, nodes, edges, truncated=truncated)
        datasources = [n.name for n in nodes.values() if n.object_type == "datasource"]
        caveats = [_ROUTINE_CAVEAT]
        if truncated:
            caveats.append(f"trace stopped at depth {depth} or the {_MAX_NODES}-node cap")
        if not datasources:
            caveats.append("no DataSource boundary reached within the depth limit")
        return TraceToSource(
            root_id=name,
            graph=graph,
            datasources_reached=sorted(datasources),
            unresolved_boundaries=sorted(datasources),  # BW-only: all datasources are unresolved
            caveats=caveats,
        )

    def impact_analysis(self, name: str, *, depth: int = 3) -> ImpactAnalysis | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        depth = max(1, min(depth, _MAX_DEPTH))
        nodes, edges, truncated = self._bfs(name, "downstream", depth, include_routine=False)

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

        graph = self._graph(name, "downstream", depth, nodes, edges, truncated=truncated)
        affected = [n for n in nodes.values() if n.name != name]
        by_type: dict[str, int] = {}
        for node in affected:
            by_type[node.object_type] = by_type.get(node.object_type, 0) + 1
        caveats = [_ROUTINE_CAVEAT]
        if truncated:
            caveats.append(f"downstream expansion stopped at depth {depth} or the node cap")
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
    ) -> tuple[dict[str, LineageNode], list[LineageEdge], bool]:
        nodes: dict[str, LineageNode] = {}
        edges: list[LineageEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()
        self._ensure_node(
            nodes, root, self._node_type_of(root), self.provenance("transformation", {})
        )
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        truncated = False

        while queue:
            current, level = queue.popleft()
            if current in visited or level >= depth:
                continue
            visited.add(current)
            for hop in self._expand(current, direction, include_routine=include_routine):
                self._ensure_node(nodes, hop.name, hop.node_type, hop.edge.provenance)
                key = (hop.edge.src, hop.edge.dst, hop.edge.kind)
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append(hop.edge)
                if len(nodes) >= _MAX_NODES:
                    truncated = True
                    break
                if hop.name not in visited:
                    queue.append((hop.name, level + 1))
            if truncated:
                break
        return nodes, edges, truncated

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
            return cached
        hops = self._expand_uncached(name, direction, include_routine=include_routine)
        self._expand_memo[memo_key] = hops
        return hops

    def _expand_uncached(
        self, name: str, direction: LineageDirection, *, include_routine: bool
    ) -> list[_Hop]:
        hops: list[_Hop] = []
        if direction in ("downstream", "both"):
            hops.extend(self._declared_hops(name, downstream=True))
            hops.extend(self._composite_consumer_hops(name))
            hops.extend(self._query_provider_hops(name, downstream=True))
        if direction in ("upstream", "both"):
            hops.extend(self._declared_hops(name, downstream=False))
            hops.extend(self._composite_part_hops(name))
            if include_routine:
                hops.extend(self._routine_lookup_hops(name))
            hops.extend(self._query_provider_hops(name, downstream=False))
        return hops

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
        query_repo = self._query_repo()
        header = query_repo._header(name)
        if header is None:
            return []
        providers = query_repo._providers_list(str(header[0]))
        if not providers:
            return []
        provider = providers[0]
        edge = LineageEdge(
            src=name if downstream else provider,
            dst=provider if downstream else name,
            kind="query_provider",
            derivation="declared",
            confidence="exact",
            note="provider resolved from the BEx query metadata",
            provenance=self.provenance("query_provider", {"COMPUID": str(header[0])}),
        )
        return [_Hop(provider, "unknown", edge)]

    # --- CompositeProvider part edges (via the generated HANA calc view) ------------------

    def _composite_part_hops(self, name: str) -> list[_Hop]:
        """If ``name`` is a CompositeProvider, the part providers that feed it.

        A CompositeProvider persists nothing and has no inbound transformation, so without this its
        upstream lineage is a dead end. Parts come from the base tables of its generated calc view
        (RSOHCPR.XML_DEF is commonly empty). Resolution is naming-convention based -> advisory.
        """
        if not self.capability.is_available("composite_header"):
            return []
        parts = self._providers.composite_parts(name)[0]
        hops: list[_Hop] = []
        for part in parts:
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
                        provenance=part.provenance,
                    ),
                )
            )
        return hops

    def _composite_consumer_hops(self, name: str) -> list[_Hop]:
        """CompositeProviders that consume ``name`` as a part provider (the reverse direction)."""
        if not (
            self.capability.is_available("object_dependencies")
            and self.capability.is_available("composite_header")
        ):
            return []
        tables: list[str] = []
        for kind in ("dso", "adso", "infocube"):
            tables.extend(candidate_tables(name, kind))
        if not tables:
            return []
        placeholders = ", ".join("?" for _ in tables)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT DEPENDENT_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        f"BASE_OBJECT_NAME IN ({placeholders})",
                        "DEPENDENCY_TYPE = ?",
                    ],
                    params=[_CALC_SCHEMA, *tables, _TRANSITIVE_DEPENDENCY],
                ),
                limit=_MAX_COMPOSITE_CONSUMERS,
            )
        )
        hops: list[_Hop] = []
        seen: set[str] = set()
        for (view_name,) in rows:
            provider = provider_from_calc_view(str(view_name).strip())
            if not provider or provider == name or provider in seen:
                continue
            seen.add(provider)
            hops.append(
                _Hop(
                    provider,
                    "compositeprovider",
                    LineageEdge(
                        src=name,
                        dst=provider,
                        kind="composite_part",
                        derivation="declared",
                        confidence="advisory",
                        note="CompositeProvider resolved via its generated calc view",
                        provenance=self.provenance(
                            "object_dependencies", {"DEPENDENT_OBJECT_NAME": str(view_name).strip()}
                        ),
                    ),
                )
            )
        return hops

    # --- declared edges (transformations + DTPs) -----------------------------------------

    def _declared_hops(self, name: str, *, downstream: bool) -> list[_Hop]:
        merged: dict[str, dict[str, Any]] = {}
        self._collect_transformation_hops(name, downstream=downstream, merged=merged)
        self._collect_dtp_hops(name, downstream=downstream, merged=merged)

        hops: list[_Hop] = []
        for other_name, info in merged.items():
            other_type = _node_type(info["type_code"])
            src, dst = (name, other_name) if downstream else (other_name, name)
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
                        update_mode=info.get("update_mode"),
                        provenance=info["provenance"],
                    ),
                )
            )
        return hops

    def _collect_transformation_hops(
        self, name: str, *, downstream: bool, merged: dict[str, dict[str, Any]]
    ) -> None:
        key_col, other_name_col, other_type_col = (
            ("SOURCENAME", "TARGETNAME", "TARGETTYPE")
            if downstream
            else ("TARGETNAME", "SOURCENAME", "SOURCETYPE")
        )
        rows = self.select(
            self.dialect.build_select(
                columns=[other_name_col, other_type_col, "TRANID"],
                from_logical="transformation",
                where=[f"{key_col} = ?"],
                params=[name],
            )
        )
        for other_name, other_type, tran_id in rows:
            other = str(other_name).strip()
            if not other:
                continue
            merged[other] = {
                "type_code": str(other_type).strip(),
                "tran_id": str(tran_id).strip() or None,
                "provenance": self.provenance("transformation", {"TRANID": str(tran_id).strip()}),
            }

    def _collect_dtp_hops(
        self, name: str, *, downstream: bool, merged: dict[str, dict[str, Any]]
    ) -> None:
        if not self.capability.is_available("dtp"):
            return
        key_col, other_name_col, other_type_col = (
            ("SRC", "TGT", "TGTTLOGO") if downstream else ("TGT", "SRC", "SRCTLOGO")
        )
        rows = self.select(
            self.dialect.build_select(
                columns=[other_name_col, other_type_col, "UPDMODE"],
                from_logical="dtp",
                where=[f"{key_col} = ?", "OBJVERS = 'A'"],  # RSBK* prefix: no OBJVERS auto-inject
                params=[name],
            )
        )
        for other_name, other_type, updmode in rows:
            other = str(other_name).strip()
            if not other:
                continue
            update_mode = _UPDMODE_MAP.get(str(updmode).strip())
            existing = merged.get(other)
            if existing is not None:
                existing["update_mode"] = update_mode  # enrich the transformation edge
            else:
                prov_key = {"SRC": name} if downstream else {"TGT": name}
                merged[other] = {
                    "type_code": str(other_type).strip(),
                    "update_mode": update_mode,
                    "dtp_only": True,
                    "provenance": self.provenance("dtp", prov_key),
                }

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
        rows = self.select(
            self.dialect.build_select(
                columns=["TRANID"],
                from_logical="transformation",
                where=["TARGETNAME = ?"],
                params=[name],
            )
        )
        return [str(r[0]).strip() for r in rows if str(r[0]).strip()]

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

    def _graph(
        self,
        root: str,
        direction: LineageDirection,
        depth: int,
        nodes: dict[str, LineageNode],
        edges: list[LineageEdge],
        *,
        truncated: bool,
    ) -> LineageGraph:
        caveats = [_ROUTINE_CAVEAT]
        if truncated:
            caveats.append(f"expansion stopped at depth {depth} or the {_MAX_NODES}-node cap")
        caveats.extend(_evidence_caveats(edges))
        return LineageGraph(
            root_id=root,
            direction=direction,
            depth=depth,
            nodes=list(nodes.values()),
            edges=edges,
            node_count=len(nodes),
            edge_count=len(edges),
            truncated=truncated,
            caveats=caveats,
        )
