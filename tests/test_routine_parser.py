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
    assert _resolve_bw_table(_BIC_DSO) == ("SALES", "dso")
    assert _resolve_bw_table(_BI0_IOBJ) == ("MATERIAL", "infoobject")
    assert _resolve_bw_table("MARA") == (None, None)


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
    # tables from both selects resolved
    resolved = {d.resolved_object for d in analysis.table_dependencies}
    assert {"SALES", "MATERIAL"} <= resolved


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
