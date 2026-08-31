"""Field-level lineage: follow one field up through the rules that populate it.

Mission Section 6 calls this "the real prize", and it is the difference between two very different
answers. Provider-level lineage says *this report reads a provider, and these DataSources feed that
provider*. Field-level lineage says *this number comes from that source field, through this rule*.
The first is a starting point for an investigation; the second ends it.

The walk, per field:

.. code-block:: text

    InfoObject in the query
      -> if the object is a CompositeProvider: the declared model on RSOHCPR names the part that
         supplies this element, and the field it supplies it from
      -> otherwise the transformation whose TARGET is the object and which maps this field
      -> the rule populating it (RSTRANRULE): direct / constant / formula / routine / master-data
         read / time conversion
      -> that rule's source field(s) (RSTRANFIELD PARAMTYPE='0')
      -> repeat from the transformation's source object, now tracking the source field
      -> stop at a DataSource

**The CompositeProvider hop is not an extra; it is the one that made this reader work at all.** A
CompositeProvider has no transformation, so the transformation route finds nothing and the walk used
to end at hop zero for every field of every CompositeProvider-based query - measured on a production
query, 100 of 100 InfoObjects. Since a CompositeProvider is what a BEx query normally reads on
BW-on-HANA, that was most of the subject matter. The mapping is declared metadata (mission Section 6
asks for it by name: "for CompositeProviders: which part-provider supplies it"), so this hop is
``observed`` rather than advisory.

Two honesty properties matter more than coverage:

* A hop through a **routine** is marked advisory. BW records that a routine populates the field, but
  what the ABAP reads is a heuristic lower bound, so the chain is real while its inputs are not
  certain.
* When no rule for a field can be found, the path says so and gives a reason rather than silently
  returning the provider's upstream objects as though they were the field's. Field lineage that
  cannot distinguish itself from provider lineage is worse than none: it invites conclusions the
  data does not support.

Cost is bounded by ``max_depth`` and by memoising per call. Each layer needs the inbound
transformations of one object plus their field mappings, and those are shared across every field of
the same provider — with the transformation cache underneath, a whole query costs a few reads.
"""

from __future__ import annotations

from typing import Any

from ..models.composite import CompositeFieldOrigin, composite_mapping_evidence
from ..models.provenance import UnsupportedResult
from ..models.queries import FieldLineageHop, FieldLineagePath
from ..models.transformations import FieldMapping, Transformation
from ..repositories.base import Repository
from ..repositories.providers import ProvidersRepository
from ..repositories.transformations import TransformationsRepository

# Endpoint kinds that end the walk: a DataSource is the warehouse boundary.
_BOUNDARY_KINDS = frozenset({"datasource"})
#: A calculation-view part is a boundary of a different kind: the field really does come from there,
#: but its own lineage is in the HANA catalogue, not in BW's transformation graph. Reported as a
#: named stop carrying the tool that continues it, not as a failure to resolve.
_HANA_BOUNDARY_KINDS = frozenset({"calcview"})
#: Depth is per *layer*, and a CompositeProvider hop consumes one without moving through a
#: transformation, so the stack a CompositeProvider sits on needs headroom above the DSO layers.
#: Measured: query -> CompositeProvider -> ADSO -> DSO -> DSO -> DataSource is five, and a stacked
#: model adds one per internal node.
_MAX_DEPTH = 12
_MAX_INBOUND_PER_LAYER = 12  # a provider fed by more than this is a merge; sampling is enough
_ROUTINE_RULES = frozenset({"routine", "formula"})


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class FieldLineageService(Repository):
    """Walks a single field upward through transformation rules."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._transformations = TransformationsRepository(connection, capability, cache)
        self._providers = ProvidersRepository(connection, capability, cache)
        # Per-instance memos: every field of a provider shares its inbound transformations.
        self._inbound: dict[str, list[str]] = {}
        self._mappings: dict[str, dict[str, FieldMapping]] = {}
        self._headers: dict[str, Transformation | None] = {}
        #: object -> its declared CompositeProvider model, or None when it is not one. ``None`` is a
        #: cached answer rather than a miss, so "this is not a CompositeProvider" costs one lookup
        #: for the whole query instead of one LOB probe per field.
        self._composite: dict[str, Any] = {}

    # --- public -------------------------------------------------------------------------

    def trace_field(
        self, provider: str, field: str, *, max_depth: int = _MAX_DEPTH
    ) -> FieldLineagePath:
        """Follow ``field`` from ``provider`` toward a DataSource, rule by rule."""
        hops: list[FieldLineageHop] = [
            FieldLineageHop(object_name=provider, object_type="provider", via="provider")
        ]
        current_object, current_field = provider, field
        advisory = False
        reached = False
        resolution: str = "none"
        reason: str | None = None
        visited: set[tuple[str, str]] = {(provider.upper(), field.upper())}

        for _ in range(max(1, min(max_depth, _MAX_DEPTH))):
            step = self._one_hop(current_object, current_field)
            if step is None:
                if resolution == "none":
                    reason = (
                        f"no rule or CompositeProvider mapping populating '{current_field}' in "
                        f"'{current_object}' was found; the field may be a navigation attribute, "
                        "or be filled by a start/end routine rather than by a field rule"
                    )
                else:
                    reason = (
                        f"the walk reached '{current_object}' but found no rule populating "
                        f"'{current_field}' there, so the chain above this point is complete and "
                        "below it is unknown"
                    )
                break
            hop, next_object, next_field, is_boundary = step
            hops.append(hop)
            resolution = "field"
            advisory = advisory or hop.advisory
            if is_boundary:
                reached = True
                break
            if hop.via == "calc_view":
                # A real answer, not a dead end: the field comes from this calculation view, whose
                # own lineage is in the HANA catalogue rather than in BW's transformation graph.
                reason = (
                    f"the field comes from calculation view '{hop.object_name}', which is outside "
                    "BW's transformation graph; continue with bw_get_calc_view_lineage or "
                    "bw_get_calc_view_logic for its base tables and its logic"
                )
                break
            if next_object is None or next_field is None:
                reason = (
                    f"rule '{hop.rule_type}' for '{hop.target_field}' has no source field to "
                    "follow (a constant, or derived without a field input)"
                )
                break
            key = (next_object.upper(), next_field.upper())
            if key in visited:
                reason = f"cycle detected at '{next_object}'.'{next_field}'; walk stopped"
                break
            visited.add(key)
            current_object, current_field = next_object, next_field

        return FieldLineagePath(
            iobjnm=field,
            provider=provider,
            hops=hops,
            reaches_datasource=reached,
            has_routine_hop=advisory,
            resolution="field" if resolution == "field" else "none",
            unresolved_reason=reason,
            provenance=self.provenance("transformation_field", {"FIELDNM": field}),
        )

    # --- one layer ----------------------------------------------------------------------

    def _one_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """One layer up from ``target_field`` in ``target_object``, however BW declares it.

        The transformation route is tried first because it is the one carrying a *rule* - how the
        field was derived, not only where it came from. A CompositeProvider has no transformation at
        all, so the declared model is its only route; trying it second costs nothing on the common
        path, since the memo answers "not a CompositeProvider" once per object.
        """
        rule_hop = self._transformation_hop(target_object, target_field)
        if rule_hop is not None:
            return rule_hop
        return self._composite_hop(target_object, target_field)

    def _composite_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """The CompositeProvider part that supplies ``target_field``, from the declared model.

        Fans out by nature: a union is normally fed the same element by several parts. Every one is
        reported in ``source_objects`` and the chain follows the first in sorted order - a
        deterministic choice, stated as a choice, rather than whichever part the model happened to
        list first. Following one branch and *not* saying so is what would make this misleading.
        """
        model = self._composite_model(target_object)
        if model is None:
            return None
        origins = model.resolve_field(target_field)
        if not origins:
            return None
        ordered = sorted(origins, key=lambda o: (o.part_name, o.source_field or ""))
        chosen = ordered[0]
        others = sorted({o.part_name for o in ordered})
        is_boundary = chosen.part_kind in _BOUNDARY_KINDS
        is_hana = chosen.part_kind in _HANA_BOUNDARY_KINDS
        hop = FieldLineageHop(
            object_name=chosen.runtime_view_name or chosen.part_name,
            object_type=chosen.part_kind,
            via="calc_view" if is_hana else "composite_part",
            advisory=False,  # BW's own stored model, read whole
            target_field=target_field,
            rule_type=(
                "composite_constant" if chosen.mapping_kind == "constant" else "composite_mapping"
            ),
            source_fields=[chosen.source_field] if chosen.source_field else [],
            source_objects=others if len(others) > 1 else [],
            via_aliases=list(chosen.via_aliases),
            evidence=composite_mapping_evidence(chosen.part_kind),
            note=self._composite_note(model.node_type, ordered, chosen),
        )
        if is_hana:
            return hop, None, None, False
        return hop, chosen.part_name, chosen.source_field, is_boundary

    @staticmethod
    def _composite_note(
        node_type: str | None, ordered: list[CompositeFieldOrigin], chosen: CompositeFieldOrigin
    ) -> str:
        """Say what the fan-out means, in the terms the node type makes true."""
        combine = "union" if (node_type or "").lower().endswith("union") else "node"
        if chosen.mapping_kind == "constant":
            return (
                "the CompositeProvider model fixes this element as a constant for this part, so "
                "there is no source field above it to follow"
            )
        if len(ordered) <= 1:
            return "declared in the CompositeProvider's stored model; a single part supplies it"
        names = ", ".join(o.part_name for o in ordered)
        return (
            f"{len(ordered)} parts supply this element through the CompositeProvider's {combine}, "
            f"each equally the source of some of its rows ({names}). This chain follows "
            f"{chosen.part_name}; source_objects lists them all, and the others are traced by "
            "asking for the same field on each part."
        )

    def _composite_model(self, name: str) -> Any:
        """The object's declared CompositeProvider model, or ``None`` when it is not one."""
        if name in self._composite:
            return self._composite[name]
        model = self._providers.composite_model(name)
        resolved = model if (model is not None and model.parsed and model.inputs) else None
        self._composite[name] = resolved
        return resolved

    def _transformation_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """The transformation rule that populates ``target_field`` in ``target_object``."""
        for tran_id in self._inbound_transformations(target_object):
            mapping = self._mappings_for(tran_id).get(target_field.upper())
            if mapping is None:
                continue
            header = self._header(tran_id)
            if header is None:
                continue
            source = header.source
            source_name = source.name.strip() if source is not None else None
            source_kind = source.kind if source is not None else "other"
            is_boundary = source_kind in _BOUNDARY_KINDS
            advisory = mapping.rule_type in _ROUTINE_RULES
            hop = FieldLineageHop(
                object_name=source_name or "(unresolved)",
                object_type=source_kind,
                via="datasource" if is_boundary else "transformation",
                advisory=advisory,
                target_field=target_field,
                rule_type=mapping.rule_type,
                source_fields=list(mapping.source_fields),
                transformation_id=tran_id,
                routine_code_id=mapping.routine_code_id,
                note=(
                    "populated by a routine: BW records the rule, but what the ABAP reads is a "
                    "heuristic lower bound"
                    if advisory
                    else None
                ),
            )
            next_field = mapping.source_fields[0] if mapping.source_fields else None
            return hop, source_name, next_field, is_boundary
        return None

    # --- memoised lookups ---------------------------------------------------------------

    def _inbound_transformations(self, target: str) -> list[str]:
        """Active transformations whose target is ``target`` (memoised per instance)."""
        cached = self._inbound.get(target)
        if cached is not None:
            return cached
        if not self.capability.is_available("transformation"):
            self._inbound[target] = []
            return []
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["TRANID"],
                    from_logical="transformation",
                    where=["TARGETNAME = ?"],
                    params=[target],
                    order_by=["TRANID"],
                ),
                limit=_MAX_INBOUND_PER_LAYER,
            )
        )
        resolved = [str(row[0]).strip() for row in rows if _clean(row[0])]
        self._inbound[target] = resolved
        return resolved

    def _mappings_for(self, tran_id: str) -> dict[str, FieldMapping]:
        """Target field (upper-cased) -> the mapping that populates it."""
        cached = self._mappings.get(tran_id)
        if cached is not None:
            return cached
        header = self._header(tran_id)
        index: dict[str, FieldMapping] = {}
        if header is not None:
            for mapping in header.field_mappings:
                for name in mapping.target_fields:
                    key = name.strip().upper()
                    if key and key not in index:
                        index[key] = mapping
        self._mappings[tran_id] = index
        return index

    def _header(self, tran_id: str) -> Transformation | None:
        if tran_id in self._headers:
            return self._headers[tran_id]
        result = self._transformations.get_transformation(tran_id)
        header = None if isinstance(result, UnsupportedResult) else result
        self._headers[tran_id] = header
        return header
