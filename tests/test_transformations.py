"""Tests for the transformations repository (B5), offline against scripted fixtures.

Synthetic names only. Routine source here uses plain ABAP (no /BIC/) so this file stays clean for
the customer-metadata scan; /BIC/ resolution is covered by test_routine_parser.py fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.transformations import TransformationsRepository

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "transformation_text": "RSTRANT",
}

# OBJSTAT, SOURCETYPE, SOURCESUBTYPE, SOURCENAME, TARGETTYPE, TARGETSUBTYPE, TARGETNAME,
# STARTROUTINE, ENDROUTINE, EXPERT, GLBCODE, GLBCODE2
_TRAN = {
    "TRANSFORM01": (
        "ACT",
        "RSDS",
        "",
        "DATASOURCE_A",
        "ADSO",
        "",
        "FIN_ADSO",
        "CODESTART",
        "",
        "",
        "CODEGLBL",
        "",
    )
}
_RULES = {"TRANSFORM01": [(1, "DIRECT"), (2, "ROUTINE"), (3, "CONSTANT")]}
_FIELDS = {
    "TRANSFORM01": [
        (1, "1", "GL_ACCOUNT"),
        (1, "0", "GLACCT"),
        (2, "1", "AMOUNT"),
        (2, "0", "DMBTR"),
        (2, "0", "WRBTR"),
        (3, "1", "COMPANY"),
    ]
}
_STEPROUT = {"TRANSFORM01": [(2, "CODEFIELD", "NORMAL")]}
_SOURCE = {
    "CODESTART": ["METHOD start_routine.", "  SELECT * FROM mara INTO TABLE lt.", "ENDMETHOD."],
    "CODEGLBL": ["* global", "DATA gv TYPE i."],
    "CODEFIELD": ["METHOD field.", "  result = src-dmbtr + src-wrbtr.", "ENDMETHOD."],
}
_TEXT = {"TRANSFORM01": ("E", "Load fin postings", "Load financial postings into ADSO")}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        name = str(params[-1]) if params else ""
        if "TOTAL_COUNT" in sql:
            return [(len(_TRAN),)]
        if "RSTRANSTEPROUT" in sql:
            rows = _STEPROUT.get(name, [])
            if "KIND" in sql:
                return [(r[0], r[1], r[2]) for r in rows]
            return [(r[0], r[1]) for r in rows]
        if "RSTRANFIELD" in sql:
            return [(r[0], r[1], r[2]) for r in _FIELDS.get(name, [])]
        if "RSTRANRULE" in sql:
            return [(r[0], r[1]) for r in _RULES.get(name, [])]
        if "RSTRANT" in sql:
            text = _TEXT.get(name)
            return [text] if text else []
        if "RSAABAP" in sql:
            code_id = str(params[0])
            return [(line,) for line in _SOURCE.get(code_id, [])]
        if "RSTRAN" in sql:  # header (checked last: substring of the others)
            if "OBJSTAT" in sql:  # get_transformation (12 cols)
                d = _TRAN.get(name)
                return [d] if d else []
            # list_transformations (8 cols)
            return [(t, d[1], d[3], d[4], d[6], d[7], d[8], d[9]) for t, d in _TRAN.items()]
        return []


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(present: set[str] | None = None) -> TransformationsRepository:
    return TransformationsRepository(ScriptedConnection(), _capability(present))


def test_get_transformation_endpoints_and_mappings() -> None:
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    assert tran.active is True
    assert tran.source is not None and tran.source.kind == "datasource"
    assert tran.source.name == "DATASOURCE_A"
    assert tran.target is not None and tran.target.kind == "adso"
    by_rule = {m.rule_id: m for m in tran.field_mappings}
    assert by_rule[1].rule_type == "direct"
    assert by_rule[1].target_fields == ["GL_ACCOUNT"]
    assert by_rule[1].source_fields == ["GLACCT"]
    assert by_rule[2].rule_type == "routine"
    assert by_rule[2].source_fields == ["DMBTR", "WRBTR"]
    assert by_rule[2].routine_code_id == "CODEFIELD"
    assert by_rule[3].rule_type == "constant"
    assert by_rule[3].target_fields == ["COMPANY"]
    assert by_rule[3].source_fields == []


def test_get_transformation_routines() -> None:
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    kinds = {(r.kind, r.code_id) for r in tran.routines}
    assert ("start", "CODESTART") in kinds
    assert ("global", "CODEGLBL") in kinds
    assert ("field", "CODEFIELD") in kinds
    assert tran.has_start_routine is True
    assert tran.has_end_routine is False


def test_list_transformations() -> None:
    result = _repo().list_transformations()
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    summary = summaries[0]
    assert summary.tran_id == "TRANSFORM01"
    assert summary.source_kind == "datasource"
    assert summary.target_kind == "adso"
    assert summary.has_routines is True
    assert summary.description == "Load fin postings"


def test_get_routine_code() -> None:
    result = _repo().get_routine_code("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    by_code = {c.code_id: c for c in result}
    assert set(by_code) == {"CODESTART", "CODEGLBL", "CODEFIELD"}
    assert by_code["CODESTART"].line_count == 3
    assert by_code["CODESTART"].provenance.source_table == "RSAABAP"


def test_analyze_routines_lower_bound() -> None:
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    assert len(result) == 3
    for analysis in result:
        assert analysis.completeness == "lower_bound"
        assert analysis.provenance.source_table == "RSAABAP"
    start = next(a for a in result if a.code_id == "CODESTART")
    assert any(d.table == "mara" for d in start.table_dependencies)


def test_unsupported_without_transformation_table() -> None:
    repo = _repo(present={"routine_source"})  # transformation absent
    result = repo.get_transformation("TRANSFORM01")
    assert isinstance(result, UnsupportedResult)


def test_routine_code_unsupported_without_rsaabap() -> None:
    repo = _repo(present={"transformation", "transformation_rule", "transformation_field"})
    result = repo.get_routine_code("TRANSFORM01")
    assert isinstance(result, UnsupportedResult)
