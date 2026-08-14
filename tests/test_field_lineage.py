"""Tests for field-level lineage.

The defect this replaces: every InfoObject in a query returned a byte-identical path (the provider's
upstream DataSource set), while the per-InfoObject response shape implied each had been traced
individually. The test that matters is therefore not "a path is returned" but **"different fields
return different paths"** — that is the only assertion the old behaviour could not satisfy.

Landscape (synthetic; the DataSource endpoint is space-padded as BW stores it):

    DS_SALES<pad>SRC100  --TR_STAGE-->  STAGE_DSO  --TR_MART-->  MART_DSO

    MART_DSO.AMOUNT      <- direct     <- STAGE_DSO.NET_VALUE <- direct <- DS field NETWR
    MART_DSO.MARGIN      <- routine    <- STAGE_DSO.COST      (advisory)
    MART_DSO.REGION      <- constant   (no source field: the walk ends there)
    MART_DSO.ORPHAN      <- no rule    (falls back to provider level, and says so)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.services.field_lineage import FieldLineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
}

_PADDED_DS = "DS_SALES".ljust(30) + "SRC100"

# OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME, START, END, EXPERT, GLB, GLB2
_HEADER: dict[str, tuple[Any, ...]] = {
    "TR_MART": ("ACT", "ODSO", "", "STAGE_DSO", "ODSO", "", "MART_DSO", "", "", "", "", ""),
    "TR_STAGE": ("ACT", "RSDS", "", _PADDED_DS, "ODSO", "", "STAGE_DSO", "", "", "", "", ""),
}
# RULEID, RULETYPE, AGGR, GROUPTYPE, NO_CONV
_RULES: dict[str, list[tuple[Any, ...]]] = {
    "TR_MART": [
        (1, "DIRECT", "MOV", "S", ""),
        (2, "ROUTINE", "SUM", "S", ""),
        (3, "CONSTANT", "", "S", ""),
    ],
    "TR_STAGE": [(1, "DIRECT", "MOV", "S", "")],
}
# RULEID, PARAMTYPE ('1' target / '0' source), FIELDNM, KEYFLAG
_FIELDS: dict[str, list[tuple[Any, ...]]] = {
    "TR_MART": [
        (1, "1", "AMOUNT", ""),
        (1, "0", "NET_VALUE", ""),
        (2, "1", "MARGIN", ""),
        (2, "0", "COST", ""),
        (3, "1", "REGION", ""),  # constant: no source field
    ],
    "TR_STAGE": [
        (1, "1", "NET_VALUE", ""),
        (1, "0", "NETWR", ""),
    ],
}
_STEPROUT = {"TR_MART": [(2, "CODE_MARGIN", "NORMAL")]}
_SOURCE = {"CODE_MARGIN": ["METHOD field.", "  result = src-cost * 2.", "ENDMETHOD."]}
_INBOUND = {"MART_DSO": ["TR_MART"], "STAGE_DSO": ["TR_STAGE"]}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        name = str(params[0]).strip() if params else ""
        if "RSTRANSTEPROUT" in sql:
            rows = _STEPROUT.get(name, [])
            return (
                [(r[0], r[1], r[2]) for r in rows]
                if "KIND" in sql
                else [(r[0], r[1]) for r in rows]
            )
        if "RSAABAP" in sql:
            return [(line,) for line in _SOURCE.get(name, [])]
        if "RSTRANFIELD" in sql:
            return list(_FIELDS.get(name, []))
        if "RSTRANRULE" in sql:
            return list(_RULES.get(name, []))
        if "RSTRAN" in sql:
            if "TARGETNAME = ?" in sql:  # inbound transformations for one target
                return [(t,) for t in _INBOUND.get(name, [])]
            header = _HEADER.get(name)
            return [header] if header else []
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


def _service() -> FieldLineageService:
    return FieldLineageService(ScriptedConnection(), _capability())


# --- the property the old implementation could not satisfy ---------------------------------


def test_different_fields_get_different_paths() -> None:
    """The regression: every InfoObject used to return the provider's upstream set verbatim."""
    service = _service()
    amount = service.trace_field("MART_DSO", "AMOUNT")
    margin = service.trace_field("MART_DSO", "MARGIN")
    assert [h.rule_type for h in amount.hops[1:]] != [h.rule_type for h in margin.hops[1:]]
    assert amount.hops[1].source_fields == ["NET_VALUE"]
    assert margin.hops[1].source_fields == ["COST"]


# --- following a field through two layers to the DataSource --------------------------------


def test_direct_field_reaches_the_datasource_through_both_layers() -> None:
    path = _service().trace_field("MART_DSO", "AMOUNT")
    assert path.resolution == "field"
    assert path.reaches_datasource is True
    assert [hop.object_name for hop in path.hops] == ["MART_DSO", "STAGE_DSO", _PADDED_DS.strip()]


def test_each_hop_records_the_rule_and_the_source_field() -> None:
    path = _service().trace_field("MART_DSO", "AMOUNT")
    mart_hop, stage_hop = path.hops[1], path.hops[2]
    assert (mart_hop.target_field, mart_hop.rule_type) == ("AMOUNT", "direct")
    assert mart_hop.source_fields == ["NET_VALUE"]
    assert mart_hop.transformation_id == "TR_MART"
    # The second layer tracks the *source* field name, not the original InfoObject.
    assert (stage_hop.target_field, stage_hop.source_fields) == ("NET_VALUE", ["NETWR"])
    assert stage_hop.via == "datasource"


def test_padded_datasource_endpoint_is_the_boundary() -> None:
    path = _service().trace_field("MART_DSO", "AMOUNT")
    assert path.hops[-1].object_type == "datasource"
    assert path.reaches_datasource is True


# --- honesty about routines ----------------------------------------------------------------


def test_routine_hop_is_marked_advisory_and_carries_the_code_id() -> None:
    path = _service().trace_field("MART_DSO", "MARGIN")
    hop = path.hops[1]
    assert hop.rule_type == "routine"
    assert hop.advisory is True
    assert hop.routine_code_id == "CODE_MARGIN"
    assert hop.note is not None and "lower bound" in hop.note
    assert path.has_routine_hop is True


def test_direct_hop_is_not_advisory() -> None:
    path = _service().trace_field("MART_DSO", "AMOUNT")
    assert all(hop.advisory is False for hop in path.hops)
    assert path.has_routine_hop is False


# --- stopping honestly ---------------------------------------------------------------------


def test_constant_rule_stops_and_explains_why() -> None:
    path = _service().trace_field("MART_DSO", "REGION")
    assert path.resolution == "field"
    assert path.hops[1].rule_type == "constant"
    assert path.reaches_datasource is False
    assert path.unresolved_reason is not None
    assert "no source field" in path.unresolved_reason


def test_field_with_no_rule_is_reported_not_guessed() -> None:
    path = _service().trace_field("MART_DSO", "ORPHAN")
    assert path.resolution == "none"
    assert path.reaches_datasource is False
    assert path.unresolved_reason is not None
    assert "no transformation rule" in path.unresolved_reason


def test_unavailable_transformation_table_yields_no_hops() -> None:
    service = FieldLineageService(ScriptedConnection(), _capability(present=set()))
    path = service.trace_field("MART_DSO", "AMOUNT")
    assert path.resolution == "none"
    assert [hop.object_name for hop in path.hops] == ["MART_DSO"]


def test_depth_is_bounded() -> None:
    path = _service().trace_field("MART_DSO", "AMOUNT", max_depth=1)
    assert len(path.hops) == 2  # the provider plus one hop
    assert path.reaches_datasource is False


# --- cost ----------------------------------------------------------------------------------


def test_tracing_many_fields_reuses_the_transformation_reads() -> None:
    """Per-instance memos: every field of a provider shares its inbound transformations."""

    class Counting(ScriptedConnection):
        def __init__(self) -> None:
            self.count = 0

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            self.count += 1
            return super().execute_select(sql, parameters)

    connection = Counting()
    service = FieldLineageService(connection, _capability())
    service.trace_field("MART_DSO", "AMOUNT")
    after_first = connection.count
    for field in ("MARGIN", "REGION", "AMOUNT"):
        service.trace_field("MART_DSO", field)
    assert connection.count < after_first * 4, "memoisation is not reducing repeat reads"
