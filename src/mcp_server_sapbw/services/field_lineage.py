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

from ..models.composite import (
    CompositeFieldOrigin,
    composite_mapping_evidence,
    multiprovider_identification_evidence,
    reference_characteristic_evidence,
)
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
#: Inbound transformations examined per layer. 12 was chosen for transaction providers, where a
#: provider fed by more than that is a merge and sampling is enough. Master data breaks that
#: assumption, and D32 brings master data into scope: the characteristic the customer documented has
#: **13** active inbound transformations - 7 direct from DataSources, 4 through InfoSources, one
#: from an ADSO, and flat-file loads - so a cap of 12 dropped one. Wide fan-in is normal for an
#: InfoObject whose attributes come from several systems, so the cap is set above the measured worst
#: case rather than at it.
_MAX_INBOUND_PER_LAYER = 24
_ROUTINE_RULES = frozenset({"routine", "formula"})


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _datasource_name(name: str) -> str:
    """The DataSource without its logical-system suffix.

    BW stores a DataSource endpoint as the name padded to 30 characters and suffixed with its
    logical system, so ``RSTRAN.SOURCENAME`` reads ``DS_SALES<spaces>SRC100``. Reporting that
    verbatim as an object name was tolerable while DataSource endpoints were rare; D32 makes them
    the common terminus, because master-data lineage lands on one at nearly every branch. The
    logical system is dropped rather than parsed into a field here for the reason ``snapshot``
    gives: BDLS rewrites it per landscape, so it is not part of the DataSource's identity.
    """
    parts = name.split()
    return parts[0] if parts else name


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
        #: upper-cased field name -> (characteristic, attribute), or None when it is not a stored
        #: navigation attribute. Cached the same way and for the same reason: a query's fields are
        #: asked about repeatedly, and "not a navigation attribute" is an answer worth keeping.
        self._nav_attributes: dict[str, tuple[str | None, str | None] | None] = {}
        #: characteristic -> the one it references (``RSDCHA.CHABASNM``) when that differs
        #: from its own name, else ``None``. A reference characteristic owns no master data: its
        #: attribute, SID, text and view tables all belong to the referenced one, and nothing loads
        #: into it, so a walk that arrives at one has to continue there (D35).
        self._reference: dict[str, str | None] = {}
        #: provider -> ``{provider_field: [(part, part_field), ...]}`` from the MultiProvider's
        #: InfoObject identification, or ``None`` when the object is not a MultiProvider. Loaded
        #: whole on first touch: one statement per provider rather than one per field, because a
        #: query asks about dozens of fields on the same provider (D22).
        self._multi_identification: dict[str, dict[str, list[tuple[str, str]]] | None] = {}

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
                    reason = self._unresolved_reason(current_object, current_field)
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

        The MultiProvider route is third, and the order is safe rather than lucky: **no**
        transformation on the reference system targets a MultiProvider (measured 0 of 1,274 active
        transformations), which is what BW's model implies - a MultiProvider stores no data, so
        nothing can load into it. Trying the rule route first therefore cannot shadow it.

        The reference-characteristic route is last, because it is the only one that is a statement
        about the *object* rather than the *field*. Placing it after the field-specific routes means
        it can only rescue a walk that was about to stop.
        """
        rule_hop = self._transformation_hop(target_object, target_field)
        if rule_hop is not None:
            return rule_hop
        composite_hop = self._composite_hop(target_object, target_field)
        if composite_hop is not None:
            return composite_hop
        multi_hop = self._multi_hop(target_object, target_field)
        if multi_hop is not None:
            return multi_hop
        nav_hop = self._nav_attribute_hop(target_field)
        if nav_hop is not None:
            return nav_hop
        # Last, deliberately: this hop says nothing about the *field*, only that the *object* holds
        # no data of its own. Trying it after the field-specific routes means it can only convert a
        # stall into a continuation, never redirect a walk that was already resolving (D35).
        return self._reference_characteristic_hop(target_object, target_field)

    def _unresolved_reason(self, current_object: str, current_field: str) -> str:
        """Why the walk stopped at hop zero, claiming only what was actually checked.

        The navigation-attribute possibility is no longer offered as a guess when it has been ruled
        out: ``_nav_attribute_hop`` consults ``RSDATRNAV`` before this point, so on a release that
        has the table, reaching here means the field is *not* one (D23). On a release without it the
        check could not be made, and the message says that instead - asserting "it is not a
        navigation attribute" from a table that was never read would replace one wrong claim with
        another.
        """
        # A MultiProvider that identifies other fields but not this one is a *specific* answer, not
        # a general miss: BW says no part supplies it. Say that, and cite the table, rather than
        # falling through to the generic "no rule found" wording (D22).
        identification = self._identification(current_object)
        if identification and current_field.upper() not in identification:
            table = self.physical("multiprovider_identification") or "RSDICMULTIIOBJ"
            return (
                f"'{current_object}' is a MultiProvider and its InfoObject identification "
                f"({table}) maps {len(identification)} other field(s) to parts but has no row for "
                f"'{current_field}', so BW does not record any part as supplying it; check whether "
                "the field is used by this query at all, or is a navigation attribute of a "
                "characteristic the provider does carry"
            )
        head = (
            f"no rule, CompositeProvider mapping or MultiProvider identification populating "
            f"'{current_field}' in '{current_object}' was found"
        )
        tail = "it may be filled by a start/end routine rather than by a field rule"
        if self.capability.is_available("nav_attribute"):
            table = self.physical("nav_attribute") or "RSDATRNAV"
            return (
                f"{head}, and it is not a navigation attribute "
                f"({table} has no row for it); {tail}"
            )
        return (
            f"{head}. Whether it is a navigation attribute could not be checked: "
            f"RSDATRNAV is absent on this release, so if it is one its lineage continues "
            f"through master data and is not reported here. Otherwise {tail}"
        )

    def _nav_attribute_hop(
        self, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """Turn a navigation attribute into the hop it really is (D23, D32).

        A navigation attribute is not populated into the provider by any transformation rule. Its
        value is read at query time from the master data of the characteristic it hangs off, so both
        of the routes above are looking somewhere the answer cannot be - and the walk used to stop
        here and report "no rule ... the field **may be** a navigation attribute".

        Two things were wrong with that. It is decidable, not a maybe: ``RSDATRNAV`` says so in one
        read of a table the capability layer already resolves. And having decided, the lineage
        continues - from the base characteristic, through its own inbound transformations, to the
        DataSources that load its attributes. Measured on the characteristic the customer supplied,
        that continuation is 22 transformations over 3 hops reaching 10 DataSources in 3 source
        systems; none of it was reported. On the subject query this affects 16 of 45 paths.

        The name is matched against stored ``ATRNAVNM`` rather than split on ``__``, for the reason
        ``ProvidersRepository._annotate_nav_attribute_fields`` gives: the convention holds for 4,127
        of 4,129 rows on the reference system, and splitting would invent a characteristic for the
        other two.
        """
        resolved = self._nav_attribute(target_field)
        if resolved is None:
            return None
        base, attribute = resolved
        if not base or not attribute:
            return None
        hop = FieldLineageHop(
            object_name=base,
            object_type="infoobject",
            via="nav_attribute",
            advisory=False,  # BW's own stored attribute model, read whole
            target_field=target_field,
            rule_type="navigation_attribute",
            source_fields=[attribute],
            note=(
                f"'{target_field}' is a navigation attribute: attribute '{attribute}' of "
                f"characteristic '{base}', declared in "
                f"{self.physical('nav_attribute') or 'RSDATRNAV'}. Its value is not stored in the "
                "provider and no transformation rule populates it there - it is read at query time "
                f"from '{base}' master data, so the lineage continues through the transformations "
                f"that load '{base}'."
            ),
        )
        return hop, base, attribute, False

    def _nav_attribute(self, name: str) -> tuple[str | None, str | None] | None:
        """``(characteristic, attribute)`` when ``name`` is a stored navigation attribute."""
        key = name.strip().upper()
        if key in self._nav_attributes:
            return self._nav_attributes[key]
        if not self.capability.is_available("nav_attribute"):
            self._nav_attributes[key] = None
            return None
        rows = self.select(
            self.dialect.build_select(
                columns=["CHANM", "ATTRINM"],
                from_logical="nav_attribute",
                where=["ATRNAVNM = ?"],
                params=[name.strip()],
            )
        )
        resolved: tuple[str | None, str | None] | None = None
        for chanm, attrinm in rows:
            resolved = (_clean(chanm), _clean(attrinm))
            break
        self._nav_attributes[key] = resolved
        return resolved

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

    # --- MultiProvider (D22) ------------------------------------------------------------

    def _multi_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """The MultiProvider part that supplies ``target_field``, from BW's own identification.

        A MultiProvider holds no data and has no inbound transformation: it unions its parts. So the
        field's origin is whichever part supplies it, and BW records that in ``RSDICMULTIIOBJ`` as
        ``(provider, provider_field, part) -> part_field``.

        The part field is read, never assumed equal to the provider's. On the reference system 66 of
        5,372 active identification rows map a field to a **differently named** field in the part,
        including navigation-attribute forms (``0COSTCENTER`` -> ``0ASSET__0COSTCENTER``), and the
        subject provider alone carries 20. A same-name shortcut would be right most of the time and
        silently wrong the rest - the failure mode this project keeps finding, and the reason the
        mapping is consulted rather than inferred.

        Fans out exactly as the CompositeProvider hop does, because a union has the same shape:
        every supplying part is reported in ``source_objects`` and the chain follows the first in
        sorted order, stated as a choice in ``note`` rather than left implicit.
        """
        identification = self._identification(target_object)
        if not identification:
            return None
        origins = identification.get(target_field.upper())
        if not origins:
            return None
        ordered = sorted(origins)
        part_name, part_field = ordered[0]
        others = sorted({p for p, _f in ordered})
        renamed = part_field.upper() != target_field.upper()
        hop = FieldLineageHop(
            object_name=part_name,
            object_type="multiprovider_part",
            via="multiprovider_part",
            advisory=False,  # BW's own stored identification, read row by row
            target_field=target_field,
            rule_type="multiprovider_identification",
            source_fields=[part_field],
            source_objects=others if len(others) > 1 else [],
            evidence=multiprovider_identification_evidence(renamed=renamed),
            note=self._multi_note(target_object, target_field, ordered, renamed),
        )
        # A part is an ordinary provider (cube, DSO, ADSO), never a DataSource, so this is not a
        # boundary: the walk continues into the part's own inbound transformation.
        return hop, part_name, part_field, False

    @staticmethod
    def _multi_note(
        provider: str,
        target_field: str,
        ordered: list[tuple[str, str]],
        renamed: bool,
    ) -> str:
        """Say what the union means, and flag a rename because it changes what to look for."""
        rename_clause = (
            f" Identification renames it to '{ordered[0][1]}' inside "
            f"'{ordered[0][0]}', so that is the field to look for there, not '{target_field}'."
            if renamed
            else ""
        )
        if len(ordered) == 1:
            return (
                f"'{provider}' is a MultiProvider, which stores no data of its own; a single part "
                f"supplies this field.{rename_clause}"
            )
        names = ", ".join(f"{p}.{f}" for p, f in ordered)
        return (
            f"'{provider}' is a MultiProvider, which stores no data of its own: {len(ordered)} "
            f"parts each supply this field for some of its rows ({names}). This chain follows "
            f"{ordered[0][0]}; source_objects lists them all, and the others are traced by asking "
            f"for the same field on each part.{rename_clause}"
        )

    # --- reference characteristic (D35) --------------------------------------------------

    def _reference_characteristic_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """Continue at the characteristic ``target_object`` references, if it references one.

        A *reference characteristic* owns no master data at all. Its attribute table, SID table,
        text table and view all belong to the characteristic it references, and **nothing loads into
        it** - measured: the subject's reference characteristic has 0 inbound
        transformations while the one it references has 10, of which 2 populate the very field the
        walk was looking for. So a walk that arrives at one is not finished, it is one lookup short.

        Fires only when ``CHABASNM`` differs from the characteristic's own name. For an ordinary
        characteristic BW stores the two as equal (6,578 of 8,879 on the reference system), and
        following that would make every characteristic walk cycle through itself.

        The field name carries across unchanged, because the reference shares the base's attribute
        table - the attribute is physically a column of the referenced characteristic's master data,
        which is exactly why the walk can continue there at all.
        """
        base = self._reference_of(target_object)
        if base is None:
            return None
        hop = FieldLineageHop(
            object_name=base,
            object_type="characteristic",
            via="reference_characteristic",
            advisory=False,  # RSDCHA states it; nothing is inferred
            target_field=target_field,
            rule_type="reference_characteristic",
            source_fields=[target_field],
            evidence=reference_characteristic_evidence(),
            note=(
                f"'{target_object}' is a reference characteristic of '{base}': it holds no master "
                f"data of its own, so no transformation loads into it and its attributes are "
                f"physically columns of '{base}'. The walk continues there, which is where "
                f"'{target_field}' is actually populated."
            ),
        )
        return hop, base, target_field, False

    def _reference_of(self, characteristic: str) -> str | None:
        """The characteristic ``characteristic`` references, or ``None`` when it references none.

        ``None`` is cached as an answer, not a miss: a query asks about dozens of fields and the
        common case - an ordinary characteristic that references itself - should cost one lookup.
        """
        if characteristic in self._reference:
            return self._reference[characteristic]
        resolved: str | None = None
        if self.capability.is_available("characteristic"):
            rows = self.select(
                self.dialect.build_select(
                    columns=["CHABASNM"],
                    from_logical="characteristic",
                    where=["CHANM = ?"],
                    params=[characteristic],
                )
            )
            if rows:
                base = str(rows[0][0] or "").strip()
                if base and base.upper() != characteristic.strip().upper():
                    resolved = base
        self._reference[characteristic] = resolved
        return resolved

    def _identification(self, provider: str) -> dict[str, list[tuple[str, str]]] | None:
        """``{provider_field: [(part, part_field)]}`` for a MultiProvider, else ``None``.

        Loaded whole on first touch and memoised, including the negative answer: a query asks about
        dozens of fields on one provider, so "not a MultiProvider" is worth one lookup rather than
        one per field. An empty identification is stored as ``None`` for the same reason.
        """
        if provider in self._multi_identification:
            return self._multi_identification[provider]
        loaded: dict[str, list[tuple[str, str]]] | None = None
        if self.capability.is_available("multiprovider_identification"):
            rows = self.select(
                self.dialect.build_select(
                    columns=["IOBJNM", "PARTCUBE", "PARTIOBJ"],
                    from_logical="multiprovider_identification",
                    where=["INFOCUBE = ?"],
                    params=[provider],
                )
            )
            collected: dict[str, list[tuple[str, str]]] = {}
            for iobjnm, partcube, partiobj in rows:
                field = str(iobjnm).strip().upper()
                part = str(partcube).strip()
                part_field = str(partiobj).strip()
                # PARTIOBJ is never blank on the reference system (0 of 5,372), so a missing part
                # field would be an unexpected shape: skip it rather than invent a same-name
                # fallback that would look like a resolved hop.
                if not field or not part or not part_field:
                    continue
                collected.setdefault(field, []).append((part, part_field))
            loaded = collected or None
        self._multi_identification[provider] = loaded
        return loaded

    def _transformation_hop(
        self, target_object: str, target_field: str
    ) -> tuple[FieldLineageHop, str | None, str | None, bool] | None:
        """The transformation rule that populates ``target_field`` in ``target_object``.

        Fans out the same way the CompositeProvider hop does, and for a reason D32 made pressing: an
        InfoObject's attributes are routinely loaded by several transformations from several
        systems, so "the transformation populating this field" is often more than one. Every one is
        in ``source_objects`` and the chain follows the first in sorted order - a deterministic
        choice, stated as a choice. Following one branch and not saying so is what would turn a
        newly-reachable lineage into a newly-misleading one.
        """
        candidates: list[tuple[str, Any]] = []
        for tran_id in self._inbound_transformations(target_object):
            mapping = self._mappings_for(tran_id).get(target_field.upper())
            if mapping is not None and self._header(tran_id) is not None:
                candidates.append((tran_id, mapping))
        if not candidates:
            return None

        def order(candidate: tuple[str, Any]) -> tuple[int, str, str]:
            """Sort key: follow a branch that can carry the lineage onward, then by name.

            Sorting at all makes ``object_name`` match what the note promises - it was previously
            whichever transformation ``RSTRAN`` returned first by id while ``source_objects`` was
            sorted, so the two disagreed about which branch the chain followed.

            The ordering then prefers branches that can *continue*. Two kinds cannot, and both were
            observed being followed on the subject query's attributes:

            * a **self-transformation**, where the object is loaded from itself. Following it can
              only reach the cycle guard, and it did - the walk reported "cycle detected" while the
              standard attribute DataSource sat unused in the same fan-out.
            * a **constant** rule, which fixes the field to a literal and so has no source field to
              follow. Alphabetical order alone picked one of these over a direct rule carrying a
              real source field, turning an informative answer into a terminal one.

            Both stay in ``source_objects``, because both exist. They are simply not the branch
            worth following when a branch that continues is available. Which of the *continuing*
            branches gets followed remains arbitrary - that is inherent to a fan-out - which is why
            the note says so and names them all.
            """
            tid, mapping = candidate
            header = self._header(tid)
            source = header.source if header is not None else None
            name = self._source_name(source) if source is not None else None
            terminal = 0
            if (name or "").upper() == target_object.upper():
                terminal = 1  # self-reference: cannot advance
            elif not mapping.source_fields:
                terminal = 1  # constant or otherwise sourceless: nothing above it to follow
            return (terminal, name or "\uffff", tid)

        candidates.sort(key=order)
        siblings = sorted(
            {
                name
                for tid, _m in candidates
                if (header := self._header(tid)) is not None
                and header.source is not None
                and (name := self._source_name(header.source))
            }
        )
        for tran_id, mapping in candidates:
            header = self._header(tran_id)
            if header is None:
                continue
            source = header.source
            source_name = self._source_name(source) if source is not None else None
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
                source_objects=siblings if len(siblings) > 1 else [],
                note=self._transformation_note(advisory, siblings, source_name),
            )
            next_field = mapping.source_fields[0] if mapping.source_fields else None
            return hop, source_name, next_field, is_boundary
        return None

    @staticmethod
    def _source_name(source: Any) -> str | None:
        """A transformation's source object name, with a DataSource's logical suffix removed."""
        raw = (source.name or "").strip()
        if not raw:
            return None
        return _datasource_name(raw) if source.kind in _BOUNDARY_KINDS else raw

    @staticmethod
    def _transformation_note(
        advisory: bool, siblings: list[str], chosen: str | None
    ) -> str | None:
        """Say both things that can be true of this hop: it fans out, and it may be advisory."""
        parts: list[str] = []
        if len(siblings) > 1:
            parts.append(
                f"{len(siblings)} transformations populate this field, each the source of some of "
                f"its rows ({', '.join(siblings)}). This chain follows "
                f"{chosen or '(unresolved)'}; source_objects lists them all, and the others are "
                "traced by asking for the same field on each source"
            )
        if advisory:
            parts.append(
                "populated by a routine: BW records the rule, but what the ABAP reads is a "
                "heuristic lower bound"
            )
        return ". ".join(parts) if parts else None

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
