"""Capture a system's structural metadata, and compare two captures.

Capture is one bounded query per object family, selecting only the columns that describe
*structure*. Volatile columns are not selected at all, rather than selected and filtered later: a
column that is never read cannot accidentally reach a fingerprint and make every object look
changed on every run.

Comparison is a set operation over canonical ids, with three corrections applied before anything is
called a difference. See ``models.snapshot`` for why each one matters.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from ..core.contract import contract_revision
from ..models.completeness import COMPLETE, BoundHit, Completeness
from ..models.objects import BwObjectRef, BwObjectType, normalise_object_type
from ..models.provenance import UnsupportedResult
from ..models.providers import classify_cube_type
from ..models.snapshot import (
    DEFAULT_FAMILIES,
    FactChange,
    ObjectChange,
    ObjectFingerprint,
    RekeyedObject,
    Snapshot,
    SnapshotDiff,
    SnapshotEdge,
    SnapshotFamily,
    SnapshotScope,
    SnapshotSummary,
    fingerprint_facts,
)
from ..repositories.base import Repository

#: Rows read per family. Generous against a real system (the largest family on the reference
#: system is 14,034 InfoObjects) and reported when it binds, because a silently truncated snapshot
#: makes the missing part look deleted on the next diff.
DEFAULT_ROW_CAP = 50_000


@dataclass(frozen=True)
class _Spec:
    """How to read one object family into fingerprints.

    ``columns`` describes shape only: no timestamp, no counter, no "last changed by". That is
    enforced by construction - a column never selected cannot reach a fingerprint - rather than by
    filtering a wide row afterwards.
    """

    logical: str
    key_column: str
    object_type: str
    columns: tuple[str, ...]
    #: Extra conditions. Needed where the dialect does not inject ``OBJVERS = 'A'`` itself: its
    #: auto-injection covers the RSD/RSO/RSZ/RSTRAN families only, and RSPCCHAINATTR and RSBKDTP are
    #: versioned but outside it. Measured on the reference system: RSBKDTP holds 1,451 active rows
    #: against 9,462 in total (6,963 delivered, 935 template, 113 modified), and RSPCCHAINATTR 1,463
    #: rows for 1,115 distinct chains. Without this, a snapshot would carry every version of every
    #: object, collapse them onto one id, and report phantom changes on every run.
    where: tuple[str, ...] = ()
    #: Derives the canonical type from the captured facts, where one table holds several types.
    type_from_facts: Callable[[dict[str, str]], str] | None = None
    #: Rewrites the fact map before fingerprinting, to separate facts BW stores in one column.
    normalise_facts: Callable[[dict[str, str]], dict[str, str]] | None = None


def _split_endpoint_facts(facts: dict[str, str]) -> dict[str, str]:
    """Separate a DTP endpoint's logical system from the object it names.

    A DataSource endpoint is stored as ``<DATASOURCE><padding><LOGSYS>``, so one column holds two
    facts. Measured on the reference system: 988 of 1,451 active DTPs carry a padded source. Left
    joined, the BDLS rewrite between environments makes every one of them read as changed, which
    buries whatever really differs. Split, the object stays comparable and the logical system
    becomes a fact of its own that can differ on its own.
    """
    out = dict(facts)
    for column in ("SRC", "TGT"):
        value = out.get(column, "")
        if " " in value:
            head, _, tail = value.partition(" ")
            out[column] = head
            out[f"{column}_LOGSYS"] = tail.strip()
    return out


def _cube_type(facts: dict[str, str]) -> str:
    """RSDCUBE holds three object kinds behind one table, discriminated by CUBETYPE.

    Measured: 74 basic InfoCubes, 67 MultiProviders, 2 virtual providers. Typing them all
    ``infocube`` would have mis-stated 69 of 143 objects and broken the canonical id they share with
    every other tool.
    """
    return classify_cube_type(facts.get("CUBETYPE", ""))


_FAMILY_SPECS: dict[str, _Spec] = {
    "providers.dso": _Spec("dso_header", "ODSOBJECT", "dso", ("ODSOTYPE", "INFOAREA", "BEXFL")),
    "providers.adso": _Spec("adso_header", "ADSONM", "adso", ("INFOAREA", "WRITE_CHANGELOG")),
    "providers.cube": _Spec(
        "cube_header",
        "INFOCUBE",
        "infocube",
        ("CUBETYPE", "OBJSTAT", "INFOAREA"),
        type_from_facts=_cube_type,
    ),
    "providers.composite": _Spec(
        "composite_header", "HCPRNM", "compositeprovider", ("OBJSTAT", "INFOAREA")
    ),
    "chains": _Spec(
        "chain_attr", "CHAIN_ID", "chain", ("OBJSTAT", "ACTIVFL"), where=("OBJVERS = 'A'",)
    ),
    # A DataSource's key includes its logical system - 3 of 949 on the reference system are
    # connected to two - so this is one of the families where the key column repeats and the facts
    # are merged. See `_capture_spec`.
    "datasources": _Spec("datasource", "DATASOURCE", "datasource", ("LOGSYS", "OBJSTAT", "TYPE")),
    "dtps": _Spec(
        "dtp",
        "DTP",
        "dtp",
        ("SRC", "SRCTLOGO", "TGT", "TGTTLOGO", "UPDMODE", "OBJSTAT"),
        where=("OBJVERS = 'A'",),
        normalise_facts=_split_endpoint_facts,
    ),
    "queries": _Spec("query_dir", "COMPUID", "query", ("COMPID", "INFOCUBE", "OBJSTAT")),
    "infoobjects": _Spec("infoobject", "IOBJNM", "infoobject", ("IOBJTP", "OBJSTAT")),
}


# Transformations are their own shape: the row carries both endpoints, which are also the edges.
# The routine slot is `EXPERT`, not `EXPERTROUTINE` - the latter was assumed from the mission's
# table map and rejected by BW 7.50 as an invalid column name. `GLBCODE`/`GLBCODE2` hold the global
# routine, which is as structural as the other three.
_TRANSFORMATION_COLUMNS = (
    "TRANID",
    "SOURCETYPE",
    "SOURCENAME",
    "TARGETTYPE",
    "TARGETNAME",
    "OBJSTAT",
    "STARTROUTINE",
    "ENDROUTINE",
    "EXPERT",
    "GLBCODE",
    "GLBCODE2",
)

_FAMILY_MEMBERS: dict[str, tuple[str, ...]] = {
    "providers": ("providers.dso", "providers.adso", "providers.cube", "providers.composite"),
    "chains": ("chains",),
    "datasources": ("datasources",),
    "dtps": ("dtps",),
    "queries": ("queries",),
    "infoobjects": ("infoobjects",),
}

_LOGICAL_SYSTEM_NOTE = (
    "DataSource identity excludes the logical system: BW stores a DataSource endpoint as the "
    "DataSource name padded out and suffixed with its logical system, and BDLS rewrites that "
    "suffix per environment. Comparing raw would report every DataSource as replaced. The logical "
    "system is kept as a fact, so a real BDLS difference shows as one changed fact."
)


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class _MemberResult:
    """What one family read produced. ``objects`` is ``None`` when the table is absent."""

    objects: list[ObjectFingerprint] | None
    truncated: bool
    merged_keys: int


def _merge_facts(variants: Sequence[dict[str, str]]) -> dict[str, str]:
    """Collapse several rows describing one object into one fact map, order-independently."""
    keys = {key for variant in variants for key in variant}
    return {
        key: ",".join(sorted({variant.get(key, "") for variant in variants} - {""}))
        for key in sorted(keys)
    }


class SnapshotService(Repository):
    """Captures snapshots. Comparison is a pure function and lives outside the class."""

    def capture(
        self,
        *,
        system: str,
        families: Sequence[str] | None = None,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> Snapshot | UnsupportedResult:
        """Read the structural facts for the requested families and fingerprint them."""
        from .. import __version__  # noqa: PLC0415 - avoids a package-level import cycle

        wanted = list(families or DEFAULT_FAMILIES)
        unknown = [f for f in wanted if f not in _FAMILY_MEMBERS and f != "transformations"]
        if unknown:
            raise ValueError(
                f"unknown snapshot families: {sorted(unknown)}; "
                f"choose from {sorted([*_FAMILY_MEMBERS, 'transformations'])}"
            )

        scope = SnapshotScope(row_cap=row_cap)
        objects, edges, merged = self._capture_families(wanted, row_cap, scope)
        caveats: list[str] = []

        if merged:
            caveats.append(
                f"{merged} object(s) were described by more than one metadata row, because BW keys "
                "the table on more than the name used as the identity here - the usual case is a "
                "DataSource connected to two logical systems. Their facts are the sorted set of "
                "the values found, so the reading is stable rather than dependent on row order."
            )
        if scope.families_unavailable:
            caveats.append(
                "these families were requested but this release does not carry the tables they "
                f"need, so they are absent from the snapshot rather than empty in it: "
                f"{', '.join(sorted(scope.families_unavailable))}"
            )
        if scope.truncated_families:
            caveats.append(
                f"the {row_cap}-row cap bound on {', '.join(sorted(scope.truncated_families))}. A "
                "diff against this snapshot would read the missing rows as deletions, so raise "
                "row_cap before comparing."
            )
        if "datasources" in scope.families or "transformations" in scope.families:
            caveats.append(_LOGICAL_SYSTEM_NOTE)

        available = sorted(
            name for name in self.capability.tables if self.capability.is_available(name)
        )
        taken_at = datetime.now(UTC)
        return Snapshot(
            snapshot_id=_snapshot_id(system, taken_at),
            system=system,
            taken_at=taken_at,
            bw_release=self.capability.bw_release,
            abap_schema=self.capability.abap_schema,
            server_version=__version__,
            contract_revision=contract_revision(),
            capability_digest=_digest(available),
            available_capabilities=available,
            objects=objects,
            edges=edges,
            scope=scope,
            caveats=caveats,
        )

    # --- per-family reads -----------------------------------------------------------------

    def _capture_families(
        self, wanted: Sequence[str], row_cap: int, scope: SnapshotScope
    ) -> tuple[list[ObjectFingerprint], list[SnapshotEdge], int]:
        """Read each requested family, recording coverage and truncation into ``scope`` as it goes.

        A family whose tables are absent lands in ``families_unavailable`` rather than contributing
        zero objects: "this release cannot report it" and "it has none" are different statements,
        and a later diff would read the second as a wholesale deletion.
        """
        objects: list[ObjectFingerprint] = []
        edges: list[SnapshotEdge] = []
        merged = 0
        for family in wanted:
            if family == "transformations":
                captured, family_edges, truncated = self._capture_transformations(row_cap)
                if captured is None:
                    scope.families_unavailable.append(family)
                    continue
                objects.extend(captured)
                edges.extend(family_edges)
                scope.families.append(family)
                scope.per_family_counts[family] = len(captured)
                if truncated:
                    scope.truncated_families.append(family)
                continue

            total, any_available = 0, False
            for member in _FAMILY_MEMBERS[family]:
                result = self._capture_spec(member, row_cap)
                if result.objects is None:
                    continue
                any_available = True
                objects.extend(result.objects)
                total += len(result.objects)
                merged += result.merged_keys
                if result.truncated:
                    scope.truncated_families.append(member)
            if any_available:
                scope.families.append(family)
                scope.per_family_counts[family] = total
            else:
                scope.families_unavailable.append(family)
        return objects, edges, merged

    def _capture_spec(self, member: str, row_cap: int) -> _MemberResult:
        """Read one family into fingerprints, one per distinct object name.

        Rows sharing a name are merged rather than allowed to overwrite each other. Two BW tables in
        this set are keyed on more than the name a snapshot uses as its identity - RSDS on
        (DATASOURCE, LOGSYS) - so taking the last row read would make the result depend on row
        order and report a phantom change between two readings of the same system. Merging makes
        each fact the sorted distinct set of its values, which is stable, and the count is reported.
        """
        spec = _FAMILY_SPECS[member]
        if not self.capability.is_available(spec.logical):
            return _MemberResult(None, False, 0)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[spec.key_column, *spec.columns],
                    from_logical=spec.logical,
                    where=list(spec.where) or None,
                    order_by=[spec.key_column],
                ),
                limit=row_cap,
            )
        )
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            name = _clean(row[0])
            if not name:
                continue
            facts = {col: _clean(val) for col, val in zip(spec.columns, row[1:], strict=False)}
            grouped.setdefault(name, []).append(
                spec.normalise_facts(facts) if spec.normalise_facts else facts
            )
        captured: list[ObjectFingerprint] = []
        for name, variants in grouped.items():
            facts = variants[0] if len(variants) == 1 else _merge_facts(variants)
            resolved = spec.type_from_facts(facts) if spec.type_from_facts else spec.object_type
            captured.append(
                ObjectFingerprint(
                    ref=BwObjectRef(object_type=cast("BwObjectType", resolved), name=name),
                    fingerprint=fingerprint_facts(facts),
                    facts=facts,
                )
            )
        merged = sum(1 for variants in grouped.values() if len(variants) > 1)
        return _MemberResult(captured, len(rows) >= row_cap, merged)

    def _capture_transformations(
        self, row_cap: int
    ) -> tuple[list[ObjectFingerprint] | None, list[SnapshotEdge], bool]:
        """Transformations, plus the dependency edges they declare.

        Keyed on `TRANID`, not on the endpoint pair. Measured on the reference system: 1,269
        active transformations across 1,261 distinct source/target pairs, so eight pairs carry
        more than one transformation and keying on the pair would silently merge them.
        """
        if not self.capability.is_available("transformation"):
            return None, [], False
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=list(_TRANSFORMATION_COLUMNS),
                    from_logical="transformation",
                    order_by=["TRANID"],
                ),
                limit=row_cap,
            )
        )
        captured: list[ObjectFingerprint] = []
        edges: list[SnapshotEdge] = []
        for row in rows:
            tranid, src_type, src_name, tgt_type, tgt_name, objstat = row[:6]
            start, end, expert, glbcode, glbcode2 = row[6:11]
            key = _clean(tranid)
            if not key:
                continue
            source = _normalise_endpoint(_clean(src_type), _clean(src_name))
            target = _normalise_endpoint(_clean(tgt_type), _clean(tgt_name))
            facts = {
                "source": source.name,
                "source_type": source.object_type,
                "target": target.name,
                "target_type": target.object_type,
                "OBJSTAT": _clean(objstat),
                # Presence, not the code id: a routine's code id changes on every re-activation
                # even when the ABAP is identical, so fingerprinting the id would report a
                # transformation as changed whenever anyone opened and saved it.
                "has_start_routine": str(bool(_clean(start))),
                "has_end_routine": str(bool(_clean(end))),
                "has_expert_routine": str(bool(_clean(expert))),
                "has_global_routine": str(bool(_clean(glbcode) or _clean(glbcode2))),
            }
            captured.append(
                ObjectFingerprint(
                    ref=BwObjectRef(object_type="transformation", name=key),
                    fingerprint=fingerprint_facts(facts),
                    facts=facts,
                )
            )
            if source.name and target.name:
                edges.append(SnapshotEdge(src=source.id, dst=target.id, kind="transformation"))
        return captured, edges, len(rows) >= row_cap


def _datasource_name(endpoint: str) -> str:
    """The DataSource out of a ``<DATASOURCE><padding><LOGSYS>`` endpoint name."""
    head = endpoint.split(maxsplit=1)
    return head[0] if head else endpoint


def _normalise_endpoint(type_code: str, name: str) -> BwObjectRef:
    """A transformation endpoint as a canonical reference, with BDLS noise removed.

    A DataSource endpoint is ``<DATASOURCE><padding><LOGSYS>``. The logical system differs between
    environments, so the raw name cannot be compared across them. Splitting on whitespace keeps the
    DataSource and drops the system - the system is captured separately by the ``datasources``
    family, where it is a fact rather than part of the identity.
    """
    resolved = normalise_object_type(type_code)
    if resolved == "datasource":
        return BwObjectRef(object_type=resolved, name=_datasource_name(name))
    return BwObjectRef(object_type=resolved, name=name)


def _digest(values: Sequence[str]) -> str:
    return hashlib.sha256("\u001f".join(sorted(values)).encode("utf-8")).hexdigest()[:16]


def _snapshot_id(system: str, taken_at: datetime) -> str:
    """Sortable and readable: profile plus a UTC stamp to the second."""
    return f"{system}-{taken_at.strftime('%Y%m%dT%H%M%SZ')}"


def summarise(snapshot: Snapshot) -> SnapshotSummary:
    return SnapshotSummary(
        snapshot_id=snapshot.snapshot_id,
        system=snapshot.system,
        taken_at=snapshot.taken_at,
        bw_release=snapshot.bw_release,
        object_count=snapshot.object_count,
        edge_count=snapshot.edge_count,
        families=list(snapshot.scope.families),
        completeness=_scope_completeness(snapshot.scope),
    )


def _scope_completeness(scope: Any) -> Completeness:
    """Which families hit the row cap during capture.

    Naming them matters more here than anywhere else: a later diff reads an object missing from a
    bounded capture as *deleted*. The families are listed so a reader can see whether the difference
    they are looking at is inside one of them (D6).
    """
    if not scope.truncated_families:
        return COMPLETE
    return Completeness(
        bounds=[
            BoundHit(bound="row_cap", scope=family, limit=scope.row_cap)
            for family in sorted(scope.truncated_families)
        ]
    )


def compare(left: Snapshot, right: Snapshot) -> SnapshotDiff:
    """Diff two snapshots. ``left`` is the baseline; ``right`` is what it is compared against.

    Restricted to the families **both** sides captured. Comparing a family only one side has would
    report its whole content as added or removed, which is a statement about the snapshots rather
    than about the systems.
    """
    comparability: list[str] = []
    comparable = True

    shared = [f for f in left.scope.families if f in right.scope.families]
    excluded = sorted(set(left.scope.families) ^ set(right.scope.families))
    if excluded:
        comparable = False
        comparability.append(
            "these families were captured on one side only and are excluded, because their whole "
            f"content would otherwise read as added or removed: {', '.join(excluded)}"
        )
    if left.bw_release != right.bw_release:
        comparable = False
        comparability.append(
            f"different releases ({left.bw_release} vs {right.bw_release}). Object models differ "
            "across releases, so an absence on one side is not necessarily a difference."
        )
    if left.capability_digest != right.capability_digest:
        comparable = False
        missing_right = sorted(set(left.available_capabilities) - set(right.available_capabilities))
        missing_left = sorted(set(right.available_capabilities) - set(left.available_capabilities))
        comparability.append(
            "the two systems can report different metadata, so absence is not evidence of "
            f"difference. Only on {left.system}: {', '.join(missing_right) or 'none'}. Only on "
            f"{right.system}: {', '.join(missing_left) or 'none'}."
        )
    if left.contract_revision != right.contract_revision:
        comparability.append(
            f"captured by different server builds ({left.contract_revision} vs "
            f"{right.contract_revision}), so a fact set may differ for reasons unrelated to the "
            "systems."
        )
    if left.scope.truncated or right.scope.truncated:
        comparability.append(
            "at least one side hit its row cap, so rows beyond the cap appear as differences "
            "rather than as unread."
        )

    types = _types_for(shared)
    left_objects = {o.id: o for o in left.objects if o.ref.object_type in types}
    right_objects = {o.id: o for o in right.objects if o.ref.object_type in types}

    added = [
        ObjectChange(ref=right_objects[k].ref, change="added")
        for k in sorted(set(right_objects) - set(left_objects))
    ]
    removed = [
        ObjectChange(ref=left_objects[k].ref, change="removed")
        for k in sorted(set(left_objects) - set(right_objects))
    ]
    changed: list[ObjectChange] = []
    for key in sorted(set(left_objects) & set(right_objects)):
        before, after = left_objects[key], right_objects[key]
        if before.fingerprint == after.fingerprint:
            continue
        changed.append(
            ObjectChange(
                ref=after.ref, change="changed", facts=_fact_changes(before.facts, after.facts)
            )
        )

    left_edges = {e.key: e for e in left.edges} if "transformations" in shared else {}
    right_edges = {e.key: e for e in right.edges} if "transformations" in shared else {}
    added_edges = [right_edges[k] for k in sorted(set(right_edges) - set(left_edges))]
    removed_edges = [left_edges[k] for k in sorted(set(left_edges) - set(right_edges))]

    rekeyed = _find_rekeyed(left_objects, right_objects, added, removed)

    caveats: list[str] = []
    unexplained = [c for c in changed if not c.facts]
    if unexplained:
        caveats.append(
            f"{len(unexplained)} object(s) changed fingerprint with no differing fact, which means "
            "the fingerprint covers something the fact list does not. That is a defect in this "
            "server, reported rather than hidden."
        )
    if rekeyed:
        caveats.append(
            f"{len(rekeyed)} of the added and removed objects connect the same two endpoints under "
            "a different technical id - the signature of a transformation or DTP built by hand in "
            "each system rather than transported. They are listed in 'rekeyed' and remain in "
            "'added' and 'removed', which stay the plain set difference. Compare endpoints, not "
            "ids, before concluding a flow is missing."
        )

    return SnapshotDiff(
        left=summarise(left),
        right=summarise(right),
        comparable=comparable,
        comparability=comparability,
        families_compared=shared,
        families_excluded=excluded,
        added=added,
        removed=removed,
        changed=changed,
        rekeyed=rekeyed,
        added_edges=added_edges,
        removed_edges=removed_edges,
        counts={
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(set(left_objects) & set(right_objects)) - len(changed),
            "rekeyed": len(rekeyed),
            "added_edges": len(added_edges),
            "removed_edges": len(removed_edges),
        },
        changed_by_fact=_changed_by_fact(changed),
        normalisations=[_LOGICAL_SYSTEM_NOTE, _VOLATILE_NOTE],
        completeness=Completeness(
            bounds=[
                *_scope_completeness(left.scope).bounds,
                *_scope_completeness(right.scope).bounds,
            ]
        ),
        caveats=caveats,
    )


_VOLATILE_NOTE = (
    "Timestamps, last-changed-by, last-used dates and record counts are excluded from every "
    "fingerprint. Including them would report every object as changed on every run, which carries "
    "the same information as reporting nothing."
)


#: Types a family can yield beyond its spec's declared one, where one table holds several kinds.
#: RSDCUBE is the case: InfoCube, MultiProvider and virtual provider share it. Missing these would
#: filter 69 of the reference system's 143 cube-table objects out of every comparison.
_EXTRA_TYPES: dict[str, tuple[str, ...]] = {
    "providers.cube": ("multiprovider", "virtualprovider"),
}


def _types_for(families: Sequence[str]) -> set[str]:
    """The canonical object types a set of families covers, for filtering both sides alike."""
    types: set[str] = set()
    for family in families:
        if family == "transformations":
            types.add("transformation")
            continue
        for member in _FAMILY_MEMBERS.get(family, ()):
            types.add(_FAMILY_SPECS[member].object_type)
            types.update(_EXTRA_TYPES.get(member, ()))
    return types


def _changed_by_fact(changed: Sequence[ObjectChange]) -> dict[str, int]:
    """How many objects each differing fact accounts for, largest first."""
    tally: dict[str, int] = {}
    for change in changed:
        for fact in change.facts:
            tally[fact.fact] = tally.get(fact.fact, 0) + 1
    return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))


def _endpoint_signature(facts: dict[str, str]) -> str | None:
    """``<source> -> <target>`` for an object that declares endpoints, else ``None``.

    Transformations record them as source/target, DTPs as SRC/TGT with TLOGO codes. Both are named
    by a generated id, which is why the endpoints are the only stable way to recognise one across
    two systems. The DataSource logical-system suffix is stripped here for the same reason it is
    stripped from an identity: it differs per environment.
    """
    if facts.get("source") and facts.get("target"):
        src, dst = facts["source"], facts["target"]
        src_type = facts.get("source_type", "unknown")
        dst_type = facts.get("target_type", "unknown")
    elif facts.get("SRC") and facts.get("TGT"):
        # Already split by `_split_endpoint_facts`; stripped again so a fact map that skipped
        # normalisation still matches rather than silently failing to.
        src, dst = _datasource_name(facts["SRC"]), _datasource_name(facts["TGT"])
        src_type = normalise_object_type(facts.get("SRCTLOGO", ""))
        dst_type = normalise_object_type(facts.get("TGTTLOGO", ""))
    else:
        return None
    return f"{src_type}:{src.upper()} -> {dst_type}:{dst.upper()}"


def _find_rekeyed(
    left_objects: dict[str, ObjectFingerprint],
    right_objects: dict[str, ObjectFingerprint],
    added: Sequence[ObjectChange],
    removed: Sequence[ObjectChange],
) -> list[RekeyedObject]:
    """Pair removals with additions that connect the same two endpoints.

    Only unambiguous pairs qualify: one removal and one addition sharing a signature. Where several
    objects connect the same endpoints - measured on a real system, eight source/target pairs carry
    more than one transformation - there is no way to say which replaced which, so none is claimed.
    """
    gone: dict[str, list[ObjectChange]] = {}
    for change in removed:
        signature = _endpoint_signature(left_objects[change.ref.id].facts)
        if signature:
            gone.setdefault(signature, []).append(change)
    arrived: dict[str, list[ObjectChange]] = {}
    for change in added:
        signature = _endpoint_signature(right_objects[change.ref.id].facts)
        if signature:
            arrived.setdefault(signature, []).append(change)

    pairs: list[RekeyedObject] = []
    for signature in sorted(set(gone) & set(arrived)):
        before, after = gone[signature], arrived[signature]
        if len(before) == 1 and len(after) == 1:
            pairs.append(
                RekeyedObject(before=before[0].ref, after=after[0].ref, matched_on=signature)
            )
    return pairs


def _fact_changes(before: dict[str, str], after: dict[str, str]) -> list[FactChange]:
    """Which facts differ, both sides shown. A fact absent on one side is reported as ``None``."""
    return [
        FactChange(fact=fact, before=before.get(fact), after=after.get(fact))
        for fact in sorted(set(before) | set(after))
        if before.get(fact) != after.get(fact)
    ]


def families_for(names: Sequence[str] | None) -> list[SnapshotFamily]:
    """Validate a caller's family list, defaulting when none is given."""
    if not names:
        return list(DEFAULT_FAMILIES)
    allowed = {*_FAMILY_MEMBERS, "transformations"}
    unknown = sorted(n for n in names if n not in allowed)
    if unknown:
        raise ValueError(f"unknown snapshot families: {unknown}; choose from {sorted(allowed)}")
    return cast("list[SnapshotFamily]", list(names))
