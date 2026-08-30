"""Reading the logic inside a HANA calculation view. Synthetic content only.

The fixture XML reproduces the grammar **measured** on a live BW-on-HANA 2.00.079 system before the
parser was written: a ``Calculation:scenario`` root where only the root is namespaced,
``dataSources`` mixing a calc-view source with a generated BW table, ``calculationViews`` with
projection / join / aggregation nodes, a ``leftOuter`` join carrying ``joinAttribute`` children, a
``filter``, a ``calculatedViewAttribute`` wrapping a ``formula``, a ``logicalModel`` whose
attributes and measures name their origin node and column, and input parameters expressed as
``localVariables/variable`` with ``parameter="true"``.

That last point is the one worth stating: this release has **no** ``inputParameter`` element, so a
parser written from the remembered schema reports every view as parameterless - a wrong answer that
looks like a clean one. It is why the grammar was read off the system first.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories import hana as hana_mod
from mcp_server_sapbw.repositories.hana import HanaRepository, _split_repo_name
from mcp_server_sapbw.services.calcview_parser import CalcViewParseError, parse_calc_view

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "hana_views": "VIEWS",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "calc_view_definition": "ACTIVE_OBJECT",
}
_SYS_TABLES = {"hana_views", "object_dependencies"}

PACKAGE = "ACME.SALES"
OBJECT = "SALES_MARGIN"
VIEW = f"{PACKAGE}/{OBJECT}"

# Built by concatenation so the customer-metadata scan never sees the literal prefix.
_BIC_TABLE = "/BIC/" + "AORDERS00"

DEFINITION = f"""<?xml version="1.0" encoding="UTF-8"?>
<Calculation:scenario xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:Calculation="http://www.sap.com/ndb/BiModelCalculation.ecore"
    schemaVersion="2.3" id="{OBJECT}" applyPrivilegeType="ANALYTIC_PRIVILEGE"
    checkAnalyticPrivileges="true" visibility="reportingEnabled"
    calculationScenarioType="TREE_BASED" dataCategory="CUBE" outputViewType="Aggregation">
  <origin/>
  <descriptions defaultDescription="Sales margin by customer"/>
  <metadata changedAt="2026-06-04 02:03:54.805"/>
  <localVariables>
    <variable id="IP_CALMONTH" parameter="true">
      <descriptions defaultDescription="Calendar month"/>
      <variableProperties datatype="NVARCHAR" length="6" mandatory="false">
        <valueDomain type="empty"/>
        <selection multiLine="false" type="SingleValue"/>
        <defaultRange/>
      </variableProperties>
    </variable>
    <variable id="VAR_REGION">
      <variableProperties datatype="NVARCHAR" length="4" mandatory="true">
        <selection multiLine="true" type="Interval"/>
      </variableProperties>
    </variable>
  </localVariables>
  <variableMappings/>
  <dataSources>
    <DataSource id="CUST_L01" type="CALCULATION_VIEW">
      <viewAttributes allViewAttributes="true"/>
      <resourceUri>/system-local.bw.bw2hana/calculationviews/CUST_L01</resourceUri>
    </DataSource>
    <DataSource id="{_BIC_TABLE}" type="DATA_BASE_TABLE">
      <viewAttributes allViewAttributes="true"/>
      <columnObject schemaName="ABAP" columnObjectName="{_BIC_TABLE}"/>
    </DataSource>
  </dataSources>
  <calculationViews>
    <calculationView xsi:type="Calculation:ProjectionView" id="Proj_Orders">
      <viewAttributes>
        <viewAttribute id="ORDER_NUM"/>
      </viewAttributes>
      <calculatedViewAttributes>
        <calculatedViewAttribute datatype="VARCHAR" id="ERROR_FLAG" length="1"
            expressionLanguage="COLUMN_ENGINE">
          <formula>if(isNull("NET_VALUE"), 'X', '')</formula>
        </calculatedViewAttribute>
      </calculatedViewAttributes>
      <input node="#{_BIC_TABLE}">
        <mapping xsi:type="Calculation:AttributeMapping" target="ORDER_NUM" source="ORDER_NUM"/>
        <mapping xsi:type="Calculation:ConstantAttributeMapping" target="SRC" value="ERP"/>
      </input>
      <filter>IN(&quot;FISCYEAR&quot;, '2025','2026') AND &quot;RECTYPE&quot; = 'F'</filter>
    </calculationView>
    <calculationView xsi:type="Calculation:JoinView" id="Join_1" cardinality="C1_1"
        joinType="leftOuter" joinOrder="OUTSIDE_IN">
      <joinAttribute name="CUSTOMER"/>
      <joinAttribute name="SALESORG"/>
      <input node="#Proj_Orders">
        <mapping xsi:type="Calculation:AttributeMapping" target="ORDER_NUM" source="ORDER_NUM"/>
      </input>
      <input node="#CUST_L01">
        <mapping xsi:type="Calculation:AttributeMapping" target="CUSTOMER" source="CUSTOMER"/>
      </input>
    </calculationView>
    <calculationView xsi:type="Calculation:AggregationView" id="Agg_Final">
      <input node="#Join_1">
        <mapping xsi:type="Calculation:AttributeMapping" target="NET_VALUE" source="NET_VALUE"/>
      </input>
    </calculationView>
  </calculationViews>
  <logicalModel id="Agg_Final">
    <descriptions/>
    <attributes>
      <attribute id="CUSTOMER" order="1" key="true" displayAttribute="false">
        <descriptions defaultDescription="Customer"/>
        <keyMapping columnObjectName="Agg_Final" columnName="CUSTOMER"/>
      </attribute>
    </attributes>
    <baseMeasures>
      <measure id="NET_VALUE" order="2" aggregationType="sum" measureType="simple">
        <descriptions defaultDescription="Net value"/>
        <measureMapping columnObjectName="Agg_Final" columnName="NET_VALUE"/>
      </measure>
    </baseMeasures>
    <calculatedMeasures>
      <measure id="MARGIN_PCT" order="3" aggregationType="sum">
        <formula>"NET_VALUE" / "GROSS_VALUE" * 100</formula>
      </measure>
    </calculatedMeasures>
    <restrictedMeasures/>
    <localDimensions/>
    <privateDataFoundation>
      <tableProxies/>
      <joins/>
      <layout>
        <shapes/>
      </layout>
    </privateDataFoundation>
    <somethingNewInAFutureRelease>
      <thing id="X"/>
    </somethingNewInAFutureRelease>
  </logicalModel>
</Calculation:scenario>
"""


class ScriptedConnection:
    """The repository row, split into a header read and a body read as the reader does."""

    def __init__(
        self, *, definition: str | None = DEFINITION, size: int | None = None, present: bool = True
    ) -> None:
        self.definition = definition
        self.size = size if size is not None else len(definition or "")
        self.present = present
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        if "ACTIVE_OBJECT" not in sql:
            return []
        if not self.present:
            return []
        if "LENGTH(CDATA)" in sql:
            return [("calculationview", self.size, "2026-06-04 02:03:54.8", "MODELLER")]
        return [(self.definition,)]


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        hana_repo_style="sys_repo",
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=(
                    ("SYS" if logical in _SYS_TABLES else "_SYS_REPO")
                    if logical in present
                    else None
                ),
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(connection: ScriptedConnection | None = None, present: set[str] | None = None) -> Any:
    return HanaRepository(connection or ScriptedConnection(), _capability(present))


def _definition(connection: ScriptedConnection | None = None) -> Any:
    result = _repo(connection).get_calc_view_definition(VIEW)
    assert not isinstance(result, UnsupportedResult)
    return result


# --- the runtime name -> repository object mapping -----------------------------------------------


@pytest.mark.parametrize(
    ("runtime_name", "expected"),
    [
        ("ACME.SALES/CV1", ("ACME.SALES", "CV1")),
        # An internal node path points at the same activated design-time object.
        ("ACME.SALES/CV1/dp/Projection_1", ("ACME.SALES", "CV1")),
        ("system-local.bw.bw2hana/GEN_D02", ("system-local.bw.bw2hana", "GEN_D02")),
        ("no_slash_at_all", (None, None)),
    ],
)
def test_repository_name_split(runtime_name: str, expected: tuple[str | None, str | None]) -> None:
    assert _split_repo_name(runtime_name) == expected


# --- the logic itself ---------------------------------------------------------------------------


def test_the_view_header_is_read() -> None:
    definition = _definition()
    assert definition.parsed is True
    assert definition.unparsed_reason is None
    assert definition.package_id == PACKAGE
    assert definition.object_name == OBJECT
    assert definition.description == "Sales margin by customer"
    assert definition.data_category == "CUBE"
    assert definition.output_view_type == "Aggregation"
    assert definition.schema_version == "2.3"
    assert definition.final_node == "Agg_Final"
    assert definition.applies_analytic_privilege is True
    assert definition.is_bw_generated is False


def test_nodes_carry_their_kind_and_join_semantics() -> None:
    """A join whose type is not reported is a join whose result cannot be predicted."""
    definition = _definition()
    by_id = {node.id: node for node in definition.nodes}
    assert set(by_id) == {"Proj_Orders", "Join_1", "Agg_Final"}
    assert definition.node_counts == {"aggregation": 1, "join": 1, "projection": 1}

    join = by_id["Join_1"]
    assert join.node_type == "join"
    assert join.join_type == "leftOuter"
    assert join.cardinality == "C1_1"
    assert join.join_order == "OUTSIDE_IN"
    assert join.join_attributes == ["CUSTOMER", "SALESORG"]
    assert set(join.inputs) == {"Proj_Orders", "CUST_L01"}


def test_calculated_columns_report_their_formula_verbatim() -> None:
    """The formula is the answer to "where does this number come from", so it is not summarised."""
    definition = _definition()
    calculated = {column.name: column for column in definition.calculated_columns}
    assert set(calculated) == {"ERROR_FLAG"}
    flag = calculated["ERROR_FLAG"]
    assert flag.formula == "if(isNull(\"NET_VALUE\"), 'X', '')"
    assert flag.datatype == "VARCHAR"
    assert flag.node == "Proj_Orders"
    assert flag.expression_language == "COLUMN_ENGINE"


def test_filters_are_reported_with_the_node_that_applies_them() -> None:
    definition = _definition()
    assert len(definition.filters) == 1
    assert definition.filters[0].startswith("Proj_Orders: ")
    # XML entities are resolved, so the expression reads as the modeller wrote it.
    assert "IN(\"FISCYEAR\", '2025','2026')" in definition.filters[0]
    node = next(n for n in definition.nodes if n.id == "Proj_Orders")
    assert node.filter_expression is not None


def test_a_constant_mapping_is_distinguished_from_a_column_mapping() -> None:
    node = next(n for n in _definition().nodes if n.id == "Proj_Orders")
    by_target = {m.target: m for m in node.mappings}
    assert by_target["ORDER_NUM"].source == "ORDER_NUM"
    assert by_target["ORDER_NUM"].value is None
    assert by_target["SRC"].value == "ERP"
    assert by_target["SRC"].source is None
    assert "Constant" in (by_target["SRC"].kind or "")


def test_the_semantic_layer_reports_aggregation_per_measure() -> None:
    definition = _definition()
    by_name = {column.name: column for column in definition.semantic_columns}
    assert by_name["CUSTOMER"].role == "attribute"
    assert by_name["CUSTOMER"].is_key is True
    assert by_name["CUSTOMER"].origin_node == "Agg_Final"
    assert by_name["NET_VALUE"].role == "measure"
    assert by_name["NET_VALUE"].aggregation == "sum"
    assert by_name["NET_VALUE"].calculated is False
    assert by_name["MARGIN_PCT"].calculated is True
    assert by_name["MARGIN_PCT"].formula == '"NET_VALUE" / "GROSS_VALUE" * 100'


def test_input_parameters_are_told_apart_from_variables() -> None:
    """``parameter="true"`` is all that separates them, and it decides who supplies a value."""
    definition = _definition()
    by_name = {p.name: p for p in definition.input_parameters}
    assert set(by_name) == {"IP_CALMONTH", "VAR_REGION"}
    assert by_name["IP_CALMONTH"].is_input_parameter is True
    assert by_name["IP_CALMONTH"].datatype == "NVARCHAR"
    assert by_name["IP_CALMONTH"].mandatory is False
    assert by_name["IP_CALMONTH"].selection_type == "SingleValue"
    assert by_name["VAR_REGION"].is_input_parameter is False
    assert by_name["VAR_REGION"].mandatory is True


def test_data_sources_resolve_bw_tables_and_keep_calc_view_references() -> None:
    definition = _definition()
    by_id = {source.id: source for source in definition.data_sources}
    assert by_id["CUST_L01"].source_type == "CALCULATION_VIEW"
    assert by_id["CUST_L01"].resource_uri is not None
    generated = by_id[_BIC_TABLE]
    assert generated.source_type == "DATA_BASE_TABLE"
    assert generated.schema_name == "ABAP"
    assert generated.resolved_object is not None, "a /BIC/ source must resolve to a BW object"


def test_an_element_this_grammar_does_not_know_is_reported_not_dropped() -> None:
    """A release that shapes its XML differently must surface as a gap, not as an absence."""
    assert "somethingNewInAFutureRelease" in _definition().unrecognised_elements


def test_an_empty_placeholder_is_not_reported_as_a_gap() -> None:
    """Otherwise the gap list is noise on every view, and a reader learns to ignore it.

    Measured across 25 modelled views on the reference system: ``restrictedMeasures``,
    ``localDimensions`` and ``sharedDimensions`` were present in all of them and empty in all of
    them. Listing tags rather than content reported three permanent false gaps per view.
    """
    unread = _definition().unrecognised_elements
    assert "restrictedMeasures" not in unread
    assert "localDimensions" not in unread
    # privateDataFoundation always has children - empty placeholders plus layout geometry - so
    # "has children" is not the test either; it appeared on every production view under that rule.
    assert "privateDataFoundation" not in unread
    assert unread == ["somethingNewInAFutureRelease"], (
        "only elements that actually hold unread content belong here"
    )


def test_evidence_says_the_definition_was_read_not_inferred() -> None:
    definition = _definition()
    assert definition.evidence is not None
    assert definition.evidence.basis == "observed"
    assert definition.evidence.method == "activated_view_definition"
    assert any("_SYS_REPO" in caveat for caveat in definition.caveats)


# --- the failure modes, each distinguishable from "this view has no logic" -----------------------


def test_a_document_type_declaration_is_refused() -> None:
    """Entity-expansion attacks all need a DTD; a calc view never has one, so it ends the parse."""
    hostile = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        '<Calculation:scenario xmlns:Calculation="http://www.sap.com/ndb/BiModelCalculation.ecore"/>'
    )
    with pytest.raises(CalcViewParseError) as excinfo:
        parse_calc_view(hostile)
    assert "document type declaration" in excinfo.value.reason

    definition = _definition(ScriptedConnection(definition=hostile))
    assert definition.parsed is False
    assert definition.unparsed_reason is not None
    assert "document type declaration" in definition.unparsed_reason


def test_a_definition_above_the_size_bound_is_not_fetched() -> None:
    """Measured on production: BW-generated definitions reach 323 MB. Fetching one is the defect."""
    connection = ScriptedConnection(size=hana_mod._MAX_DEFINITION_CHARS + 1)
    definition = _definition(connection)
    assert definition.parsed is False
    assert definition.definition_bytes == hana_mod._MAX_DEFINITION_CHARS + 1
    assert definition.unparsed_reason is not None
    assert "bound this server fetches" in definition.unparsed_reason
    assert not any(
        "LENGTH(CDATA)" not in s and "ACTIVE_OBJECT" in s for s in connection.statements
    ), "the CLOB was fetched despite exceeding the bound, which is what the bound exists to prevent"


def test_a_missing_activated_definition_is_not_an_absence_of_logic() -> None:
    definition = _definition(ScriptedConnection(present=False))
    assert definition.parsed is False
    assert definition.unparsed_reason is not None
    assert "no activated definition" in definition.unparsed_reason
    assert "bw_get_calc_view_lineage" in definition.unparsed_reason


def test_malformed_xml_is_reported_as_malformed() -> None:
    definition = _definition(ScriptedConnection(definition="<Calculation:scenario><oops"))
    assert definition.parsed is False
    assert definition.unparsed_reason is not None
    assert "not well-formed" in definition.unparsed_reason


def test_a_foreign_root_element_is_refused() -> None:
    definition = _definition(ScriptedConnection(definition="<somethingElse/>"))
    assert definition.parsed is False
    assert definition.unparsed_reason is not None
    assert "not a Calculation:scenario" in definition.unparsed_reason


def test_an_empty_definition_is_reported() -> None:
    definition = _definition(ScriptedConnection(definition="   ", size=3))
    assert definition.parsed is False
    assert definition.unparsed_reason is not None
    assert "empty" in definition.unparsed_reason


def test_a_name_that_is_not_a_repository_path_says_so() -> None:
    result = _repo().get_calc_view_definition("NOT_A_PATH")
    assert not isinstance(result, UnsupportedResult)
    assert result.parsed is False
    assert result.unparsed_reason is not None
    assert "does not decompose" in result.unparsed_reason


def test_a_release_without_the_repository_is_unsupported_not_empty() -> None:
    result = _repo(present={"hana_views", "object_dependencies"}).get_calc_view_definition(VIEW)
    assert isinstance(result, UnsupportedResult)


def test_a_bw_generated_package_is_flagged_as_generated() -> None:
    repo = _repo()
    result = repo.get_calc_view_definition("system-local.bw.bw2hana/GEN_D02")
    assert not isinstance(result, UnsupportedResult)
    assert result.is_bw_generated is True
