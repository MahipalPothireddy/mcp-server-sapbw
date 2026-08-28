"""One vocabulary for how strongly a fact is established.

**The problem this solves.** The server was honest about uncertainty from the start, but in seven
separate vocabularies: a part provider was ``confirmed``/``advisory``, a lineage edge
``exact``/``advisory``, a decoded code ``dictionary``/``cross_domain``/``advisory``, a source-system
kind ``dictionary``/``advisory`` under a differently named field, a table dependency
``confirmed``/``advisory``, a HANA crossing ``bic_table``/``bw_provider_view``/``unresolved``, a
field-lineage path ``field``/``provider``/``none``, a routine analysis ``lower_bound``, and a chain
cadence ``high``/``low``. Each was locally reasonable. Together they were not comparable: a caller
could not sort a mixed set of findings by how much to trust them, and "advisory" meant a naming
convention in one place and a heuristic ABAP parse in another.

:class:`Evidence` adds the comparable axis without discarding the per-mechanism detail. ``basis``
is the four-value scale everything maps onto; ``method`` keeps the specific mechanism verbatim, so
nothing is flattened; ``detail`` answers "why did you conclude this" in a sentence; ``completeness``
is orthogonal - whether the set is exhaustive is a different question from whether each member is
right.

The legacy fields stay where they are. They are published tool schemas, and removing them would
break a caller to gain nothing: ``evidence`` is derived from them at the same point they are set, by
the single mapping in :data:`_MAPPING`, which is the one place to review how a code becomes a basis.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How a fact was established, from strongest to weakest. The distinction is deliberately about
#: *how the conclusion was reached*, not about how likely it feels:
#:
#: ``observed``
#:     A metadata row states it. "RSTRAN says this transformation's source is X."
#: ``derived``
#:     Computed from rows by a documented rule - a join, a decode against the ABAP dictionary, an
#:     aggregation of run history. Every input was read; the conclusion was assembled.
#: ``inferred``
#:     Rests on a naming convention, a name shape, or a heuristic parse of ABAP. Can be wrong even
#:     when every input was read correctly. This is the class that must never be presented as fact.
#: ``unknown``
#:     Could not be established. Reported as such rather than omitted.
EvidenceBasis = Literal["observed", "derived", "inferred", "unknown"]

#: Whether the set a fact belongs to is exhaustive. Orthogonal to :data:`EvidenceBasis`: a routine's
#: table reads are each ``inferred`` *and* the list is a ``lower_bound``, which are two separate
#: statements, and collapsing them loses one.
EvidenceCompleteness = Literal["complete", "lower_bound", "unknown"]

#: Basis ordering, so a caller can sort or filter a mixed set of findings. Lower is stronger.
BASIS_RANK: dict[str, int] = {"observed": 0, "derived": 1, "inferred": 2, "unknown": 3}


class Evidence(BaseModel):
    """How one fact was established, in terms comparable across every subsystem."""

    model_config = ConfigDict(extra="forbid")

    basis: EvidenceBasis
    #: The specific mechanism, kept per-domain so unification costs no precision. Stable strings,
    #: safe to match on: e.g. ``"bw_provider_view"``, ``"bic_table_naming"``,
    #: ``"dictionary_domain"``, ``"routine_select_parse"``, ``"observed_run_history"``.
    method: str
    #: One sentence saying why this conclusion was reached, for a reader who did not write the code.
    detail: str | None = None
    completeness: EvidenceCompleteness = "complete"
    #: The legacy vocabulary and code this was mapped from, so the translation is auditable and a
    #: caller migrating off the old field can see exactly what it corresponded to.
    mapped_from: str | None = None

    @property
    def rank(self) -> int:
        """Sort key: 0 strongest (observed) to 3 weakest (unknown)."""
        return BASIS_RANK.get(self.basis, len(BASIS_RANK))

    @property
    def is_advisory(self) -> bool:
        """True when the fact rests on a convention or a heuristic and could be wrong."""
        return self.basis in {"inferred", "unknown"}


class EvidenceSummary(BaseModel):
    """Counts by basis over a set of facts, so a result can say how much of it is inferred.

    The number that matters is ``inferred``: a graph of 200 edges is a different object depending on
    whether 2 or 150 of them rest on a routine parse, and a count is the cheapest way to say so.
    """

    model_config = ConfigDict(extra="forbid")

    observed: int = 0
    derived: int = 0
    inferred: int = 0
    unknown: int = 0
    #: Distinct methods seen, so "how" is answerable without walking every item.
    methods: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return self.observed + self.derived + self.inferred + self.unknown

    @property
    def advisory_count(self) -> int:
        return self.inferred + self.unknown


# --- the single mapping from every legacy vocabulary to a canonical Evidence -----------------
#
# Keyed ``(vocabulary, code)``. The vocabulary name is the model field it came from, so a reviewer
# can trace any entry back to its origin. Every value that vocabulary can hold appears here; an
# unmapped code degrades to ``unknown`` rather than being silently treated as a fact.
_MAPPING: dict[tuple[str, str], tuple[EvidenceBasis, str, str]] = {
    # PartProviderRef.confidence - how a CompositeProvider's part was resolved.
    ("part_provider", "confirmed"): (
        "derived",
        "calc_view_dependency_confirmed",
        "Resolved from the generated calc view's base tables and confirmed against the provider "
        "catalogue, so the table -> object reading names an object that exists.",
    ),
    ("part_provider", "advisory"): (
        "inferred",
        "bic_table_naming",
        "Read from the generated table's /BIC/ name by convention; the provider catalogue did not "
        "confirm it, so the object may not exist under that name.",
    ),
    # LineageEdge.confidence - whether an edge is declared or parsed out of ABAP.
    ("lineage_edge", "exact"): (
        "observed",
        "declared_metadata",
        "A metadata row declares this edge: a transformation, DTP, MultiProvider part or "
        "CompositeProvider part states the source and target directly.",
    ),
    # How a consumer was resolved from a generated calc view. Distinct from the routine-parse
    # vocabulary below, and the distinction is defect D10: an edge read out of
    # SYS.OBJECT_DEPENDENCIES described itself as parsed out of ABAP. Both are advisory, but for
    # different reasons and with different follow-up, so a caller deciding how far to trust an edge
    # needs the mechanism that actually produced it.
    ("calc_view_consumer", "provider"): (
        "derived",
        "generated_view_naming",
        "Read from SYS.OBJECT_DEPENDENCIES: a generated calc view in the BW package depends on "
        "this object's table, and the view's name resolves to its provider by BW's generation "
        "convention. Declared as a dependency; resolved to a BW object by convention.",
    ),
    ("calc_view_consumer", "query"): (
        "derived",
        "generated_view_naming",
        "Read from SYS.OBJECT_DEPENDENCIES: the calc view BW generates for this BEx query depends "
        "on the object's table. The query and its provider come from the view's package name by "
        "BW's generation convention.",
    ),
    ("lineage_edge", "advisory"): (
        "inferred",
        "routine_select_parse",
        "Derived by parsing SELECTs out of a routine's ABAP. BW's own where-used lists do not "
        "contain this edge, and dynamic SQL and function-module calls are not followed, so it is a "
        "lower bound that can also be wrong.",
    ),
    # AggregationRule.confidence / ValueSource.confidence - how a code's meaning was obtained.
    ("code_decode", "dictionary"): (
        "derived",
        "dictionary_domain",
        "Decoded against this system's own ABAP dictionary (DD07L/DD07T on the column's domain), "
        "not from recalled knowledge.",
    ),
    ("code_decode", "cross_domain"): (
        "derived",
        "cross_domain_decode",
        "The column's own domain carries shifted texts on this release, so the meaning was taken "
        "from a sibling domain that is internally consistent and agrees with its neighbours.",
    ),
    ("code_decode", "advisory"): (
        "inferred",
        "undocumented_code",
        "The ABAP dictionary does not document this code, so the value is reported as found with a "
        "conventional reading rather than a dictionary-backed meaning.",
    ),
    # SourceSystem.kind_confidence - same two cases, under a differently named field.
    ("source_system_kind", "dictionary"): (
        "derived",
        "dictionary_domain",
        "The source-system type code is documented by this system's ABAP dictionary.",
    ),
    ("source_system_kind", "advisory"): (
        "inferred",
        "conventional_reading",
        "The dictionary does not document this source-system type code; the reading is the widely "
        "used convention and may not hold on this landscape.",
    ),
    # TableDependency.resolution_confidence - a /BIC/ or /BI0/ table name mapped to a BW object.
    ("table_resolution", "confirmed"): (
        "derived",
        "catalogue_confirmed",
        "The generated table name was decomposed and the resulting object was found in the "
        "provider or InfoObject catalogue.",
    ),
    ("table_resolution", "advisory"): (
        "inferred",
        "bic_table_naming",
        "The reading rests on the /BIC/ or /BI0/ naming convention alone; no catalogue entry "
        "confirmed that the object exists.",
    ),
    # HanaCrossing.resolution - how the BW side of a BW <-> HANA boundary crossing was identified.
    ("hana_crossing", "bw_provider_view"): (
        "derived",
        "bw_provider_view",
        "Parsed from BW's own generated 0BW:BIA:<provider> view name and confirmed against the "
        "provider catalogue, so both the name and the object type are established.",
    ),
    ("hana_crossing", "bic_table"): (
        "inferred",
        "bic_table_naming",
        "Read from a /BIC/ table name by convention. The BW object behind a generated table is not "
        "recorded anywhere, so this cannot be confirmed.",
    ),
    ("hana_crossing", "unresolved"): (
        "unknown",
        "unresolved",
        "The dependency names an object this server could not map back to a BW object at all.",
    ),
    # FieldLineagePath.resolution - how far a field's own derivation was followed.
    ("field_lineage", "field"): (
        "derived",
        "rule_walk",
        "Followed rule by rule through RSTRANFIELD/RSTRANRULE: this is the field's own derivation, "
        "not its provider's.",
    ),
    ("field_lineage", "provider"): (
        "inferred",
        "provider_fallback",
        "No rule populating this field was found, so the path falls back to the provider's "
        "upstream objects. The field's specific derivation is unknown, and reading this as field "
        "lineage is how wrong conclusions get drawn about which source field feeds a number.",
    ),
    ("field_lineage", "none"): (
        "unknown",
        "unresolved",
        "Nothing upstream resolved for this field at all.",
    ),
    # ChainSchedule cadence - how a chain's frequency was established.
    ("cadence", "high"): (
        "derived",
        "observed_run_history",
        "Classified from enough observed runs in the retained log window for the interval between "
        "them to be meaningful.",
    ),
    ("cadence", "low"): (
        "inferred",
        "sparse_run_history",
        "Too few runs in the retained window to establish a cadence; the classification is a "
        "reading of a small sample rather than a measured interval.",
    ),
    # RoutineAnalysis.completeness - the one vocabulary that is about the set, not the member.
    ("routine_analysis", "lower_bound"): (
        "inferred",
        "routine_static_parse",
        "Static parse of ABAP source. Dynamic SQL, function-module calls and class methods are not "
        "followed, so the true dependency set can only be larger than what is reported.",
    ),
}

#: Every vocabulary this module translates, for the test that asserts none is left behind.
VOCABULARIES: frozenset[str] = frozenset(name for name, _code in _MAPPING)


def evidence_for(vocabulary: str, code: str | None, *, detail: str | None = None) -> Evidence:
    """Translate a legacy confidence code into canonical :class:`Evidence`.

    ``detail`` overrides the mapping's standard sentence when a call site knows something more
    specific - the reason a particular edge exists, say, rather than the reason its class exists.

    An unmapped code yields ``basis="unknown"`` naming the code, never a default of "observed": a
    vocabulary gaining a value must degrade to "we do not know" rather than silently promote an
    unrecognised code to a fact.
    """
    key = (vocabulary, (code or "").strip())
    mapped = _MAPPING.get(key)
    if mapped is None:
        return Evidence(
            basis="unknown",
            method="unmapped_code",
            detail=(
                f"{vocabulary} reported {code!r}, which this build does not translate into an "
                "evidence basis, so how firmly the fact is established is not known."
            ),
            mapped_from=f"{vocabulary}={code}",
        )
    basis, method, standard_detail = mapped
    completeness: EvidenceCompleteness = (
        "lower_bound"
        if vocabulary == "routine_analysis" or method == "routine_select_parse"
        else "complete"
    )
    return Evidence(
        basis=basis,
        method=method,
        detail=detail or standard_detail,
        completeness=completeness,
        mapped_from=f"{vocabulary}={key[1]}",
    )


def summarise(items: list[Evidence]) -> EvidenceSummary:
    """Count a set of facts by basis, and list the mechanisms involved."""
    summary = EvidenceSummary()
    methods: dict[str, None] = {}
    for item in items:
        setattr(summary, item.basis, getattr(summary, item.basis, 0) + 1)
        methods[item.method] = None
    summary.methods = sorted(methods)
    return summary
