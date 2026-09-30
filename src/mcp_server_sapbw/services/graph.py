"""The object graph as a component, rather than a shape each caller re-walks.

**Why this exists.** Every dependency question was answered by its own traversal: the lineage
service walked transformations, the layer analyzer built a separate adjacency map, the load closure
walked chains, and the field-lineage service walked rules. Each was correct, and none could answer
a question the others already had the data for. The visible cost was in the layer analyzer, which
carried this caveat:

    Write-back detection covers self-loops and two-object cycles. Longer cycles (A->B->C->A) are
    not searched.

A three-object circular dependency has no correct load order either, and it was invisible. (The
finding is no longer called "write-back": in BW that term already means planning data written back
to a provider, so it was ambiguous inside BW's own vocabulary as well as outside it.)

:class:`ObjectGraph` holds nodes keyed by canonical id (``models.objects.BwObjectRef.id``) and
answers the dependency questions once: neighbours, reachability, paths, and cycles.

**Cycles are reported as strongly connected components, not as enumerated loops.** Enumerating
elementary cycles is exponential and has to be truncated, which turns "is this landscape cyclic"
into "here are some cycles we found before giving up". Tarjan's algorithm is linear in nodes plus
edges, needs no cap, and produces the more useful statement: every object in a component of two or
more is reachable from every other, so *no* load order for that group is correct. Self-loops are
reported separately because a single object that feeds itself is a different conversation.

Pure module: no connection, no I/O. Callers hand it edges.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..models.evidence import Evidence, EvidenceSummary, summarise
from ..models.lineage import LineageGraph
from ..models.objects import BwObjectRef, normalise_object_type, object_id

#: Bound on path enumeration. Paths between two objects can multiply combinatorially through a
#: MultiProvider, so this is capped and the cap is reported rather than hidden.
MAX_PATHS = 200
MAX_PATH_LENGTH = 24

Direction = str  # "downstream" | "upstream" | "both"


@dataclass(frozen=True)
class GraphEdge:
    """One directed dependency, in the direction data flows."""

    src: str
    dst: str
    kind: str = "transformation"
    evidence: Evidence | None = None


@dataclass
class Cycle:
    """A circular dependency: objects that each depend on the others, transitively.

    ``members`` of size one is a self-loop: an object whose transformation reads and writes it.
    Size two or more is a strongly connected component - every member is reachable from every
    other, so no load order produces a defined result and re-running a failed request cannot
    fix it.
    """

    members: list[str]
    edges: list[GraphEdge] = field(default_factory=list)

    @property
    def is_self_loop(self) -> bool:
        return len(self.members) == 1


@dataclass
class GraphStats:
    """Shape of the graph, for a caller deciding whether to trust or narrow a result."""

    node_count: int = 0
    edge_count: int = 0
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_kind: dict[str, int] = field(default_factory=dict)
    #: Objects nothing feeds (entry points) and objects that feed nothing (terminal consumers).
    source_count: int = 0
    sink_count: int = 0
    isolated_count: int = 0
    max_fan_out: int = 0
    max_fan_in: int = 0
    evidence: EvidenceSummary | None = None


@dataclass
class PathResult:
    """Paths from one object to another, with the truncation stated."""

    src: str
    dst: str
    paths: list[list[str]] = field(default_factory=list)
    truncated: bool = False
    reason: str | None = None


class ObjectGraph:
    """A directed graph of BW objects keyed by canonical id."""

    def __init__(self) -> None:
        self._nodes: dict[str, BwObjectRef] = {}
        self._out: dict[str, list[GraphEdge]] = {}
        self._in: dict[str, list[GraphEdge]] = {}

    # --- construction ---------------------------------------------------------------------

    def add_node(self, ref: BwObjectRef) -> str:
        """Add or merge a node. A later, better-typed ref upgrades an ``unknown`` one."""
        key = ref.id
        existing = self._nodes.get(key)
        if existing is None or (existing.object_type == "unknown" and ref.object_type != "unknown"):
            self._nodes[key] = ref
        return key

    def add_edge(
        self,
        src: BwObjectRef,
        dst: BwObjectRef,
        *,
        kind: str = "transformation",
        evidence: Evidence | None = None,
    ) -> None:
        """Add a directed edge, creating either endpoint if it is new. Duplicates are ignored."""
        src_id, dst_id = self.add_node(src), self.add_node(dst)
        edge = GraphEdge(src=src_id, dst=dst_id, kind=kind, evidence=evidence)
        out = self._out.setdefault(src_id, [])
        if any(e.dst == dst_id and e.kind == kind for e in out):
            return
        out.append(edge)
        self._in.setdefault(dst_id, []).append(edge)

    @classmethod
    def from_lineage(cls, graph: LineageGraph) -> ObjectGraph:
        """Build from a :class:`LineageGraph`, using each node's canonical ref as its key.

        The lineage graph keys its own nodes by ``id``, because its edges reference that key. Here
        the key is type-qualified, which is what makes two graphs from different tools - or from
        different systems - comparable.

        **Endpoints resolve through ``id``, not ``name``.** They used to resolve through ``name``,
        which held only because the two were equal for every node ever built: a lineage node's id
        *is* its bare technical name. The moment one node's display name legitimately differs from
        its key - which is what D19 does to a DataSource, whose stored endpoint carries a logical
        system that is not part of its identity - every edge touching it would fail this lookup and
        be dropped silently, taking the DataSource out of every rendered diagram with no caveat.
        """
        built = cls()
        by_id = {node.id: node for node in graph.nodes}
        for node in graph.nodes:
            built.add_node(node.ref or BwObjectRef(object_type="unknown", name=node.name))
        for edge in graph.edges:
            src_node, dst_node = by_id.get(edge.src), by_id.get(edge.dst)
            if src_node is None or dst_node is None:
                continue
            built.add_edge(
                src_node.ref or BwObjectRef(object_type="unknown", name=src_node.name),
                dst_node.ref or BwObjectRef(object_type="unknown", name=dst_node.name),
                kind=edge.kind,
                evidence=edge.evidence,
            )
        return built

    @classmethod
    def from_pairs(
        cls, pairs: list[tuple[str, str, str]], *, kind: str = "transformation"
    ) -> ObjectGraph:
        """Build from ``(source_name, target_name, type_code)`` triples.

        The shape the transformation catalogue produces: names plus a TLOGO code. Types normalise
        through the canonical vocabulary, so the ids match those from any other route.
        """
        built = cls()
        for source, target, target_type in pairs:
            if not source or not target:
                continue
            built.add_edge(
                BwObjectRef(object_type="unknown", name=source),
                BwObjectRef(object_type=normalise_object_type(target_type), name=target),
                kind=kind,
            )
        return built

    # --- inspection -----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, key: str) -> bool:
        return key in self._nodes

    @property
    def node_ids(self) -> list[str]:
        return sorted(self._nodes)

    def node(self, key: str) -> BwObjectRef | None:
        return self._nodes.get(key)

    def key_for(self, object_type: object, name: str) -> str:
        """The canonical key for a type/name pair, so a caller need not build a ref."""
        return object_id(object_type, name)

    def edges(self) -> list[GraphEdge]:
        return [edge for edges in self._out.values() for edge in edges]

    def neighbours(self, key: str, direction: Direction = "downstream") -> list[str]:
        """Immediate neighbours in the given direction."""
        found: list[str] = []
        if direction in ("downstream", "both"):
            found.extend(e.dst for e in self._out.get(key, []))
        if direction in ("upstream", "both"):
            found.extend(e.src for e in self._in.get(key, []))
        return sorted(set(found))

    def reachable(
        self, key: str, direction: Direction = "downstream", depth: int | None = None
    ) -> set[str]:
        """Everything reachable from ``key``, excluding itself unless a cycle returns to it."""
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(key, 0)])
        while queue:
            current, level = queue.popleft()
            if depth is not None and level >= depth:
                continue
            for neighbour in self.neighbours(current, direction):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                queue.append((neighbour, level + 1))
        return seen

    def paths(
        self,
        src: str,
        dst: str,
        *,
        max_paths: int = MAX_PATHS,
        max_length: int = MAX_PATH_LENGTH,
    ) -> PathResult:
        """Every simple path from ``src`` to ``dst``, bounded and honest about the bound.

        "How does data get from this DataSource to this report" is the question a troubleshooter
        actually asks, and it is not answerable from a neighbour list.
        """
        result = PathResult(src=src, dst=dst)
        if src not in self._nodes or dst not in self._nodes:
            result.reason = "one or both endpoints are not in this graph"
            return result

        stack: list[tuple[str, list[str], set[str]]] = [(src, [src], {src})]
        while stack:
            current, path, on_path = stack.pop()
            if len(result.paths) >= max_paths:
                result.truncated = True
                result.reason = f"stopped at the {max_paths}-path cap"
                break
            if len(path) > max_length:
                result.truncated = True
                result.reason = f"stopped at the {max_length}-hop path-length cap"
                continue
            for neighbour in self.neighbours(current, "downstream"):
                if neighbour == dst:
                    result.paths.append([*path, neighbour])
                elif neighbour not in on_path:
                    stack.append((neighbour, [*path, neighbour], on_path | {neighbour}))
        result.paths.sort(key=lambda p: (len(p), p))
        return result

    def cycles(self) -> list[Cycle]:
        """Every cyclic group in the graph. Complete: no cap, and nothing is truncated.

        Reported as strongly connected components rather than enumerated elementary cycles.
        Enumeration is exponential and would have to be capped, which would answer "here are some
        cycles we found" instead of "this is where the graph is cyclic". A component of two or more
        means every member is reachable from every other, so no load order for the group is correct.
        """
        found: list[Cycle] = []
        for component in self._strongly_connected():
            members = sorted(component)
            if len(members) > 1:
                found.append(Cycle(members=members, edges=self._edges_within(component)))
                continue
            only = members[0]
            if any(e.dst == only for e in self._out.get(only, [])):
                found.append(Cycle(members=members, edges=self._edges_within(component)))
        found.sort(key=lambda c: (-len(c.members), c.members))
        return found

    def stats(self) -> GraphStats:
        """Counts and extremes, so a caller can judge a result without walking it."""
        all_edges = self.edges()
        by_type: dict[str, int] = {}
        for ref in self._nodes.values():
            by_type[ref.object_type] = by_type.get(ref.object_type, 0) + 1
        by_kind: dict[str, int] = {}
        for edge in all_edges:
            by_kind[edge.kind] = by_kind.get(edge.kind, 0) + 1

        sources = sinks = isolated = 0
        for key in self._nodes:
            fan_in, fan_out = len(self._in.get(key, [])), len(self._out.get(key, []))
            if not fan_in and not fan_out:
                isolated += 1
            elif not fan_in:
                sources += 1
            elif not fan_out:
                sinks += 1
        return GraphStats(
            node_count=len(self._nodes),
            edge_count=len(all_edges),
            nodes_by_type=dict(sorted(by_type.items())),
            edges_by_kind=dict(sorted(by_kind.items())),
            source_count=sources,
            sink_count=sinks,
            isolated_count=isolated,
            max_fan_out=max((len(v) for v in self._out.values()), default=0),
            max_fan_in=max((len(v) for v in self._in.values()), default=0),
            evidence=summarise([e.evidence for e in all_edges if e.evidence]) or None,
        )

    # --- internals ------------------------------------------------------------------------

    def _edges_within(self, component: set[str]) -> list[GraphEdge]:
        return [
            edge
            for key in sorted(component)
            for edge in self._out.get(key, [])
            if edge.dst in component
        ]

    def _strongly_connected(self) -> list[set[str]]:
        """Tarjan's algorithm, iterative.

        Iterative rather than recursive on purpose: a BW dependency chain can be hundreds of objects
        deep, and a recursive implementation would hit Python's stack limit on a real landscape -
        turning a legitimate answer into a crash.
        """
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        components: list[set[str]] = []
        counter = 0

        for root in sorted(self._nodes):
            if root in index:
                continue
            # Each work item is (node, iterator over its successors).
            work: list[tuple[str, list[str], int]] = [
                (root, self.neighbours(root, "downstream"), 0)
            ]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)

            while work:
                node, successors, position = work[-1]
                if position < len(successors):
                    work[-1] = (node, successors, position + 1)
                    nxt = successors[position]
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, self.neighbours(nxt, "downstream"), 0))
                    elif nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: set[str] = set()
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.add(member)
                        if member == node:
                            break
                    components.append(component)
        return components
