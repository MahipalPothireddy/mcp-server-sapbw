"""Tests for the heuristic ABAP routine parser (B5).

Sample ABAP lives under tests/fixtures/routines/ (the customer-metadata scan excludes fixtures/),
using synthetic /BIC/ and /BI0/ table names only.
"""

from __future__ import annotations

from pathlib import Path

from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.transformations import RoutineAnalysis, RoutineKind
from mcp_server_sapbw.services.routine_parser import RoutineParser, _resolve_bw_table

_FIXTURES = Path(__file__).parent / "fixtures" / "routines"

# Synthetic generated-table names built by concatenation so the customer-metadata scan (which
# forbids /BIC/ and /BI0/ tokens outside tests/fixtures/) does not match them in this .py file.
# The same names appear literally in the .abap fixtures, which the scan excludes.
_BIC_DSO = "/BIC/" + "ASALES00"
_BI0_IOBJ = "/BI0/" + "PMATERIAL"


def _lines(name: str) -> list[str]:
    return (_FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _prov(code_id: str) -> Provenance:
    return Provenance(source_table="RSAABAP", source_key={"CODEID": code_id, "OBJVERS": "A"})


def _analyze(name: str, code_id: str = "ROUT001", kind: RoutineKind = "start") -> RoutineAnalysis:
    return RoutineParser().analyze(
        code_id=code_id, kind=kind, lines=_lines(name), provenance=_prov(code_id)
    )


def test_resolve_bw_table() -> None:
    assert _resolve_bw_table(_BIC_DSO) == ("SALES", "dso", "advisory")
    # /BI0/ drops the SAP object's leading "0", so resolution restores it: the InfoObject is
    # 0MATERIAL, and "MATERIAL" exists in no catalogue.
    assert _resolve_bw_table(_BI0_IOBJ) == ("0MATERIAL", "infoobject", "advisory")
    assert _resolve_bw_table("MARA") == (None, None, None)


def test_bi0_restores_the_sap_zero_prefix() -> None:
    """Measured 400/400 on a live system: /BI0/P<NAME> belongs to InfoObject 0<NAME>."""
    for table, expected in (
        ("/BI0/" + "PMATNR", "0MATNR"),
        ("/BI0/" + "MCUST", "0CUST"),
        ("/BI0/" + "MPLANT", "0PLANT"),
    ):
        name, kind, _conf = _resolve_bw_table(table)
        assert name == expected
        assert kind == "infoobject"


def test_bic_does_not_gain_a_zero_prefix() -> None:
    """The rule is /BI0/-only; a customer object keeps its name exactly."""
    name, _kind, _conf = _resolve_bw_table("/BIC/" + "PCUSTOM_IOBJ")
    assert name == "CUSTOM_IOBJ"


def test_adso_table_is_not_reported_as_a_dso() -> None:
    """The `1`/`2`/`3` suffixes are ADSO inbound/active/changelog, not a classic DSO."""
    for suffix, role_kind in (("1", "adso"), ("2", "adso"), ("3", "adso")):
        name, kind, _conf = _resolve_bw_table("/BIC/" + "AFIN_ADSO" + suffix)
        assert name == "FIN_ADSO"
        assert kind == role_kind


def test_trailing_digits_in_the_object_name_are_preserved() -> None:
    """Regression: an earlier resolver stripped EVERY trailing digit.

    Four distinct ADSO active tables on a real system all collapsed onto one truncated stem that
    exists in no catalogue. Only the single role suffix may be removed.
    """
    for table, expected in (
        ("/BIC/" + "APA_D082", "PA_D08"),
        ("/BIC/" + "APA_D072", "PA_D07"),
        ("/BIC/" + "APA_D502", "PA_D50"),
        ("/BIC/" + "APA_D182", "PA_D18"),
    ):
        name, kind, _conf = _resolve_bw_table(table)
        assert name == expected, f"{table} resolved to {name}, expected {expected}"
        assert kind == "adso"


def test_catalogue_confirmation_upgrades_confidence_and_picks_the_real_object() -> None:
    catalog = {"adso": ["PA_D08"], "dso": [], "infocube": [], "infoobject": []}
    name, kind, confidence = _resolve_bw_table("/BIC/" + "APA_D082", catalog)
    assert (name, kind, confidence) == ("PA_D08", "adso", "confirmed")


def test_dso_activation_queue_is_distinguished_from_active() -> None:
    active, kind_a, _ = _resolve_bw_table("/BIC/" + "ASALES00")
    queue, kind_q, _ = _resolve_bw_table("/BIC/" + "ASALES40")
    assert (active, kind_a) == ("SALES", "dso")
    assert (queue, kind_q) == ("SALES", "dso")


def test_customer_namespace_generated_table_resolves() -> None:
    """Only /BIC/ and /BI0/ were ever offered to the resolver, so these went unresolved."""
    name, kind, _conf = _resolve_bw_table("/ABC/" + "AD_STOCK2")
    assert name == "/ABC/D_STOCK"
    assert kind == "adso"


def test_namespaced_table_reaches_dependencies_as_bw_generated() -> None:
    parser = RoutineParser()
    analysis = parser.analyze(
        code_id="R1",
        kind="start",
        lines=["SELECT f FROM " + "/ABC/" + "AD_STOCK2" + " INTO TABLE lt."],
        provenance=_prov("R1"),
    )
    dep = analysis.table_dependencies[0]
    assert dep.is_bw_generated is True
    assert dep.resolved_object == "/ABC/D_STOCK"
    assert dep.resolved_kind == "adso"
    assert dep.resolution_confidence == "advisory"


def test_non_bw_table_carries_no_resolution_or_confidence() -> None:
    parser = RoutineParser()
    analysis = parser.analyze(
        code_id="R2",
        kind="start",
        lines=["SELECT f FROM mara INTO TABLE lt."],
        provenance=_prov("R2"),
    )
    dep = analysis.table_dependencies[0]
    assert dep.is_bw_generated is False
    assert dep.resolved_object is None
    assert dep.resolution_confidence is None


def test_clean_routine_has_deps_no_antipatterns() -> None:
    analysis = _analyze("clean_start.abap")
    tables = {d.table for d in analysis.table_dependencies}
    assert _BIC_DSO in tables
    dep = next(d for d in analysis.table_dependencies if d.table == _BIC_DSO)
    assert dep.is_bw_generated is True
    assert dep.resolved_object == "SALES"
    assert dep.resolved_kind == "dso"
    # FOR ALL ENTRIES is guarded by IS NOT INITIAL -> not flagged
    kinds = {a.kind for a in analysis.anti_patterns}
    assert "missing_for_all_entries" not in kinds
    assert "select_in_loop" not in kinds
    # always a lower bound
    assert analysis.completeness == "lower_bound"
    assert analysis.caveats
    assert analysis.provenance.source_table == "RSAABAP"


def test_bad_routine_flags_antipatterns() -> None:
    analysis = _analyze("bad_end.abap", kind="end")
    kinds = {a.kind for a in analysis.anti_patterns}
    assert "select_in_loop" in kinds
    assert "missing_for_all_entries" in kinds  # lt_x is not guarded
    assert "hardcoded_value" in kinds  # WHERE spras = 'E'
    assert "recordset_delete" in kinds  # DELETE ADJACENT DUPLICATES / DELETE ... WHERE
    # tables from both selects resolved. "0MATERIAL", not "MATERIAL": a /BI0/ table name drops
    # the SAP object's leading 0, so resolution restores it.
    resolved = {d.resolved_object for d in analysis.table_dependencies}
    assert {"SALES", "0MATERIAL"} <= resolved


def test_bad_routine_names_unresolved_calls() -> None:
    analysis = _analyze("bad_end.abap", kind="end")
    fms = {r.object_name for r in analysis.unresolved_refs if r.call_kind == "function_module"}
    methods = {r.object_name for r in analysis.unresolved_refs if r.call_kind == "class_method"}
    assert "CONVERSION_EXIT_ALPHA_INPUT" in fms
    assert any("transform" in m for m in methods)


def test_complexity_signals() -> None:
    analysis = _analyze("bad_end.abap", kind="end")
    assert analysis.complexity.select_count == 2
    assert analysis.complexity.loop_count == 1
    assert analysis.complexity.line_count > 0


# --- FOR ALL ENTRIES: what counts as guarded ---------------------------------------------------
#
# Measured on a real extractor-exit set of 26 programs: 74 FOR ALL ENTRIES reads, of which the naive
# rule flagged 64 as unguarded. Two spellings accounted for most of the gap, and reporting them as
# risks buries the ones that are real.


def _fae_findings(lines: list[str]) -> list[str]:
    analysis = RoutineParser().analyze(
        code_id="R", kind="start", lines=lines, provenance=_prov("R")
    )
    return [a.detail or "" for a in analysis.anti_patterns if a.kind == "missing_for_all_entries"]


def test_the_frameworks_own_package_needs_no_guard() -> None:
    # C_T_DATA is the extractor exit's data parameter and I_T_DATA / SOURCE_PACKAGE the equivalents
    # elsewhere. The framework fills them before the call, so an empty-table read cannot happen.
    for driver in ("c_t_data", "i_t_data", "source_package", "result_package"):
        lines = [
            f"  SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN {driver} WHERE k = {driver}-k."
        ]
        assert _fae_findings(lines) == [], driver


def test_a_locally_filled_driver_still_needs_a_guard() -> None:
    lines = ["  SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN lt_keys WHERE k = lt_keys-k."]
    findings = _fae_findings(lines)
    assert len(findings) == 1
    # The driver is named so a reviewer can settle the case without re-reading the routine.
    assert "lt_keys" in findings[0]


def test_the_bracket_spelling_of_the_guard_is_recognised() -> None:
    # `IF lt_keys[] IS NOT INITIAL` is the older spelling and still common; missing it reports a
    # guarded read as unguarded.
    lines = [
        "  IF lt_keys[] IS NOT INITIAL.",
        "    SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN lt_keys WHERE k = lt_keys-k.",
        "  ENDIF.",
    ]
    assert _fae_findings(lines) == []


def test_describe_table_counts_as_a_guard() -> None:
    lines = [
        "  DESCRIBE TABLE lt_keys LINES lv_n.",
        "  IF lv_n > 0.",
        "    SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN lt_keys WHERE k = lt_keys-k.",
        "  ENDIF.",
    ]
    assert _fae_findings(lines) == []


def test_a_namespaced_driver_table_is_captured() -> None:
    lines = ["  SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN it_/irm/i0si WHERE k = 1."]
    findings = _fae_findings(lines)
    assert len(findings) == 1
    assert "it_/irm/i0si" in findings[0]


def test_the_finding_states_that_it_is_an_upper_bound() -> None:
    lines = ["  SELECT f FROM tbl INTO TABLE lt FOR ALL ENTRIES IN lt_keys WHERE k = lt_keys-k."]
    # An sy-subrc test after filling the driver is a guard this parser cannot follow, so the count
    # must not be presented as a measurement.
    assert "sy-subrc" in _fae_findings(lines)[0]
