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
    # The CompositeProvider's stored model. Without this hop a field of a CompositeProvider resolves
    # nothing at all - there is no transformation targeting one - and on BW-on-HANA that is what a
    # BEx query normally reads.
    "composite_header": "RSOHCPR",
    # A navigation attribute is populated by no transformation rule at all, so without this the walk
    # stops at the provider and reports "no rule found" for a field whose lineage is fully knowable
    # (D23/D32).
    "nav_attribute": "RSDATRNAV",
    # A MultiProvider has no transformation either - it unions its parts - so without this every
    # field of one resolves nothing. Identification also names the field's name *inside* the part,
    # which is not always the provider's own name (D22).
    "multiprovider_identification": "RSDICMULTIIOBJ",
    # RSDCHA.CHABASNM: a reference characteristic holds no master data of its own, so a walk that
    # arrives at one must continue at the characteristic it references (D35).
    "characteristic": "RSDCHA",
}

_PADDED_DS = "DS_SALES".ljust(30) + "SRC100"

# A CompositeProvider unioning the mart with a second ADSO. AMOUNT is fed by both, which is the
# normal shape and the one a single-answer reader gets wrong.
_CP_MODEL = """<?xml version="1.0" encoding="utf-8"?>
<Composite:compositeView xmlns:Composite="urn:c" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    name="SALES_CP" withHanaModel="true" defaultNode="#///U1">
  <viewNode xsi:type="View:Union" name="U1">
    <element xsi:type="BwCore:BwElement" name="AMOUNT" infoObjectName="AMOUNT"/>
    <element xsi:type="BwCore:BwElement" name="FIXED" infoObjectName="FIXED"/>
    <input xsi:type="Composite:CompositeInput" alias="U1.ODSO.1" selectAll="false">
      <entity>MART_DSO.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="AMOUNT" sourceName="AMOUNT"/>
      <mapping xsi:type="Type:ConstantElementMapping" targetName="FIXED" sourceName="X"/>
    </input>
    <input xsi:type="Composite:CompositeInput" alias="U1.CALC.2" selectAll="false">
      <entity>PKG/SUB/CV_EXTRA.calculationview#/</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="AMOUNT" sourceName="EXTRA_VALUE"/>
    </input>
  </viewNode>
</Composite:compositeView>
"""
_COMPOSITE_MODELS = {"SALES_CP": _CP_MODEL}

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

# RSDATRNAV: ATRNAVNM -> (CHANM, ATTRINM). CUSTOMER__COUNTRY is a navigation attribute of the
# characteristic CUSTOMER. It appears in the provider's field list under its own name and nothing in
# the field row says its value is not stored there.
#
# Matched on stored ATRNAVNM rather than split on '__', for the reason ProvidersRepository gives:
# the convention holds for 4,127 of 4,129 rows on the reference system, so splitting would invent a
# characteristic for the other two. CUSTOMER__ODD_ONE is that case - a name that looks splittable
# but has no row, so it must stay unresolved instead of yielding a fabricated 'CUSTOMER'.
_NAV_ATTRIBUTES: dict[str, tuple[str, str]] = {
    "CUSTOMER__COUNTRY": ("CUSTOMER", "COUNTRY"),
}

# Master data fans in: two transformations load CUSTOMER's attributes from different sources, which
# is the normal shape for an InfoObject and the one that makes following a single branch silently
# misleading. TR_CUST_A loads COUNTRY; TR_CUST_B also loads it, from a second system.
_HEADER.update(
    {
        "TR_CUST_A": (
            "ACT",
            "RSDS",
            "",
            "DS_CUST_MAIN".ljust(30) + "SRC100",
            "IOBJ",
            "",
            "CUSTOMER",
            "",
            "",
            "",
            "",
            "",
        ),
        "TR_CUST_B": (
            "ACT",
            "RSDS",
            "",
            "DS_CUST_EXTRA".ljust(30) + "SRC200",
            "IOBJ",
            "",
            "CUSTOMER",
            "",
            "",
            "",
            "",
            "",
        ),
    }
)
_RULES.update(
    {
        "TR_CUST_A": [(1, "DIRECT", "MOV", "S", "")],
        "TR_CUST_B": [(1, "DIRECT", "MOV", "S", "")],
    }
)
_FIELDS.update(
    {
        "TR_CUST_A": [(1, "1", "COUNTRY", ""), (1, "0", "LAND1", "")],
        "TR_CUST_B": [(1, "1", "COUNTRY", ""), (1, "0", "CTRY_CODE", "")],
    }
)
_INBOUND = {
    "MART_DSO": ["TR_MART"],
    "STAGE_DSO": ["TR_STAGE"],
    "CUSTOMER": ["TR_CUST_A", "TR_CUST_B"],
}

# RSDICMULTIIOBJ: a MultiProvider's InfoObject identification, (IOBJNM, PARTCUBE, PARTIOBJ) per
# provider. SALES_MP unions the mart with a second part.
#   AMOUNT      - supplied by both parts under the same name (the fan-out shape)
#   RENAMED_KF  - supplied by MART_DSO under the DIFFERENT name AMOUNT, which is the case a
#                 same-name shortcut resolves to a field that is not there
#   NOT_UNIONED - deliberately absent, so "no part supplies it" can be told apart from "no rule"
_MULTI_IDENTIFICATION: dict[str, list[tuple[str, str, str]]] = {
    "SALES_MP": [
        ("AMOUNT", "MART_DSO", "AMOUNT"),
        ("AMOUNT", "MART_TWO", "AMOUNT"),
        ("RENAMED_KF", "MART_DSO", "AMOUNT"),
    ],
}

# RSDCHA.CHABASNM. PAYER is a reference characteristic of CUSTOMER: it holds no master data, so
# nothing loads into it and its attributes are columns of CUSTOMER's master data. CUSTOMER
# references itself, which is how BW stores an ordinary characteristic - the hop must not fire for
# that, or every characteristic walk cycles through itself (6,578 of 8,879 on the reference system).
_REFERENCE: dict[str, str] = {
    "PAYER": "CUSTOMER",
    "CUSTOMER": "CUSTOMER",
}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        name = str(params[0]).strip() if params else ""
        if "RSOHCPR" in sql:
            model = _COMPOSITE_MODELS.get(name)
            if "LENGTH(XML_UI)" in sql:
                return [(len(model.encode()) if model else 0,)]
            if "LENGTH(XML_DEF)" in sql:
                return [(0,)]  # empty, as on the release measured
            if "XML_UI" in sql:
                return [(model.encode(),)] if model else []
            return []
        if "RSTRANSTEPROUT" in sql:
            rows = _STEPROUT.get(name, [])
            return (
                [(r[0], r[1], r[2]) for r in rows]
                if "KIND" in sql
                else [(r[0], r[1]) for r in rows]
            )
        if "RSAABAP" in sql:
            return [(line,) for line in _SOURCE.get(name, [])]
        if "RSDATRNAV" in sql:
            match = _NAV_ATTRIBUTES.get(name)
            return [match] if match else []
        if "RSDICMULTIIOBJ" in sql:
            return list(_MULTI_IDENTIFICATION.get(name, []))
        if "RSDCHA" in sql:
            base = _REFERENCE.get(name)
            return [(base,)] if base is not None else []
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
    # The DataSource is named without its logical-system suffix. BW stores the endpoint padded to 30
    # characters and suffixed with the logical system, and this used to be reported verbatim - a
    # tolerable wart while DataSource endpoints were rare, but D32 makes one the common terminus
    # because master-data lineage lands there at nearly every branch. BDLS rewrites the suffix per
    # landscape, so it is not part of the DataSource's identity.
    assert [hop.object_name for hop in path.hops] == ["MART_DSO", "STAGE_DSO", "DS_SALES"]
    assert _PADDED_DS.startswith("DS_SALES"), "the stored form really is padded"
    assert _PADDED_DS != "DS_SALES", "so this assertion is not vacuous"


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
    # Names all three routes that were actually tried, so the message cannot imply a route was
    # checked when it was not (D22 added the MultiProvider one).
    assert "no rule, CompositeProvider mapping or MultiProvider identification" in (
        path.unresolved_reason
    )


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


# --- crossing a CompositeProvider ----------------------------------------------------------
#
# A CompositeProvider has no transformation, so before this hop existed the walk found nothing for
# any of its fields and every one fell back to provider level. Measured on a production query: 100
# of 100 InfoObjects. The mapping is declared in BW's stored model, so the hop is exact.


def test_a_composite_field_resolves_through_the_declared_model_to_the_part() -> None:
    path = _service().trace_field("SALES_CP", "AMOUNT")
    assert path.resolution == "field", "a CompositeProvider field used to resolve nothing at all"
    hop = path.hops[1]
    assert hop.via == "composite_part"
    assert hop.rule_type == "composite_mapping"
    assert hop.advisory is False, "BW's own stored model is read whole, not inferred"
    assert hop.evidence is not None and hop.evidence.basis == "observed"


def test_the_walk_continues_through_the_part_to_the_datasource() -> None:
    """The point of the hop: the chain does not stop at the CompositeProvider's part."""
    path = _service().trace_field("SALES_CP", "AMOUNT")
    assert [h.object_name for h in path.hops] == [
        "SALES_CP",
        "MART_DSO",
        "STAGE_DSO",
        "DS_SALES",  # logical-system suffix removed; see the direct-field test above
    ]
    assert path.reaches_datasource is True


def test_a_union_field_names_every_part_that_supplies_it() -> None:
    """Following one branch quietly names an arbitrary source for a figure that has several."""
    path = _service().trace_field("SALES_CP", "AMOUNT")
    hop = path.hops[1]
    assert hop.source_objects == ["MART_DSO", "PKG/SUB/CV_EXTRA"]
    assert hop.note is not None and "2 parts supply this element" in hop.note
    assert "source_objects lists them all" in hop.note


def test_the_chain_follows_a_reproducible_branch_not_document_order() -> None:
    service = _service()
    first = service.trace_field("SALES_CP", "AMOUNT")
    second = FieldLineageService(ScriptedConnection(), _capability()).trace_field(
        "SALES_CP", "AMOUNT"
    )
    assert [h.object_name for h in first.hops] == [h.object_name for h in second.hops]


def test_a_constant_in_the_composite_model_stops_and_says_so() -> None:
    path = _service().trace_field("SALES_CP", "FIXED")
    assert path.resolution == "field"
    hop = path.hops[1]
    assert hop.rule_type == "composite_constant"
    assert hop.source_fields == []
    assert path.reaches_datasource is False
    assert path.unresolved_reason is not None and "no source field" in path.unresolved_reason


def test_a_calc_view_part_is_a_named_stop_not_a_failure() -> None:
    """The field really comes from there; its lineage is in the HANA catalogue, not in BW."""

    class CalcOnly(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSOHCPR" in sql and "XML_UI" in sql and "LENGTH" not in sql:
                only_calc = _CP_MODEL.replace(
                    '<mapping xsi:type="Type:ElementMapping" targetName="AMOUNT" '
                    'sourceName="AMOUNT"/>',
                    "",
                )
                return [(only_calc.encode(),)]
            return super().execute_select(sql, parameters)

    path = FieldLineageService(CalcOnly(), _capability()).trace_field("SALES_CP", "AMOUNT")
    assert path.resolution == "field"
    hop = path.hops[1]
    assert hop.via == "calc_view"
    assert hop.object_name == "PKG.SUB/CV_EXTRA", "the runtime name is what the catalogue holds"
    assert path.reaches_datasource is False
    assert path.unresolved_reason is not None
    assert "bw_get_calc_view_lineage" in path.unresolved_reason


def test_a_transformation_rule_still_wins_over_the_composite_route() -> None:
    """The rule route carries *how* the field was derived, so it is tried first."""
    path = _service().trace_field("MART_DSO", "AMOUNT")
    assert path.hops[1].via == "transformation"
    assert path.hops[1].rule_type == "direct"


def test_an_absent_composite_table_leaves_the_field_unresolved_not_wrong() -> None:
    service = FieldLineageService(
        ScriptedConnection(), _capability(present=set(_TABLES) - {"composite_header"})
    )
    path = service.trace_field("SALES_CP", "AMOUNT")
    assert path.resolution == "none"
    assert path.unresolved_reason is not None


# --- D23 / D32: navigation attributes ------------------------------------------------------
#
# A navigation attribute is not populated into the provider by any transformation rule - its value
# is read at query time from the master data of the characteristic it hangs off. Both routes above
# look somewhere the answer cannot be, and the walk used to stop with "no rule ... the field **may
# be** a navigation attribute". Two things were wrong with that: it is decidable rather than a maybe
# (RSDATRNAV says so), and having decided, the lineage continues. On the subject query this affected
# 16 of 45 paths; on the characteristic the customer supplied the continuation is 22 transformations
# reaching 10 DataSources across 3 source systems.


def test_navigation_attribute_is_stated_as_fact_not_offered_as_a_guess() -> None:
    """D23: the hedge is replaced by the answer, read from RSDATRNAV."""
    path = _service().trace_field("SALES_CP", "CUSTOMER__COUNTRY")
    nav = next(h for h in path.hops if h.via == "nav_attribute")
    assert nav.object_name == "CUSTOMER"
    assert nav.rule_type == "navigation_attribute"
    assert nav.source_fields == ["COUNTRY"]
    assert nav.advisory is False, "BW's own stored attribute model, read whole"
    assert "is a navigation attribute" in (nav.note or "")
    assert "RSDATRNAV" in (nav.note or ""), "the note must cite what established it"


def test_navigation_attribute_lineage_continues_to_the_datasource() -> None:
    """D32: the point of naming it is being able to follow it.

    Before this the path stopped at the provider with reaches_datasource=False, which is why the
    sealed answer named the characteristic 20 times and none of its 10 upstream DataSources.
    """
    path = _service().trace_field("SALES_CP", "CUSTOMER__COUNTRY")
    assert path.reaches_datasource is True
    assert path.resolution == "field"
    assert [h.via for h in path.hops] == ["provider", "nav_attribute", "datasource"]
    assert path.hops[-1].object_type == "datasource"
    assert path.unresolved_reason is None


def test_master_data_fan_in_names_every_transformation_that_loads_the_attribute() -> None:
    """An InfoObject's attributes come from several systems; following one silently would mislead.

    This is the same property the CompositeProvider hop already had, brought to the transformation
    hop because D32 puts master data - where wide fan-in is the norm - in scope.
    """
    path = _service().trace_field("SALES_CP", "CUSTOMER__COUNTRY")
    ds_hop = path.hops[-1]
    assert ds_hop.source_objects == ["DS_CUST_EXTRA", "DS_CUST_MAIN"], "sorted, so reproducible"
    assert ds_hop.object_name == "DS_CUST_EXTRA", "the chain follows the first in that same order"
    assert "2 transformations populate this field" in (ds_hop.note or "")
    assert "source_objects lists them all" in (ds_hop.note or "")


def test_a_name_that_looks_like_an_attribute_but_has_no_row_stays_unresolved() -> None:
    """The convention is not the authority: RSDATRNAV is.

    'CUSTOMER__ODD_ONE' splits cleanly on '__' and would yield characteristic 'CUSTOMER' if the name
    were parsed. It has no stored row, so it must resolve to nothing rather than to a fabrication -
    the 2-of-4,129 case the providers repository already refuses to guess at.
    """
    path = _service().trace_field("SALES_CP", "CUSTOMER__ODD_ONE")
    assert not any(h.via == "nav_attribute" for h in path.hops)
    assert path.reaches_datasource is False


def test_the_unresolved_message_no_longer_offers_a_possibility_it_has_ruled_out() -> None:
    """Once RSDATRNAV has been consulted, "may be a navigation attribute" is a false hedge."""
    path = _service().trace_field("MART_DSO", "NO_SUCH_FIELD")
    reason = path.unresolved_reason or ""
    assert "may be a navigation attribute" not in reason
    assert "not a navigation attribute" in reason
    assert "RSDATRNAV" in reason, "say what was checked, so the claim is auditable"


def test_without_rsdatrnav_the_attribute_is_not_claimed_either_way() -> None:
    """On a release without the table the walk must not assert what it cannot read."""
    service = FieldLineageService(
        ScriptedConnection(), _capability(set(_TABLES) - {"nav_attribute"})
    )
    path = service.trace_field("SALES_CP", "CUSTOMER__COUNTRY")
    assert not any(h.via == "nav_attribute" for h in path.hops)
    reason = path.unresolved_reason or ""
    assert "is not a navigation attribute" not in reason, "never assert from a table never read"
    assert "could not be checked" in reason
    assert "absent on this release" in reason


def test_the_nav_attribute_lookup_is_cached_per_call() -> None:
    """Every field of a query asks; "not one" is an answer worth keeping."""

    class Counting(ScriptedConnection):
        def __init__(self) -> None:
            self.nav_reads = 0

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDATRNAV" in sql:
                self.nav_reads += 1
            return super().execute_select(sql, parameters)

    conn = Counting()
    service = FieldLineageService(conn, _capability())
    for _ in range(4):
        service.trace_field("SALES_CP", "CUSTOMER__COUNTRY")
    assert conn.nav_reads == 1


def test_a_self_transformation_is_not_the_branch_the_chain_follows() -> None:
    """An object loaded from itself cannot advance the walk, only reach the cycle guard.

    Measured on one of the subject query's attributes: the self-reference sorted first, the walk
    stopped with "cycle detected", and the standard attribute DataSource sat beside it in the same
    fan-out unused. The self-transformation is real, so it stays in source_objects - it is just not
    the branch worth following.
    """
    _HEADER["TR_SELF"] = (
        "ACT",
        "IOBJ",
        "",
        "CUSTOMER",
        "IOBJ",
        "",
        "CUSTOMER",
        "",
        "",
        "",
        "",
        "",
    )
    _RULES["TR_SELF"] = [(1, "DIRECT", "MOV", "S", "")]
    _FIELDS["TR_SELF"] = [(1, "1", "COUNTRY", ""), (1, "0", "COUNTRY", "")]
    _INBOUND["CUSTOMER"] = ["TR_SELF", "TR_CUST_A", "TR_CUST_B"]
    try:
        path = _service().trace_field("SALES_CP", "CUSTOMER__COUNTRY")
        assert path.reaches_datasource is True, "the self-reference must not strand the walk"
        assert "cycle detected" not in (path.unresolved_reason or "")
        ds_hop = path.hops[-1]
        assert ds_hop.object_type == "datasource"
        assert "CUSTOMER" in ds_hop.source_objects, "still reported: it does exist"
    finally:
        for store in (_HEADER, _RULES, _FIELDS):
            store.pop("TR_SELF", None)
        _INBOUND["CUSTOMER"] = ["TR_CUST_A", "TR_CUST_B"]


def test_a_constant_branch_is_not_preferred_over_one_carrying_a_source_field() -> None:
    """A constant rule terminates the walk, so it is not the branch to follow when another exists.

    Observed on the subject query: alphabetical order alone picked a DataSource whose rule was a
    constant over one with a direct rule and a real source field, turning an informative answer into
    a terminal one for no reason.
    """
    _HEADER["TR_CUST_C"] = (
        "ACT",
        "RSDS",
        "",
        "AAA_FIRST_ALPHABETICALLY".ljust(30) + "SRC300",
        "IOBJ",
        "",
        "CUSTOMER",
        "",
        "",
        "",
        "",
        "",
    )
    _RULES["TR_CUST_C"] = [(1, "CONSTANT", "", "S", "")]
    _FIELDS["TR_CUST_C"] = [(1, "1", "COUNTRY", "")]  # target only: no source field
    _INBOUND["CUSTOMER"] = ["TR_CUST_C", "TR_CUST_A", "TR_CUST_B"]
    try:
        path = _service().trace_field("SALES_CP", "CUSTOMER__COUNTRY")
        ds_hop = path.hops[-1]
        assert "AAA_FIRST_ALPHABETICALLY" in ds_hop.source_objects, "still reported"
        assert ds_hop.object_name != "AAA_FIRST_ALPHABETICALLY", "but not the branch followed"
        assert ds_hop.rule_type == "direct"
        assert ds_hop.source_fields, "the followed branch carries a source field"
    finally:
        for store in (_HEADER, _RULES, _FIELDS):
            store.pop("TR_CUST_C", None)
        _INBOUND["CUSTOMER"] = ["TR_CUST_A", "TR_CUST_B"]


# --- D22: a MultiProvider field resolves instead of stopping at the provider ----------------
# On the subject query 29 of 45 field paths stopped at the provider, and every one of them was a
# field whose data arrives through the MultiProvider's parts. A MultiProvider has no inbound
# transformation at all (measured: 0 of 1,274 active transformations target one), so the union is
# the only route, and BW records it in RSDICMULTIIOBJ.


def test_multiprovider_field_continues_into_the_part() -> None:
    """D22: the walk crosses the union and keeps going to the DataSource."""
    path = _service().trace_field("SALES_MP", "AMOUNT")
    assert path.resolution == "field"
    assert path.reaches_datasource is True
    # The union hop is inserted ahead of the part's own chain and changes nothing else about it:
    # asserting it that way keeps the test honest if the part's chain is ever restructured. (A
    # transformation whose source is a DataSource is itself the 'datasource' hop, which is why there
    # is no separate 'transformation' hop for it.)
    direct = _service().trace_field("MART_DSO", "AMOUNT")
    assert [h.via for h in path.hops] == ["provider", "multiprovider_part"] + [
        h.via for h in direct.hops[1:]
    ]
    assert [h.via for h in path.hops] == [
        "provider",
        "multiprovider_part",
        "transformation",
        "datasource",
    ]


def test_multiprovider_hop_is_declared_not_advisory() -> None:
    """Identification is BW's own stored mapping, so the hop is exact - like the CP hop, not a
    naming convention."""
    path = _service().trace_field("SALES_MP", "AMOUNT")
    hop = next(h for h in path.hops if h.via == "multiprovider_part")
    assert hop.advisory is False
    assert hop.rule_type == "multiprovider_identification"
    assert hop.evidence is not None
    # 'observed', the same standing the CompositeProvider mapping hop carries: BW's stored model,
    # not a naming convention. Anything weaker would misrepresent it as a guess.
    assert hop.evidence.basis == "observed"
    assert hop.evidence.method == "declared_multiprovider_identification"
    assert "RSDICMULTIIOBJ" in (hop.evidence.detail or "")


def test_multiprovider_fan_out_names_every_supplying_part() -> None:
    """A union is normally fed the same field by several parts; following one silently is the
    failure. ``object_name`` is the branch taken, ``source_objects`` is the whole set."""
    path = _service().trace_field("SALES_MP", "AMOUNT")
    hop = next(h for h in path.hops if h.via == "multiprovider_part")
    assert hop.source_objects == ["MART_DSO", "MART_TWO"]
    assert hop.object_name == "MART_DSO"  # first in sorted order: a choice, not row order
    assert "2 parts" in (hop.note or "")
    assert "MART_TWO" in (hop.note or ""), "the note must name the branch not taken"


def test_multiprovider_identification_rename_is_read_not_assumed() -> None:
    """The case a same-name shortcut gets wrong: the provider's field is called something else
    inside the part, so the walk must follow the part's name (measured 66 of 5,372 rows)."""
    path = _service().trace_field("SALES_MP", "RENAMED_KF")
    hop = next(h for h in path.hops if h.via == "multiprovider_part")
    assert hop.target_field == "RENAMED_KF"
    assert hop.source_fields == ["AMOUNT"], "the part's own field name, from identification"
    assert "RENAMED_KF" not in (hop.source_fields or [])
    # And having renamed, the walk still resolves through the part's rules.
    assert path.resolution == "field"
    assert path.reaches_datasource is True
    assert "renames it to 'AMOUNT'" in (hop.note or "")


def test_multiprovider_field_with_no_identification_row_says_so_specifically() -> None:
    """ "No part supplies this" is a different answer from "no rule found", and only one of them is
    true here. Saying the general thing would hide that BW was actually consulted."""
    path = _service().trace_field("SALES_MP", "NOT_UNIONED")
    assert path.resolution == "none"
    reason = path.unresolved_reason or ""
    assert "MultiProvider" in reason
    assert "RSDICMULTIIOBJ" in reason, "cite the table that was read"
    assert "no row for 'NOT_UNIONED'" in reason


def test_without_identification_table_no_multiprovider_hop_is_claimed() -> None:
    """On a release without the table the union cannot be read, and the walk must not pretend
    otherwise by falling back to the same field name in a guessed part."""
    service = FieldLineageService(
        ScriptedConnection(), _capability(set(_TABLES) - {"multiprovider_identification"})
    )
    path = service.trace_field("SALES_MP", "AMOUNT")
    assert not any(h.via == "multiprovider_part" for h in path.hops)
    assert path.resolution == "none"


def test_multiprovider_identification_is_read_once_per_provider() -> None:
    """A query asks about dozens of fields on one provider; the identification must be loaded whole
    and memoised rather than re-queried per field."""

    class CountingConnection(ScriptedConnection):
        def __init__(self) -> None:
            self.identification_reads = 0

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDICMULTIIOBJ" in sql:
                self.identification_reads += 1
            return super().execute_select(sql, parameters)

    conn = CountingConnection()
    service = FieldLineageService(conn, _capability())
    for field in ("AMOUNT", "RENAMED_KF", "NOT_UNIONED", "AMOUNT"):
        service.trace_field("SALES_MP", field)
    assert conn.identification_reads == 1


# --- D35: a walk that reaches a reference characteristic is one lookup short, not finished -------
# Surfaced by the D22 fix: once field lineage could reach that far, one of the subject query's paths
# walked provider -> part -> navigation attribute and stopped at a reference characteristic. Nothing
# loads into one - measured on production, 0 inbound transformations against 10 for the one it
# references, 2 of which populate the very field being traced.


def test_a_reference_characteristic_continues_at_the_one_it_references() -> None:
    ref = _service().trace_field("PAYER", "COUNTRY")
    hop = next((h for h in ref.hops if h.via == "reference_characteristic"), None)
    assert hop is not None, "the walk must cross to the referenced characteristic"
    assert hop.object_name == "CUSTOMER"
    assert hop.source_fields == ["COUNTRY"], "the field name carries across unchanged"
    # And having crossed, it keeps going: CUSTOMER is loaded by transformations in the fixture, so
    # the hop turns a dead end into a resolved path rather than just relabelling the stop.
    assert ref.resolution == "field"
    assert [h.via for h in ref.hops][:2] == ["provider", "reference_characteristic"]


def test_the_reference_hop_is_declared_not_advisory() -> None:
    """RSDCHA states the reference. The *constructed* route to the same fact is the one that gets it
    wrong - a reference characteristic's master-data table does not carry its own name."""
    path = _service().trace_field("PAYER", "COUNTRY")
    hop = next(h for h in path.hops if h.via == "reference_characteristic")
    assert hop.advisory is False
    assert hop.evidence is not None
    assert hop.evidence.basis == "observed"
    assert hop.evidence.method == "declared_reference_characteristic"
    assert "CHABASNM" in (hop.evidence.detail or "")
    assert "CUSTOMER" in (hop.note or "")


def test_a_self_referencing_characteristic_does_not_hop() -> None:
    """BW stores CHABASNM equal to CHANM for an ordinary characteristic. Following that would make
    every characteristic walk cycle through itself."""
    path = _service().trace_field("CUSTOMER", "NOSUCHFIELD")
    assert not any(h.via == "reference_characteristic" for h in path.hops)
    assert path.resolution == "none"


def test_the_reference_hop_fires_last_so_it_cannot_redirect_a_resolving_walk() -> None:
    """It is the only route that is a statement about the object rather than the field, so it must
    only ever rescue a walk that was about to stop."""
    # AMOUNT on MART_DSO resolves through a transformation rule. MART_DSO is not a characteristic at
    # all, but the ordering property is what matters: a field that resolves never takes this hop.
    path = _service().trace_field("MART_DSO", "AMOUNT")
    assert path.resolution == "field"
    assert not any(h.via == "reference_characteristic" for h in path.hops)


def test_without_rsdcha_the_reference_is_not_claimed() -> None:
    service = FieldLineageService(
        ScriptedConnection(), _capability(set(_TABLES) - {"characteristic"})
    )
    path = service.trace_field("PAYER", "COUNTRY")
    assert not any(h.via == "reference_characteristic" for h in path.hops)


def test_the_reference_is_read_once_per_characteristic() -> None:
    class CountingConnection(ScriptedConnection):
        def __init__(self) -> None:
            self.reads = 0

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDCHA" in sql:
                self.reads += 1
            return super().execute_select(sql, parameters)

    conn = CountingConnection()
    service = FieldLineageService(conn, _capability())
    for _ in range(3):
        service.trace_field("PAYER", "COUNTRY")
    assert conn.reads == 1, "including the negative answer, cached once"
