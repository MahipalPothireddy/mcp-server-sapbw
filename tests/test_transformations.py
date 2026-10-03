"""Tests for the transformations repository (B5), offline against scripted fixtures.

Synthetic names only. Routine source here uses plain ABAP (no /BIC/) so this file stays clean for
the customer-metadata scan; /BIC/ resolution is covered by test_routine_parser.py fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.core.dialect import LIKE_ESCAPE
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.services.routine_parser import form_bodies, performed_forms
from tests.sqllike import matches_like

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "dtp": "RSBKDTP",  # needed to dereference a DTP-typed endpoint (D18)
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "transformation_step_const": "RSTRANSTEPCNST",
    "transformation_step_master": "RSTRANSTEPMASTER",
    "transformation_step_dso": "RSTRANSTEPODSO",
    "transformation_step_adso": "RSTRANSTEPADSO",
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
# A DataSource endpoint as BW actually stores it: '<DATASOURCE><padding><LOGSYS>'. An equality
# filter on the DataSource name alone can never match this, which is why endpoints are LIKE-matched.
_PADDED_DS = "DATASOURCE_B".ljust(30) + "SRCSYS100"
_TRAN["TRANSFORM02"] = (
    "ACT",
    "RSDS",
    "",
    _PADDED_DS,
    "ODSO",
    "",
    "FIN_DSO",
    "",
    "",
    "",
    "",
    "",
)
# A self-transformation as BW stores one: SOURCETYPE 'DTPA' and SOURCENAME the *DTP's* technical
# name. Reported verbatim it names a load where a data source belongs, which was defect D18 - on a
# production ADSO the dependency list carried a DTP id instead of the ADSO's own name.
_SELF_DTP_ID = "DTP_FIN_SELF"
_TRAN["TRANSFORM03"] = (
    "ACT",
    "DTPA",
    "",
    _SELF_DTP_ID,
    "ADSO",
    "",
    "FIN_ADSO",
    "",
    "",
    "",
    "",
    "",
)
# RSBKDTP: DTP id -> (SRC, SRCTLOGO). The dereference D18 needs.
_DTP_HEADER = {_SELF_DTP_ID: ("FIN_ADSO", "ADSO")}
# RULEID, RULETYPE, AGGR, GROUPTYPE, NO_CONV
_RULES = {
    "TRANSFORM01": [
        (1, "DIRECT", "MOV", "S", ""),  # direct assignment = overwrite
        (2, "ROUTINE", "SUM", "S", "X"),  # summation, conversion suppressed
        (3, "CONSTANT", "", "T", ""),
    ]
}
# RULEID, PARAMTYPE ('1' target / '0' source), FIELDNM, KEYFLAG
_FIELDS = {
    "TRANSFORM01": [
        (1, "1", "GL_ACCOUNT", "X"),  # target + part of the semantic key
        (1, "0", "GLACCT", ""),
        (2, "1", "AMOUNT", ""),
        (2, "0", "DMBTR", ""),
        (2, "0", "WRBTR", ""),
        (3, "1", "COMPANY", ""),
    ]
}
# RULEID, VALUE, INTTYPE, LENGTH, DECIMALS
_CONSTANTS = {"TRANSFORM01": [(3, "1000", "C", 4, 0)]}
# RULEID, STEPID, IOBJNM, MPER, DATEIOBJNM, CONSTANT
_MASTER_LOOKUPS = {"TRANSFORM01": [(2, 1, "COST_CENTER", "2", "POSTING_DATE", "")]}
# RULEID, STEPID, ODSOBJECT/ADSONM, BEHAVIOR, CONSTANT
_DSO_LOOKUPS = {"TRANSFORM01": [(2, 2, "RATES_DSO", "C", "0.00")]}
_ADSO_LOOKUPS: dict[str, list[tuple[Any, ...]]] = {}
_STEPROUT = {"TRANSFORM01": [(2, "CODEFIELD", "NORMAL")]}
_SOURCE = {
    "CODESTART": ["METHOD start_routine.", "  SELECT * FROM mara INTO TABLE lt.", "ENDMETHOD."],
    # A migrated update rule's body lives in the GLOBAL block as a FORM, not in the field routine's
    # own block. The SELECT here is the dependency the field routine really has (D39). The second
    # FORM is never performed, so it must not leak into the wrapper's analysis.
    "CODEGLBL": [
        "* global",
        "DATA gv TYPE i.",
        "FORM routine_0013",
        "  USING &CS& TYPE ty_src",
        "  CHANGING &RS& TYPE ty_tgt.",
        # Synthetic namespace on purpose: the leak check forbids the real generated-table
        # namespaces outside tests/fixtures/, and the parser offers ANY namespaced table to the
        # resolver, so a neutral one exercises the same path.
        "  SELECT SINGLE * FROM /TST/PRATE INTO ls WHERE key = &CS&-cost.",
        "  CALL FUNCTION 'CONVERT_TO_STAT_CURRENCY'.",
        "ENDFORM.",
        "FORM routine_9999",
        "  SELECT * FROM /TST/PNEVER INTO TABLE lt2.",
        "ENDFORM.",
    ],
    # The wrapper: locals, copy the source structure, perform the subroutine, convert messages.
    # Carries no business logic of its own, which is exactly why analysing it alone said nothing.
    "CODEFIELD": [
        "  DATA: l_monitor TYPE STANDARD TABLE OF rsmonitor.",
        "  MOVE-CORRESPONDING &SF& to &CS&.",
        "  Perform routine_0013",
        "  TABLES l_monitor",
        "  USING &CS&",
        "  CHANGING &RS& l_subrc l_abort.",
        "  LOOP AT l_monitor INTO ls_monitor.",
        "    append monitor_rec to &MO&.",
        "  ENDLOOP.",
    ],
}
_TEXT = {"TRANSFORM01": ("E", "Load fin postings", "Load financial postings into ADSO")}


def _endpoint_filtered(sql: str, params: list[Any]) -> list[str]:
    """Transformation ids surviving the built SOURCENAME/TARGETNAME LIKE filters, HANA-style."""
    ids = list(_TRAN)
    index = 0
    for column, position in (("SOURCENAME", 3), ("TARGETNAME", 6)):
        clause = f"{column} LIKE ?"
        if clause not in sql:
            continue
        pattern = str(params[index])
        index += 1
        # Each clause carries its own ESCAPE, so check this clause rather than the whole statement.
        escape = LIKE_ESCAPE if f"{clause} ESCAPE" in sql else None
        ids = [t for t in ids if matches_like(pattern, str(_TRAN[t][position]), escape)]
    return ids


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        name = str(params[-1]) if params else ""
        if "TOTAL_COUNT" in sql:
            return [(len(_endpoint_filtered(sql, params)),)]
        if "RSBKDTP" in sql:
            row = _DTP_HEADER.get(str(params[0]))
            return [row] if row else []
        if "RSTRANSTEPROUT" in sql:
            rows = _STEPROUT.get(name, [])
            if "KIND" in sql:
                return [(r[0], r[1], r[2]) for r in rows]
            return [(r[0], r[1]) for r in rows]
        if "RSTRANSTEPCNST" in sql:
            return list(_CONSTANTS.get(name, []))
        if "RSTRANSTEPMASTER" in sql:
            return list(_MASTER_LOOKUPS.get(name, []))
        if "RSTRANSTEPODSO" in sql:
            return list(_DSO_LOOKUPS.get(name, []))
        if "RSTRANSTEPADSO" in sql:
            return list(_ADSO_LOOKUPS.get(name, []))
        if "RSTRANFIELD" in sql:
            return list(_FIELDS.get(name, []))
        if "RSTRANRULE" in sql:
            return list(_RULES.get(name, []))
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
            return [
                (t, _TRAN[t][1], _TRAN[t][3], _TRAN[t][4], _TRAN[t][6], *_TRAN[t][7:10])
                for t in _endpoint_filtered(sql, params)
            ]
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
    assert total == 3  # TRANSFORM03 is the DTP-sourced self-transformation added for D18
    summary = next(s for s in summaries if s.tran_id == "TRANSFORM01")
    assert summary.source_kind == "datasource"
    assert summary.target_kind == "adso"
    assert summary.has_routines is True
    assert summary.description == "Load fin postings"


def test_list_transformations_matches_padded_datasource_endpoint() -> None:
    """Regression: SOURCENAME/TARGETNAME were compared with '='; a DataSource endpoint is stored
    '<DATASOURCE><padding><LOGSYS>', so filtering by the DataSource name matched nothing."""
    result = _repo().list_transformations(source_name="DATASOURCE_B")
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert [s.tran_id for s in summaries] == ["TRANSFORM02"]
    assert summaries[0].source_name == _PADDED_DS.strip()


def test_list_transformations_target_filter_is_a_substring_match() -> None:
    result = _repo().list_transformations(target_name="FIN_DSO")
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert [s.tran_id for s in summaries] == ["TRANSFORM02"]  # FIN_ADSO must not match


def test_list_transformations_both_endpoint_filters_apply() -> None:
    result = _repo().list_transformations(source_name="DATASOURCE_A", target_name="FIN_ADSO")
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert [s.tran_id for s in summaries] == ["TRANSFORM01"]


def test_get_routine_code() -> None:
    result = _repo().get_routine_code("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    by_code = {c.code_id: c for c in result}
    assert set(by_code) == {"CODESTART", "CODEGLBL", "CODEFIELD"}
    assert by_code["CODESTART"].line_count == 3
    assert by_code["CODESTART"].provenance.source_table == "RSAABAP"
    # Retrieval reports what the source table stores, unchanged: the wrapper is 9 lines and the
    # subroutine body is not spliced into it here. Only the ANALYSIS follows the PERFORM (D39), so
    # that "what is stored under this code id" stays answerable.
    assert by_code["CODEFIELD"].line_count == 9


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


# --- rule depth (slice 3) -----------------------------------------------------------------


def test_aggregation_is_decoded_not_guessed() -> None:
    """AGGR distinguishes overwrite from summation, which changes what a key figure means."""
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    by_rule = {m.rule_id: m for m in tran.field_mappings}
    assert by_rule[1].aggregation == "direct_assignment"  # MOV
    assert by_rule[1].aggregation_code == "MOV"
    assert by_rule[2].aggregation == "summation"  # SUM
    # An empty code decodes to nothing rather than a made-up default.
    assert by_rule[3].aggregation is None
    assert by_rule[3].aggregation_code is None


def test_group_type_and_no_conversion_flags() -> None:
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    by_rule = {m.rule_id: m for m in tran.field_mappings}
    assert by_rule[1].group_type == "standard"
    assert by_rule[3].group_type == "technical_fields"
    assert by_rule[2].no_conversion is True
    assert by_rule[1].no_conversion is False


def test_key_fields_are_separated_from_plain_targets() -> None:
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    by_rule = {m.rule_id: m for m in tran.field_mappings}
    assert by_rule[1].target_fields == ["GL_ACCOUNT"]
    assert by_rule[1].key_fields == ["GL_ACCOUNT"]  # KEYFLAG = 'X'
    assert by_rule[2].key_fields == []  # target, but not part of the key


def test_constant_value_is_surfaced_with_its_type() -> None:
    """The rule type alone cannot distinguish a business default from a technical zero-fill."""
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    constant = {m.rule_id: m.constant for m in tran.field_mappings}[3]
    assert constant is not None
    assert constant.value == "1000"
    assert constant.internal_type == "C"
    assert constant.internal_type_label == "character"
    assert constant.length == 4


def test_declared_lookups_are_exact_and_carry_miss_behaviour() -> None:
    tran = _repo().get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    lookups = {lookup.object_name: lookup for lookup in tran.declared_lookups}
    assert set(lookups) == {"COST_CENTER", "RATES_DSO"}

    master = lookups["COST_CENTER"]
    assert master.kind == "master_data"
    assert master.derivation == "declared"  # exact, unlike routine-parsed reads
    assert master.key_date == "period_end"  # MPER = '2'
    assert master.key_date_field == "POSTING_DATE"

    dso = lookups["RATES_DSO"]
    assert dso.kind == "dso"
    # BEHAVIOR 'C': a miss substitutes a constant instead of failing the record.
    assert dso.miss_behaviour == "constant"
    assert dso.miss_constant == "0.00"


def test_a_dtp_typed_source_endpoint_is_dereferenced_to_the_object() -> None:
    """D18: BW stores a self-transformation's source as a DTP id, not an object."""
    tran = _repo().get_transformation("TRANSFORM03")
    assert not isinstance(tran, UnsupportedResult)
    assert tran.source is not None
    assert tran.source.name == "FIN_ADSO", "the DTP id is standing in for a source object"
    assert tran.source.kind == "adso"
    assert tran.source.via_dtp == _SELF_DTP_ID, "the load must stay identifiable"
    assert tran.source.unresolved_dtp is False


def test_an_unresolvable_dtp_endpoint_says_so_instead_of_looking_resolved() -> None:
    """Without RSBKDTP the name cannot be dereferenced; that must be stated, not disguised."""
    tran = _repo(set(_TABLES) - {"dtp"}).get_transformation("TRANSFORM03")
    assert not isinstance(tran, UnsupportedResult)
    assert tran.source is not None
    assert tran.source.name == _SELF_DTP_ID  # kept, not dropped
    assert tran.source.unresolved_dtp is True
    assert tran.source.via_dtp == _SELF_DTP_ID


def test_missing_lookup_tables_are_declared_as_a_caveat() -> None:
    """An empty lookup list must be distinguishable from 'this release cannot tell us'."""
    present = set(_TABLES) - {
        "transformation_step_master",
        "transformation_step_dso",
        "transformation_step_adso",
        "transformation_step_const",
    }
    tran = _repo(present).get_transformation("TRANSFORM01")
    assert not isinstance(tran, UnsupportedResult)
    assert tran.declared_lookups == []
    assert any("RSTRANSTEPMASTER" in c for c in tran.caveats)


# --- D39: a migrated update rule's analysis must describe the body, not the wrapper -----------
# Found by S02. BW splits this kind of field routine in two: the field routine's own source block is
# a short wrapper that performs a subroutine, and the subroutine is a FORM in the transformation's
# global block. Measured on production: 1,859 of 10,383 source blocks are wrappers, and 63 of the
# bodies they perform contain a SELECT - dependencies that exist, were already retrieved, and were
# being reported as none.


def test_migrated_update_rule_analysis_follows_the_performed_subroutine() -> None:
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    field = next(a for a in result if a.code_id == "CODEFIELD")
    tables = {d.table.lower() for d in field.table_dependencies}
    assert "/tst/prate" in tables, "the subroutine's SELECT is the field routine's real dependency"


def test_the_wrapper_analysis_says_where_the_body_came_from() -> None:
    """A line number in the result must remain locatable, so the splice is stated, not silent."""
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    field = next(a for a in result if a.code_id == "CODEFIELD")
    spliced = [c for c in field.caveats if "routine_0013" in c]
    assert spliced, "the caveat must name the subroutine that was followed"
    assert "CODEGLBL" in spliced[0], "and the code id its body came from"
    assert "after line 9" in spliced[0], "and where in this routine's own source it was appended"


def test_a_subroutine_that_is_not_performed_does_not_leak_in() -> None:
    """Appending every FORM in the global block would attribute unrelated reads to this routine."""
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    field = next(a for a in result if a.code_id == "CODEFIELD")
    tables = {d.table.lower() for d in field.table_dependencies}
    assert "/tst/pnever" not in tables


def test_the_global_block_is_not_double_counted_against_itself() -> None:
    """The block defining the FORM already contains its statements; appending them again would
    report the same dependency twice."""
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    glbl = next(a for a in result if a.code_id == "CODEGLBL")
    rate = [d for d in glbl.table_dependencies if d.table.lower() == "/tst/prate"]
    assert len(rate) == 1
    assert not any("body is defined in code id" in c for c in glbl.caveats)


def test_dynamic_perform_in_program_is_not_resolved_as_a_local_subroutine() -> None:
    """``PERFORM x IN PROGRAM (y)`` resolves at runtime. Treating it as a local FORM would claim a
    resolution that was never made - the exact shape of mission Known Limitation 3."""
    lines = [
        "  CONCATENATE 'PRE_' i_vnam INTO lv_prog.",
        "  PERFORM execute_variable_exit",
        "    IN PROGRAM (lv_prog)",
        "    USING i_vnam.",
        "  PERFORM local_helper.",
    ]
    assert performed_forms(lines) == ["local_helper"]
    assert form_bodies(lines) == {}


def test_a_form_without_endform_is_skipped_rather_than_guessed() -> None:
    bodies = form_bodies(
        ["FORM good.", "  SELECT * FROM a.", "ENDFORM.", "FORM truncated.", "  SELECT * FROM b."]
    )
    assert set(bodies) == {"good"}
    assert bodies["good"] == ["  SELECT * FROM a."]


def test_a_followed_subroutine_stops_counting_as_an_unfollowed_call() -> None:
    """The evidence line reports how many calls were named but not followed. A subroutine whose body
    was spliced in *was* followed, so leaving it in that count overstates the shortfall - the mirror
    of the error that omitting a caveat would be."""
    result = _repo().analyze_routines("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    field = next(a for a in result if a.code_id == "CODEFIELD")
    forms = [r for r in field.unresolved_refs if r.call_kind == "form"]
    assert not any(r.object_name.lower() == "routine_0013" for r in forms)
    # The function module inside the body is a different matter: it genuinely was not followed, and
    # naming it is the useful form of a lower bound.
    modules = [
        r.object_name.upper() for r in field.unresolved_refs if r.call_kind == "function_module"
    ]
    assert "CONVERT_TO_STAT_CURRENCY" in modules
    assert field.complexity.call_count == len(field.unresolved_refs)
