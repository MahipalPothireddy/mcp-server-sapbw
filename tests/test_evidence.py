"""The unified evidence vocabulary, and the guarantee that nothing escapes it.

Two kinds of test here. The first group pins the semantics: what each basis means, that an unmapped
code degrades to ``unknown`` rather than being promoted to a fact, and that completeness stays a
separate statement from basis.

The second group is the one that keeps this honest over time: it walks every legacy confidence
vocabulary's own ``Literal`` and asserts each of its values has a mapping. Adding a value to
``LineageEdge.confidence`` without deciding what it means as evidence fails here rather than
silently arriving in a tool response as ``unknown``.
"""

from __future__ import annotations

import typing

from pydantic import BaseModel

from mcp_server_sapbw.models.aggregation import AggregationRule
from mcp_server_sapbw.models.chains import ChainCadence
from mcp_server_sapbw.models.evidence import (
    BASIS_RANK,
    VOCABULARIES,
    Evidence,
    evidence_for,
    summarise,
)
from mcp_server_sapbw.models.hana import HanaCrossing
from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.providers import PartProviderRef
from mcp_server_sapbw.models.queries import FieldLineageHop, FieldLineagePath, ValueSource
from mcp_server_sapbw.models.sources import SourceSystem
from mcp_server_sapbw.models.transformations import RoutineAnalysis, TableDependency

_PROV = Provenance(source_table="RSTRAN")


# --- semantics ----------------------------------------------------------------------------


def test_basis_ranks_strongest_first() -> None:
    assert BASIS_RANK["observed"] < BASIS_RANK["derived"] < BASIS_RANK["inferred"]
    assert BASIS_RANK["inferred"] < BASIS_RANK["unknown"]
    assert (
        evidence_for("lineage_edge", "exact").rank < evidence_for("lineage_edge", "advisory").rank
    )


def test_declared_metadata_is_observed_and_a_routine_parse_is_inferred() -> None:
    declared = evidence_for("lineage_edge", "exact")
    parsed = evidence_for("lineage_edge", "advisory")
    assert (declared.basis, declared.is_advisory) == ("observed", False)
    assert (parsed.basis, parsed.is_advisory) == ("inferred", True)
    # Completeness is a separate statement: a parsed edge set is also a lower bound.
    assert declared.completeness == "complete"
    assert parsed.completeness == "lower_bound"


def test_a_dictionary_decode_is_derived_not_observed() -> None:
    """A row holds a code; its meaning comes from a join. Those are different claims."""
    decoded = evidence_for("code_decode", "dictionary")
    assert decoded.basis == "derived"
    assert decoded.method == "dictionary_domain"
    assert decoded.is_advisory is False


def test_cross_domain_decode_says_why_it_had_to_look_elsewhere() -> None:
    evidence = evidence_for("code_decode", "cross_domain")
    assert evidence.basis == "derived"
    assert evidence.detail is not None
    assert "shifted texts" in evidence.detail


def test_unmapped_code_degrades_to_unknown_rather_than_a_fact() -> None:
    evidence = evidence_for("lineage_edge", "something_new")
    assert evidence.basis == "unknown"
    assert evidence.method == "unmapped_code"
    assert evidence.mapped_from == "lineage_edge=something_new"
    assert evidence.detail is not None and "does not translate" in evidence.detail


def test_unknown_vocabulary_also_degrades() -> None:
    assert evidence_for("no_such_vocabulary", "confirmed").basis == "unknown"


def test_mapped_from_records_the_translation_for_audit() -> None:
    assert evidence_for("hana_crossing", "bic_table").mapped_from == "hana_crossing=bic_table"


def test_call_site_detail_overrides_the_class_level_reason() -> None:
    specific = evidence_for("lineage_edge", "advisory", detail="A SELECT in routine R reads X.")
    assert specific.detail == "A SELECT in routine R reads X."
    assert specific.basis == "inferred"  # the basis is not negotiable


def test_summarise_counts_by_basis_and_lists_the_methods() -> None:
    summary = summarise(
        [
            evidence_for("lineage_edge", "exact"),
            evidence_for("lineage_edge", "advisory"),
            evidence_for("lineage_edge", "advisory"),
            evidence_for("hana_crossing", "unresolved"),
        ]
    )
    assert (summary.observed, summary.inferred, summary.unknown) == (1, 2, 1)
    assert summary.total == 4
    assert summary.advisory_count == 3
    assert summary.methods == ["declared_metadata", "routine_select_parse", "unresolved"]


# --- every legacy vocabulary is covered ---------------------------------------------------
#
# Each entry pairs a model field with the vocabulary name it maps through. The test reads the
# field's own Literal, so it cannot fall behind the model.
_COVERED: list[tuple[type[BaseModel], str, str]] = [
    (LineageEdge, "confidence", "lineage_edge"),
    (PartProviderRef, "confidence", "part_provider"),
    (AggregationRule, "confidence", "code_decode"),
    (ValueSource, "confidence", "code_decode"),
    (SourceSystem, "kind_confidence", "source_system_kind"),
    (TableDependency, "resolution_confidence", "table_resolution"),
    (HanaCrossing, "resolution", "hana_crossing"),
    (FieldLineagePath, "resolution", "field_lineage"),
    (ChainCadence, "confidence", "cadence"),
    (RoutineAnalysis, "completeness", "routine_analysis"),
]

# Vocabularies chosen by a *call site* rather than derived from a field's Literal, with the codes
# that call site uses. A service can know something the model cannot: two edges may share
# `confidence="advisory"` and still have been obtained by different mechanisms, and the reader needs
# the mechanism. Registered here so the orphan check below stays meaningful - a vocabulary nothing
# uses is still caught - and so each code is verified to have a mapping.
_CALL_SITE_COVERED: dict[str, tuple[str, ...]] = {
    # services/lineage.py: a consumer discovered through a generated calc view, which is either a
    # CompositeProvider's view or a BEx query's. Neither was parsed out of ABAP.
    "calc_view_consumer": ("provider", "query"),
    # services/lineage.py: a BEx query assigned to a provider by RSZCOMPIC. Deliberately distinct
    # from calc_view_consumer='query' - that one notices a generated view touching a table, this one
    # reads BW's own assignment - and the two are not interchangeable (D12).
    "declared_query_provider": ("rszcompic",),
}


def _literal_values(model: type[BaseModel], field: str) -> list[str]:
    annotation = model.model_fields[field].annotation
    values: list[str] = []
    for arg in typing.get_args(annotation) or (annotation,):
        if arg is type(None):
            continue
        inner = typing.get_args(arg)
        values.extend(str(v) for v in (inner or ()) if isinstance(v, str))
        if not inner and isinstance(arg, str):
            values.append(arg)
    return values


def test_every_value_of_every_legacy_vocabulary_has_a_mapping() -> None:
    missing: list[str] = []
    for model, field, vocabulary in _COVERED:
        values = _literal_values(model, field)
        assert values, f"{model.__name__}.{field} exposes no Literal values to check"
        for value in values:
            # A code deliberately mapped to basis 'unknown' is fine - that is a decision. What is
            # not fine is a code with no entry at all, which the sentinel method identifies.
            if evidence_for(vocabulary, value).method == "unmapped_code":
                missing.append(f"{model.__name__}.{field}={value!r} (vocabulary {vocabulary!r})")
    assert not missing, (
        "these confidence values have no evidence mapping, so they would reach a caller as "
        f"basis='unknown': {missing}. Add them to _MAPPING in models/evidence.py."
    )


def test_every_call_site_vocabulary_code_has_a_mapping() -> None:
    """A code a service passes must map, or the edge reaches the caller as basis='unknown'."""
    missing = [
        f"{vocabulary!r}={code!r}"
        for vocabulary, codes in _CALL_SITE_COVERED.items()
        for code in codes
        if evidence_for(vocabulary, code).method == "unmapped_code"
    ]
    assert not missing, f"call-site vocabulary codes with no mapping: {missing}"


def test_no_vocabulary_in_the_mapping_is_orphaned() -> None:
    """A mapping entry nothing maps through is dead weight and hides a removed field."""
    used = {vocabulary for _model, _field, vocabulary in _COVERED} | set(_CALL_SITE_COVERED)
    assert VOCABULARIES - used == set(), f"unused vocabularies in _MAPPING: {VOCABULARIES - used}"


# --- auto-derivation on the carrying models ------------------------------------------------


def test_lineage_edge_evidence_is_derived_and_cannot_disagree() -> None:
    edge = LineageEdge(
        src="dso:A",
        dst="dso:B",
        kind="routine_lookup",
        derivation="routine",
        confidence="advisory",
        transformation_id="TR1",
        provenance=_PROV,
    )
    assert edge.evidence is not None
    assert edge.evidence.basis == "inferred"
    assert edge.evidence.detail is not None
    assert "TR1" in edge.evidence.detail  # the reason names *this* edge, not just its class


def test_declared_edge_detail_names_the_transformation() -> None:
    edge = LineageEdge(
        src="dso:A", dst="cube:B", kind="transformation", transformation_id="TR9", provenance=_PROV
    )
    assert edge.evidence is not None
    assert edge.evidence.basis == "observed"
    assert edge.evidence.detail is not None and "TR9" in edge.evidence.detail


def test_graph_summarises_its_edges() -> None:
    graph = LineageGraph(
        root_id="dso:A",
        direction="both",
        depth=2,
        edges=[
            LineageEdge(src="a", dst="b", kind="transformation", provenance=_PROV),
            LineageEdge(
                src="b",
                dst="c",
                kind="routine_lookup",
                derivation="routine",
                confidence="advisory",
                provenance=_PROV,
            ),
        ],
    )
    assert graph.evidence_summary is not None
    assert graph.evidence_summary.observed == 1
    assert graph.evidence_summary.inferred == 1


def test_part_provider_evidence_names_the_table_it_came_from() -> None:
    part = PartProviderRef(
        name="SALES_DSO",
        via_table="/BIC/" + "ASALES_DSO00",
        confidence="advisory",
        provenance=_PROV,
    )
    assert part.evidence is not None
    assert part.evidence.basis == "inferred"
    assert part.evidence.detail is not None and "naming convention" in part.evidence.detail


def test_frozen_aggregation_rule_still_gets_evidence() -> None:
    """AggregationRule is frozen, so its evidence is filled before validation, not after."""
    rule = AggregationRule(code="LAS", label="Last value", confidence="dictionary")
    assert rule.evidence is not None
    assert rule.evidence.basis == "derived"


def test_field_lineage_path_with_a_routine_hop_becomes_a_lower_bound() -> None:
    path = FieldLineagePath(
        iobjnm="0MATERIAL", resolution="field", has_routine_hop=True, provenance=_PROV
    )
    assert path.evidence is not None
    assert path.evidence.basis == "derived"  # the walk itself was exact
    assert path.evidence.completeness == "lower_bound"  # but a routine hop bounds it
    assert path.evidence.detail is not None and "routine" in path.evidence.detail


def test_provider_fallback_path_is_inferred_not_derived() -> None:
    path = FieldLineagePath(iobjnm="0MATERIAL", resolution="provider", provenance=_PROV)
    assert path.evidence is not None
    assert path.evidence.basis == "inferred"
    assert path.evidence.method == "provider_fallback"


def test_hop_evidence_reuses_the_edge_vocabulary() -> None:
    hop = FieldLineageHop(
        object_name="SALES_DSO", object_type="dso", advisory=True, routine_code_id="R1"
    )
    assert hop.evidence is not None
    assert hop.evidence.basis == "inferred"
    assert hop.evidence.detail is not None and "R1" in hop.evidence.detail


def test_unresolved_hana_crossing_is_unknown_not_inferred() -> None:
    crossing = HanaCrossing(
        direction="hana_reads_bw", hana_object="CV1", bw_object="SOMETHING", provenance=_PROV
    )
    assert crossing.evidence is not None
    assert crossing.evidence.basis == "unknown"


def test_type_confirmed_crossing_is_derived() -> None:
    crossing = HanaCrossing(
        direction="hana_reads_bw",
        hana_object="CV1",
        bw_object="0BW:BIA:SALES_CP",
        bw_object_resolved="SALES_CP",
        bw_object_kind="compositeprovider",
        resolution="bw_provider_view",
        provenance=_PROV,
    )
    assert crossing.evidence is not None
    assert crossing.evidence.basis == "derived"
    assert crossing.evidence.detail is not None
    assert "type-confirmed" in crossing.evidence.detail


def test_cadence_evidence_states_the_sample_it_rests_on() -> None:
    cadence = ChainCadence(
        chain_id="DAILY_LOAD",
        frequency="daily",
        run_count=30,
        run_days=30,
        median_gap_days=1.0,
        provenance=_PROV,
    )
    assert cadence.evidence is not None
    assert cadence.evidence.basis == "derived"
    assert cadence.evidence.detail is not None and "30 run(s)" in cadence.evidence.detail


def test_sparse_cadence_is_inferred() -> None:
    cadence = ChainCadence(
        chain_id="ONE_OFF",
        frequency="unknown",
        run_count=1,
        run_days=1,
        confidence="low",
        provenance=_PROV,
    )
    assert cadence.evidence is not None
    assert cadence.evidence.basis == "inferred"


def test_routine_analysis_evidence_counts_the_calls_it_could_not_follow() -> None:
    analysis = RoutineAnalysis(code_id="R1", kind="start", provenance=_PROV)
    assert analysis.evidence is not None
    assert analysis.evidence.basis == "inferred"
    assert analysis.evidence.completeness == "lower_bound"


def test_supplied_evidence_is_not_overwritten() -> None:
    supplied = Evidence(basis="observed", method="hand_made", detail="because I said so")
    edge = LineageEdge(
        src="a",
        dst="b",
        kind="transformation",
        confidence="advisory",
        evidence=supplied,
        provenance=_PROV,
    )
    assert edge.evidence == supplied


def test_table_dependency_without_a_resolution_has_no_evidence() -> None:
    """A standard table nobody tried to resolve makes no claim, so it carries no evidence."""
    dependency = TableDependency(table="T001")
    assert dependency.evidence is None
