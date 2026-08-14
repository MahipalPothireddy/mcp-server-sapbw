"""Field-level lineage: follow one field up through the rules that populate it.

Mission Section 6 calls this "the real prize", and it is the difference between two very different
answers. Provider-level lineage says *this report reads a provider, and these DataSources feed that
provider*. Field-level lineage says *this number comes from that source field, through this rule*.
The first is a starting point for an investigation; the second ends it.

The walk, per field:

.. code-block:: text

    InfoObject in the query
      -> the transformation whose TARGET is the provider and which maps this field
      -> the rule populating it (RSTRANRULE): direct / constant / formula / routine / master-data
         read / time conversion
      -> that rule's source field(s) (RSTRANFIELD PARAMTYPE='0')
      -> repeat from the transformation's source object, now tracking the source field
      -> stop at a DataSource

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

from ..models.provenance import UnsupportedResult
from ..models.queries import FieldLineageHop, FieldLineagePath
from ..models.transformations import FieldMapping, Transformation
from ..repositories.base import Repository
from ..repositories.transformations import TransformationsRepository

# Endpoint kinds that end the walk: a DataSource is the warehouse boundary.
_BOUNDARY_KINDS = frozenset({"datasource"})
_MAX_DEPTH = 8
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
        # Per-instance memos: every field of a provider shares its inbound transformations.
        self._inbound: dict[str, list[str]] = {}
        self._mappings: dict[str, dict[str, FieldMapping]] = {}
        self._headers: dict[str, Transformation | None] = {}

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
                        f"no transformation rule populating '{current_field}' in "
                        f"'{current_object}' was found; the field may be a navigation "
                        "attribute, come from a CompositeProvider mapping, or be filled by "
                        "a start/end routine rather than a field rule"
                    )
                break
            hop, next_object, next_field, is_boundary = step
            hops.append(hop)
            resolution = "field"
            advisory = advisory or hop.advisory
            if is_boundary:
                reached = True
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
        """The rule that populates ``target_field`` in ``target_object``, and where it leads."""
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
