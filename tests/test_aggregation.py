"""Tests for aggregation decoding and summability.

The contract under test is narrow but consequential: the server must say when a number cannot be
reproduced by adding rows up, and must never claim more certainty about a code than the dictionary
supports. Every code table here was read from a live dictionary; these tests pin the behaviour, not
the SAP semantics.
"""

from __future__ import annotations

from typing import Any

from mcp_server_sapbw.models.aggregation import KeyFigureAggregation
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.aggregation import (
    build_exception_aggregation,
    build_key_figure_aggregation,
    classify_reference,
    decode_exception_aggregation,
    decode_keyfigure_aggregation,
    decode_query_aggregation,
)

_PROV = Provenance(source_table="RSDKYF", source_key={"KYFNM": "KF_TEST"})


# --- code decoding ----------------------------------------------------------------------------


def test_blank_code_means_no_setting_not_a_default() -> None:
    assert decode_exception_aggregation("") is None
    assert decode_exception_aggregation(None) is None
    assert decode_keyfigure_aggregation("  ") is None


def test_documented_exception_codes_decode_with_dictionary_confidence() -> None:
    rule = decode_exception_aggregation("CNT")
    assert rule is not None
    assert rule.label == "Counter (all values)"
    assert rule.confidence == "dictionary"
    assert rule.is_summation is False


def test_summation_is_the_only_code_flagged_as_summation() -> None:
    assert decode_exception_aggregation("SUM").is_summation is True  # type: ignore[union-attr]
    for code in ("LAS", "FIR", "AVG", "CNT", "MAX", "MIN", "STD", "VAR", "MED"):
        assert decode_exception_aggregation(code).is_summation is False  # type: ignore[union-attr]


def test_undocumented_code_is_advisory_not_invented() -> None:
    rule = decode_exception_aggregation("ZZZ")
    assert rule is not None
    assert rule.code == "ZZZ"
    assert rule.label is None
    assert rule.confidence == "advisory"


def test_late_release_codes_are_known() -> None:
    """MED / SLS / SLI / SLC exist in RSDAGGREXC and must not fall through to advisory."""
    for code in ("MED", "SLS", "SLI", "SLC", "NHA", "NGA"):
        assert decode_exception_aggregation(code).confidence == "dictionary"  # type: ignore[union-attr]


def test_keyfigure_obsolete_codes_report_the_dictionary_verdict() -> None:
    """RSDAGGRGEN calls NOP/NO1/NO2 obsolete; that is the useful fact, so it is passed through."""
    rule = decode_keyfigure_aggregation("NOP")
    assert rule is not None
    assert rule.label is not None
    assert "No longer used" in rule.label
    assert rule.confidence == "dictionary"


# --- the RSAGGRGEN dictionary defect ----------------------------------------------------------


def test_query_aggregation_never_uses_the_corrupt_domain_text() -> None:
    """Domain RSAGGRGEN stores 'SUM' against a "No Aggregation" text. That must never surface."""
    rule = decode_query_aggregation("SUM")
    assert rule is not None
    assert rule.label == "Summation"
    assert rule.is_summation is True
    assert rule.confidence == "dictionary"
    assert "No aggregation" not in (rule.label or "")


def test_query_codes_absent_from_the_consistent_domain_are_cross_domain() -> None:
    rule = decode_query_aggregation("NGA")
    assert rule is not None
    assert rule.confidence == "cross_domain"
    assert rule.label == "No aggregation of postable nodes along hierarchy"
    assert rule.is_summation is False


def test_query_aggregation_unknown_code_is_advisory() -> None:
    assert decode_query_aggregation("QQQ").confidence == "advisory"  # type: ignore[union-attr]


# --- reference characteristics ----------------------------------------------------------------


def test_plain_infoobject_reference() -> None:
    assert classify_reference("0PLANT") == "infoobject"


def test_provider_qualified_reference_is_not_claimed_to_be_an_infoobject() -> None:
    assert classify_reference("4ZPP_L12-D30_P_INDEX") == "provider_field"
    assert classify_reference("2HAOS.MD.MM_YMM_DAT-EBELN") == "provider_field"


def test_bare_zero_sentinel_is_unresolved_not_an_object() -> None:
    """'0' occurs 109 times on the reference system and is not an InfoObject."""
    assert classify_reference("0") == "unresolved"
    assert classify_reference("") == "unresolved"


def test_multiple_reference_characteristics_keep_declared_order() -> None:
    exc = build_exception_aggregation(
        code="CNT", references=["0PLANT", "0MATERIAL", "", "0REQ_DATE", ""]
    )
    assert exc is not None
    assert [r.name for r in exc.reference_characteristics] == ["0PLANT", "0MATERIAL", "0REQ_DATE"]
    # Position reflects the AGGRCHA column it came from, so blanks do not renumber the rest.
    assert [r.position for r in exc.reference_characteristics] == [1, 2, 4]


def test_more_than_five_references_are_ignored() -> None:
    exc = build_exception_aggregation(code="CNT", references=["A"] * 9)
    assert exc is not None
    assert len(exc.reference_characteristics) == 5


# --- summability ------------------------------------------------------------------------------


def test_counter_over_a_characteristic_is_not_reproducible_by_summation() -> None:
    exc = build_exception_aggregation(code="CNT", references=["0MATERIAL"])
    assert exc is not None
    assert exc.reproducible_by_summation is False


def test_explicit_summation_is_reproducible() -> None:
    exc = build_exception_aggregation(code="SUM", references=["0CALDAY"])
    assert exc is not None
    assert exc.reproducible_by_summation is True


def test_exclude_flag_breaks_summability_even_for_sum() -> None:
    exc = build_exception_aggregation(code="SUM", references=["0CALDAY"], exclude="X")
    assert exc is not None
    assert exc.excludes_reference is True
    assert exc.reproducible_by_summation is False
    assert "excluded" in (exc.note or "")


def test_exception_aggregation_with_no_reference_is_reported_as_such() -> None:
    exc = build_exception_aggregation(code="LAS", references=["", "", "", "", ""])
    assert exc is not None
    assert exc.reference_characteristics == []
    assert "cannot be stated from metadata" in (exc.note or "")


def test_unresolved_reference_is_flagged_in_the_note() -> None:
    exc = build_exception_aggregation(code="CNT", references=["0"])
    assert exc is not None
    assert "unresolved rather than guessed" in (exc.note or "")


# --- key-figure record ------------------------------------------------------------------------


def _kf(**kwargs: Any) -> KeyFigureAggregation:
    return build_key_figure_aggregation(key_figure="KF_TEST", provenance=_PROV, **kwargs)


def test_plain_summed_amount_is_summable() -> None:
    kf = _kf(kyftp="AMO", datatp="CURR", aggrgen="SUM", fixcuky="USD")
    assert kf.summable is True
    assert kf.summability_caveats == []
    assert kf.key_figure_type is not None and kf.key_figure_type.label == "Amount"
    assert kf.fixed_currency == "USD"


def test_last_value_over_calday_is_not_summable_and_says_why() -> None:
    kf = _kf(aggrgen="SUM", aggrexc="LAS", aggrcha="0CALDAY")
    assert kf.summable is False
    joined = " ".join(kf.summability_caveats)
    assert "LAS" in joined
    assert "0CALDAY" in joined
    assert "does not reproduce the reported number" in joined


def test_non_cumulative_stock_is_not_summable_independently_of_exception_aggregation() -> None:
    """A stock figure has no sum over time even when its aggregation says SUM."""
    kf = _kf(aggrgen="SUM", aggrexc="SUM", ncumfl="1")
    assert kf.summable is False
    assert kf.non_cumulative == "stock_with_change"
    assert any("no meaningful sum over time" in c for c in kf.summability_caveats)


def test_both_reasons_are_reported_together() -> None:
    kf = _kf(aggrexc="LAS", aggrcha="0CALDAY", ncumfl="2")
    assert len(kf.summability_caveats) >= 2


def test_undocumented_non_cumulative_value_is_not_treated_as_safe() -> None:
    """Unknown must not read as cumulative; that would silently license a wrong total."""
    kf = _kf(ncumfl="9")
    assert kf.summable is False
    assert any("undocumented value" in c for c in kf.summability_caveats)


def test_min_max_standard_aggregation_breaks_summability() -> None:
    assert _kf(aggrgen="MAX").summable is False
    assert _kf(aggrgen="MIN").summable is False


def test_varying_unit_is_caveated_even_when_otherwise_summable() -> None:
    kf = _kf(aggrgen="SUM", uninm="0DOC_CURRCY")
    assert kf.unit_infoobject == "0DOC_CURRCY"
    assert any("varies per record" in c for c in kf.summability_caveats)


def test_fixed_unit_suppresses_the_varying_unit_caveat() -> None:
    kf = _kf(aggrgen="SUM", uninm="0BASE_UOM", fixunit="KG")
    assert not any("varies per record" in c for c in kf.summability_caveats)


def test_stock_coverage_flag_is_surfaced() -> None:
    assert _kf(semantic="S").stock_coverage is True
    assert _kf(semantic="").stock_coverage is False


def test_provenance_cites_the_row() -> None:
    assert _kf().provenance.source_table == "RSDKYF"
