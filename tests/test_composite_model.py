"""The declared CompositeProvider model: parsing it, and what depends on getting it right.

A CompositeProvider keeps its composition and its whole field mapping in one XML column on
``RSOHCPR`` and nowhere else. Two things went wrong for want of reading it, and both are asserted
here rather than described:

* Part providers were resolved from the base tables of the generated calc view - a naming-convention
  reading - because only ``XML_DEF`` was ever probed and it is empty on the release measured, while
  ``XML_UI`` holds the model. So a *declared* fact was reported as an inference.
* Field-level lineage had no CompositeProvider hop, so every field of every CompositeProvider-based
  query stopped at hop zero. Since a BEx query normally reads a CompositeProvider on BW-on-HANA,
  that was most of the subject matter.

All names here are synthetic.
"""

from __future__ import annotations

import pytest

from mcp_server_sapbw.models.completeness import bounded
from mcp_server_sapbw.models.composite import (
    CompositeFieldMapping,
    CompositeInput,
    CompositeModel,
    CompositeViewNode,
)
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.composite_parser import (
    MAX_INPUTS,
    CompositeParseError,
    alias_kind_code,
    decode_entity_ref,
    decode_node_ref,
    parse_composite_model,
    runtime_view_name,
)

_NS = "$" + "IMO" + "$"  # the model's encoding of a namespaced name; assembled, never a real one

UNION_MODEL = """<?xml version="1.0" encoding="utf-8"?>
<Composite:compositeView xmlns:Composite="urn:composite" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    schemaVersion="1.12" name="SALES_CP" withHanaModel="true" defaultNode="#///U1">
  <viewNode xsi:type="View:Union" name="U1">
    <element xsi:type="BwCore:BwElement" name="0BILL_NUM" infoObjectName="0BILL_NUM"/>
    <element xsi:type="BwCore:BwElement" name="AMOUNT" infoObjectName="AMOUNT"/>
    <element xsi:type="BwCore:BwElement" name="REGION" infoObjectName="REGION"/>
    <input xsi:type="Composite:CompositeInput" name="" alias="U1.ADSO.1" selectAll="false">
      <entity>ORDERS_ADSO.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="0BILL_NUM" sourceName="0BILL_NUM"/>
      <mapping xsi:type="Type:ElementMapping" targetName="AMOUNT" sourceName="NET_VALUE"/>
      <mapping xsi:type="Type:ConstantElementMapping" targetName="REGION" sourceName="IGNORED"/>
    </input>
    <input xsi:type="Composite:CompositeInput" name="" alias="U1.ADSO.2" selectAll="true">
      <entity>BILLING_ADSO.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="AMOUNT" sourceName="BILL_VALUE"/>
    </input>
    <input xsi:type="Composite:CompositeInput" name="" alias="U1.CALC.3" selectAll="false">
      <entity>PKG/SUB/CV_MASTER.calculationview#/</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="0BILL_NUM" sourceName="DOC_NO"/>
    </input>
    <input xsi:type="Composite:CompositeInput" name="" alias="U1.FBPA.4" selectAll="false">
      <entity>NS_PLACEHOLDER.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="REGION" sourceName="AREA"/>
    </input>
  </viewNode>
</Composite:compositeView>
""".replace("NS_PLACEHOLDER", _NS + "ODD_PART")

# A join over a union: the join's second input reads the union node rather than an object.
STACKED_MODEL = """<?xml version="1.0" encoding="utf-8"?>
<Composite:compositeView xmlns:Composite="urn:composite" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    schemaVersion="1.12" name="STACK_CP" withHanaModel="true" defaultNode="#///J1">
  <viewNode xsi:type="View:Union" name="U1">
    <element xsi:type="BwCore:BwElement" name="INNER_KEY" infoObjectName="INNER_KEY"/>
    <input xsi:type="Composite:CompositeInput" alias="U1.ADSO.1" selectAll="false">
      <entity>DEEP_ADSO.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="INNER_KEY" sourceName="DEEP_KEY"/>
    </input>
  </viewNode>
  <viewNode xsi:type="View:JoinNode" name="J1">
    <element xsi:type="BwCore:BwElement" name="OUTER_KEY" infoObjectName="OUTER_KEY"/>
    <input xsi:type="Composite:CompositeInput" alias="J1.IOBJ.1" selectAll="false">
      <entity>MASTER_IOBJ.composite#//</entity>
      <mapping xsi:type="Type:ElementMapping" targetName="OUTER_KEY" sourceName="MASTER_KEY"/>
    </input>
    <input xsi:type="Composite:CompositeInput" alias="U1" selectAll="false">
      <viewNode xsi:type="View:Union">#///U1</viewNode>
      <mapping xsi:type="Type:ElementMapping" targetName="OUTER_KEY" sourceName="INNER_KEY"/>
    </input>
  </viewNode>
</Composite:compositeView>
"""


def _model(definition: str, provider: str) -> CompositeModel:
    """Map a parsed definition onto the model the way the repository does, without a database."""
    parsed = parse_composite_model(definition)
    return CompositeModel(
        provider=provider,
        parsed=True,
        source_column="XML_UI",
        node_type=parsed.node_type,
        node_name=parsed.node_name,
        nodes=[CompositeViewNode(**n) for n in parsed.nodes],
        schema_version=parsed.root.get("schema_version"),
        with_hana_model=bool(parsed.root.get("with_hana_model")),
        element_count=parsed.element_count,
        input_count=len(parsed.inputs),
        inputs=[
            CompositeInput(
                alias=entry["alias"],
                node_name=entry["node_name"],
                part_name=entry["part_name"],
                part_kind=entry["part_kind"],
                alias_kind=entry["alias_kind"],
                entity_ref=entry["entity_ref"],
                internal_node=entry["internal_node"],
                runtime_view_name=entry["runtime_view_name"],
                select_all=entry["select_all"],
                mappings=[CompositeFieldMapping(**m) for m in entry["mappings"]],
                mapping_count=entry["mapping_count"],
            )
            for entry in parsed.inputs
        ],
        provenance=Provenance(source_table="RSOHCPR", source_key={"HCPRNM": provider}),
    )


# --- the reference decodings, each verified live before being encoded here ------------------


def test_an_entity_reference_splits_into_object_and_document_kind() -> None:
    assert decode_entity_ref("ORDERS_ADSO.composite#//") == ("ORDERS_ADSO", "composite")
    assert decode_entity_ref("PKG/SUB/CV_X.calculationview#/") == (
        "PKG/SUB/CV_X",
        "calculationview",
    )


def test_a_namespaced_name_is_decoded_to_the_form_the_catalogues_hold() -> None:
    """``$NS$NAME`` is how the model writes ``/NS/NAME``; joined raw, it matches nothing."""
    decoded = decode_entity_ref(_NS + "D_STOCK.composite#//")
    assert decoded == ("/IMO/D_STOCK", "composite")


def test_an_unparseable_entity_reference_is_none_rather_than_a_guess() -> None:
    assert decode_entity_ref("no-kind-marker") is None
    assert decode_entity_ref("") is None
    assert decode_entity_ref(None) is None


def test_the_alias_carries_bws_own_kind_code() -> None:
    assert alias_kind_code("U1.ADSO.1") == "ADSO"
    assert alias_kind_code("J1.CALC.12") == "CALC"
    # An alias that is a bare node name carries no kind - it is an internal reference.
    assert alias_kind_code("U1") is None


def test_an_internal_node_reference_decodes_to_the_node_name() -> None:
    assert decode_node_ref("#///U1") == "U1"
    assert decode_node_ref("#///J1") == "J1"
    assert decode_node_ref(None) is None


def test_a_calc_view_part_gets_the_runtime_name_the_catalogue_uses() -> None:
    """The package separator differs between the model and ``_SYS_BIC``; verified both ways live."""
    assert runtime_view_name("PKG/SUB/LEAF/CV_NAME") == "PKG.SUB.LEAF/CV_NAME"
    assert runtime_view_name("NO_PACKAGE") is None


# --- the union model -----------------------------------------------------------------------


def test_every_input_resolves_to_a_part_with_its_declared_kind() -> None:
    model = _model(UNION_MODEL, "SALES_CP")
    assert model.node_type == "View:Union"
    assert model.node_name == "U1"
    assert model.element_count == 3
    by_alias = {i.alias: i for i in model.inputs}
    assert by_alias["U1.ADSO.1"].part_name == "ORDERS_ADSO"
    assert by_alias["U1.ADSO.1"].part_kind == "adso"
    assert by_alias["U1.CALC.3"].part_kind == "calcview"
    assert by_alias["U1.ADSO.2"].select_all is True


def test_a_calc_view_input_carries_the_runtime_name_as_well_as_the_model_name() -> None:
    model = _model(UNION_MODEL, "SALES_CP")
    calc = next(i for i in model.inputs if i.part_kind == "calcview")
    assert calc.part_name == "PKG/SUB/CV_MASTER"
    assert calc.runtime_view_name == "PKG.SUB/CV_MASTER"


def test_an_undecoded_kind_code_stays_unknown_and_keeps_the_code() -> None:
    """A part type guessed wrong sends a change review to the wrong object; ``unknown`` does not."""
    model = _model(UNION_MODEL, "SALES_CP")
    odd = next(i for i in model.inputs if i.alias_kind == "FBPA")
    assert odd.part_kind == "unknown"
    assert odd.alias_kind == "FBPA", "BW's own code must survive so a reader sees what BW said"
    assert odd.part_name == "/IMO/ODD_PART", "the name still decodes even when the kind does not"


def test_a_constant_mapping_carries_no_source_field() -> None:
    """Inventing one would put a field name on a value hard-coded in the model."""
    model = _model(UNION_MODEL, "SALES_CP")
    region = next(
        m
        for i in model.inputs
        for m in i.mappings
        if m.target_field == "REGION" and i.alias.endswith(".1")
    )
    assert region.mapping_kind == "constant"
    assert region.source_field is None


# --- the fan-out, which is the normal case for a union -------------------------------------


def test_a_union_element_resolves_to_every_part_that_supplies_it() -> None:
    model = _model(UNION_MODEL, "SALES_CP")
    origins = model.resolve_field("AMOUNT")
    assert {(o.part_name, o.source_field) for o in origins} == {
        ("ORDERS_ADSO", "NET_VALUE"),
        ("BILLING_ADSO", "BILL_VALUE"),
    }


def test_the_field_name_is_matched_case_insensitively() -> None:
    """BW field names are upper-case; a caller's are not always, and the answer must not change."""
    model = _model(UNION_MODEL, "SALES_CP")
    assert model.resolve_field("amount") == model.resolve_field("AMOUNT")
    # The declared spelling is what comes back, not the argument's.
    assert {o.target_field for o in model.resolve_field("amount")} == {"AMOUNT"}


def test_an_element_no_part_supplies_resolves_to_nothing() -> None:
    model = _model(UNION_MODEL, "SALES_CP")
    assert model.resolve_field("NOT_MAPPED") == []


# --- the stacked case, which a flat input list cannot represent ----------------------------


def test_an_input_reading_another_node_is_not_reported_as_a_part() -> None:
    """40 of 226 inputs on the measured system are these; as parts they are 40 absent objects."""
    model = _model(STACKED_MODEL, "STACK_CP")
    internal = next(i for i in model.inputs if i.is_internal)
    assert internal.internal_node == "U1"
    assert internal.part_name == ""
    assert internal.alias not in {i.alias for i in model.part_inputs}


def test_resolve_field_crosses_the_internal_node_to_the_object_beneath() -> None:
    """Stopping at the node reference loses every part below it."""
    model = _model(STACKED_MODEL, "STACK_CP")
    origins = model.resolve_field("OUTER_KEY")
    by_part = {o.part_name: o for o in origins}
    assert set(by_part) == {"MASTER_IOBJ", "DEEP_ADSO"}
    # The direct part is one hop; the one under the union is two, and the route says so.
    assert by_part["MASTER_IOBJ"].via_aliases == ["J1.IOBJ.1"]
    assert by_part["DEEP_ADSO"].via_aliases == ["U1", "U1.ADSO.1"]
    # The field is renamed at each hop, and the *part's* name is what is reported.
    assert by_part["DEEP_ADSO"].source_field == "DEEP_KEY"


def test_the_default_node_is_what_a_consumer_reads_not_the_first_declared() -> None:
    """Document order puts the union first; ``defaultNode`` says the join is the output."""
    model = _model(STACKED_MODEL, "STACK_CP")
    assert model.node_name == "J1"
    assert model.node_type == "View:JoinNode"
    assert {n.name for n in model.nodes} == {"U1", "J1"}


def test_inputs_are_attributed_to_their_own_node() -> None:
    model = _model(STACKED_MODEL, "STACK_CP")
    assert {i.alias for i in model.inputs_of("U1")} == {"U1.ADSO.1"}
    assert {i.alias for i in model.inputs_of("J1")} == {"J1.IOBJ.1", "U1"}


def test_a_cyclic_node_reference_terminates() -> None:
    """A model referencing its own node cannot make the resolution run away."""
    cyclic = STACKED_MODEL.replace(
        '<viewNode xsi:type="View:Union">#///U1</viewNode>',
        '<viewNode xsi:type="View:Union">#///J1</viewNode>',
    )
    model = _model(cyclic, "STACK_CP")
    origins = model.resolve_field("OUTER_KEY")
    assert {o.part_name for o in origins} == {"MASTER_IOBJ"}


# --- refusals and bounds -------------------------------------------------------------------


def test_a_document_type_declaration_ends_the_parse() -> None:
    """The entity-expansion precondition. A CompositeProvider model never carries one."""
    with pytest.raises(CompositeParseError) as caught:
        parse_composite_model('<!DOCTYPE x><Composite:compositeView xmlns:Composite="u"/>')
    assert "document type declaration" in caught.value.reason


def test_a_definition_that_is_not_a_composite_view_is_refused_by_name() -> None:
    with pytest.raises(CompositeParseError) as caught:
        parse_composite_model('<Calculation:scenario xmlns:Calculation="u"/>')
    assert "not a compositeView" in caught.value.reason


def test_an_empty_definition_is_refused_rather_than_read_as_no_parts() -> None:
    with pytest.raises(CompositeParseError) as caught:
        parse_composite_model("   ")
    assert "empty" in caught.value.reason


def test_malformed_xml_is_refused_with_the_reason() -> None:
    with pytest.raises(CompositeParseError) as caught:
        parse_composite_model('<Composite:compositeView xmlns:Composite="u"><unclosed>')
    assert "well-formed" in caught.value.reason


def test_the_input_bound_is_named_not_just_flagged() -> None:
    inputs = "".join(
        f'<input alias="U1.ADSO.{n}"><entity>P{n}.composite#//</entity></input>'
        for n in range(MAX_INPUTS + 5)
    )
    definition = (
        '<Composite:compositeView xmlns:Composite="u" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" name="WIDE_CP">'
        f'<viewNode xsi:type="View:Union" name="U1">{inputs}</viewNode>'
        "</Composite:compositeView>"
    )
    parsed = parse_composite_model(definition)
    assert len(parsed.inputs) == MAX_INPUTS
    assert ("inputs", MAX_INPUTS) in parsed.bounds


def test_an_unbounded_model_names_no_bound() -> None:
    assert parse_composite_model(UNION_MODEL).bounds == []


def test_a_bound_model_reads_as_truncated_with_a_reason() -> None:
    """The D6 invariant on this reader: the flag and the named bound move together."""
    provenance = Provenance(source_table="RSOHCPR", source_key={"HCPRNM": "WIDE_CP"})
    unbounded = CompositeModel(provider="WIDE_CP", provenance=provenance)
    assert unbounded.truncated is False
    assert unbounded.completeness.is_complete is True

    named = CompositeModel(
        provider="WIDE_CP",
        provenance=provenance,
        completeness=bounded("row_cap", scope="inputs", limit=MAX_INPUTS),
    )
    assert named.truncated is True, "naming a bound must set the published flag"
    assert named.completeness.status == "row_cap"
    assert named.completeness.bounds[0].scope == "inputs"

    # The legacy path: a producer setting only the flag must not read as complete.
    flag_only = CompositeModel(provider="WIDE_CP", provenance=provenance, truncated=True)
    assert flag_only.completeness.is_complete is False
    assert flag_only.completeness.status == "unspecified"
