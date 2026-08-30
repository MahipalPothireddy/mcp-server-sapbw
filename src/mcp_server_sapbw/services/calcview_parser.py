"""Parse a HANA calculation-view definition into the logic it expresses.

**What this closes.** The server could say which tables a calc view reads and which InfoProviders
consume it, but not what it *does* with them. Joins, join types, filters, calculated columns and
their formulas, input parameters and per-measure aggregation were a black box: a calculation
happening in the view rather than in a BW transformation was invisible, which is exactly the case
mission scenario 9.3 is about - a calc view change silently changes DSO content on the next load,
with no BW where-used warning.

**The grammar here was measured, not recalled.** Every element and attribute below was read off a
live BW-on-HANA 2.00.079 system (589 activated calculation views) before this parser was written,
because guessing an XML schema is the same mistake as guessing a table name. What that measurement
established:

* the root is ``Calculation:scenario`` in the ``BiModelCalculation.ecore`` namespace, and it is the
  *only* namespaced element - every child sits in no namespace, so tags compare as plain strings;
* node types present are ``Calculation:{Projection,Join,Aggregation,Union}View``;
* join types present are ``inner``, ``leftOuter``, ``rightOuter``, ``fullOuter``, ``referential``;
* a calculated column is ``calculatedViewAttribute`` carrying a child ``formula``;
* the semantic layer is ``logicalModel`` with ``attribute``/``measure`` children whose
  ``keyMapping``/``measureMapping`` names the node and column they come from;
* **input parameters are ``localVariables/variable`` with ``parameter="true"``**. There is no
  ``inputParameter`` element on this release, so a parser looking for one would report every view as
  having no parameters - a wrong answer that looks like a clean one.

Anything this grammar does not cover is reported through ``unrecognised_elements`` rather than
dropped, so a release or a modelling feature that shapes its XML differently shows up as a gap
instead of as an absence.

**Security.** The definition is a large CLOB from a database, which is untrusted input by the same
rule as everything else the server reads. Entity-expansion attacks (billion laughs, quadratic
blowup) and external-entity reads all require a document type declaration, so one is refused
outright before any parsing begins - a real calc view never carries one. That closes the class
without adding a dependency; the tree is then built by the standard library, whose builder is
iterative, and this module's own traversal is iterative too, so neither a deep document nor a wide
one can exhaust the stack.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

# Bounds. A calc view is a modelling artefact, not a data set, so these are far above anything
# measured (the widest modelled view on the reference system has 22 nodes and 255 mappings) and
# exist so a pathological or machine-generated definition cannot produce an unbounded reply.
MAX_NODES = 400
MAX_COLUMNS_PER_NODE = 500
MAX_CALCULATED_COLUMNS = 500
MAX_SEMANTIC_COLUMNS = 2000
MAX_DATA_SOURCES = 400
MAX_PARAMETERS = 200
#: Longest formula or filter expression kept verbatim. Expressions are the point of this reader, so
#: this is generous; anything longer is truncated with a marker rather than silently shortened.
MAX_EXPRESSION_CHARS = 4000

_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

#: ``xsi:type`` on a ``calculationView`` -> the node kind, and the raw value is kept alongside.
_NODE_TYPES: dict[str, str] = {
    "Calculation:ProjectionView": "projection",
    "Calculation:JoinView": "join",
    "Calculation:AggregationView": "aggregation",
    "Calculation:UnionView": "union",
    "Calculation:RankView": "rank",
}

#: A document type declaration in a calc view definition is never legitimate and is the precondition
#: for every entity-expansion and external-entity attack, so its presence ends the parse.
_REFUSE_DTD = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class ParsedCalcView:
    """The logic read out of one definition. A plain carrier; the repository maps it to models."""

    __slots__ = (
        "attributes",
        "calculated_columns",
        "data_sources",
        "filters",
        "nodes",
        "parameters",
        "root",
        "truncated",
        "unrecognised_elements",
    )

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self.data_sources: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.calculated_columns: list[dict[str, Any]] = []
        self.attributes: list[dict[str, Any]] = []
        self.filters: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.unrecognised_elements: list[str] = []
        self.truncated: bool = False


class CalcViewParseError(Exception):
    """The definition could not be read. Carries a reason fit to return to a caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _expression(value: str | None) -> str | None:
    """A formula or filter, whitespace-normalised and bounded with a visible marker."""
    cleaned = _text(value)
    if cleaned is None:
        return None
    if len(cleaned) > MAX_EXPRESSION_CHARS:
        return cleaned[:MAX_EXPRESSION_CHARS] + " ... [truncated]"
    return cleaned


def _child(element: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    for candidate in element:
        if candidate.tag == tag:
            return candidate
    return None


def _children(element: ElementTree.Element, tag: str) -> list[ElementTree.Element]:
    return [candidate for candidate in element if candidate.tag == tag]


def parse_calc_view(definition: str) -> ParsedCalcView:
    """Read a ``Calculation:scenario`` definition. Raises :class:`CalcViewParseError` on refusal."""
    if not definition or not definition.strip():
        raise CalcViewParseError("the stored definition is empty")
    if _REFUSE_DTD.search(definition):
        raise CalcViewParseError(
            "the definition carries a document type declaration, which a calculation view never "
            "does and which is the precondition for entity-expansion attacks; it was not parsed"
        )
    try:
        root = ElementTree.fromstring(definition)
    except ElementTree.ParseError as exc:
        raise CalcViewParseError(f"the definition is not well-formed XML: {exc}") from exc

    if not root.tag.endswith("scenario"):
        raise CalcViewParseError(
            f"the definition's root element is {root.tag!r}, not a Calculation:scenario, so this "
            "is not a calculation view this parser recognises"
        )

    parsed = ParsedCalcView()
    _read_root(root, parsed)
    _read_data_sources(root, parsed)
    _read_nodes(root, parsed)
    _read_logical_model(root, parsed)
    _read_parameters(root, parsed)
    return parsed


def _read_root(root: ElementTree.Element, parsed: ParsedCalcView) -> None:
    descriptions = _child(root, "descriptions")
    metadata = _child(root, "metadata")
    parsed.root = {
        "id": _text(root.get("id")),
        "schema_version": _text(root.get("schemaVersion")),
        "data_category": _text(root.get("dataCategory")),
        "output_view_type": _text(root.get("outputViewType")),
        "scenario_type": _text(root.get("calculationScenarioType")),
        "privilege_type": _text(root.get("applyPrivilegeType")),
        "checks_privileges": root.get("checkAnalyticPrivileges") == "true",
        "visibility": _text(root.get("visibility")),
        "description": _text(descriptions.get("defaultDescription"))
        if descriptions is not None
        else None,
        "changed_at": _text(metadata.get("changedAt")) if metadata is not None else None,
    }


def _read_data_sources(root: ElementTree.Element, parsed: ParsedCalcView) -> None:
    container = _child(root, "dataSources")
    if container is None:
        return
    for element in _children(container, "DataSource"):
        if len(parsed.data_sources) >= MAX_DATA_SOURCES:
            parsed.truncated = True
            break
        column_object = _child(element, "columnObject")
        resource = _child(element, "resourceUri")
        parsed.data_sources.append(
            {
                "id": _text(element.get("id")) or "",
                "source_type": _text(element.get("type")),
                "schema_name": _text(column_object.get("schemaName"))
                if column_object is not None
                else None,
                "column_object": _text(column_object.get("columnObjectName"))
                if column_object is not None
                else None,
                "resource_uri": _text(resource.text) if resource is not None else None,
            }
        )


def _read_nodes(root: ElementTree.Element, parsed: ParsedCalcView) -> None:
    container = _child(root, "calculationViews")
    if container is None:
        return
    for element in _children(container, "calculationView"):
        if len(parsed.nodes) >= MAX_NODES:
            parsed.truncated = True
            break
        node_id = _text(element.get("id")) or ""
        raw_type = _text(element.get(_XSI_TYPE))
        node: dict[str, Any] = {
            "id": node_id,
            "node_type": _NODE_TYPES.get(raw_type or "", "other"),
            "raw_type": raw_type,
            "join_type": _text(element.get("joinType")),
            "cardinality": _text(element.get("cardinality")),
            "join_order": _text(element.get("joinOrder")),
            "join_attributes": [
                name
                for attribute in _children(element, "joinAttribute")
                if (name := _text(attribute.get("name"))) is not None
            ],
            "inputs": [],
            "mappings": [],
            "filter": None,
        }
        filter_element = _child(element, "filter")
        if filter_element is not None:
            expression = _expression(filter_element.text)
            node["filter"] = expression
            if expression:
                parsed.filters.append(f"{node_id}: {expression}" if node_id else expression)

        for source in _children(element, "input"):
            # '#Join_1' references another node; a bare name references a data source.
            reference = _text(source.get("node")) or ""
            node["inputs"].append(reference.lstrip("#"))
            for mapping in _children(source, "mapping"):
                if len(node["mappings"]) >= MAX_COLUMNS_PER_NODE:
                    parsed.truncated = True
                    break
                target = _text(mapping.get("target"))
                if target is None:
                    continue
                node["mappings"].append(
                    {
                        "target": target,
                        "source": _text(mapping.get("source")),
                        "value": _text(mapping.get("value")),  # a constant mapping carries this
                        "kind": _text(mapping.get(_XSI_TYPE)),
                        "from_node": reference.lstrip("#") or None,
                    }
                )

        _read_calculated(element, node_id, parsed)
        parsed.nodes.append(node)


def _read_calculated(element: ElementTree.Element, node_id: str, parsed: ParsedCalcView) -> None:
    """Calculated columns of one node, each with the formula that produces it."""
    container = _child(element, "calculatedViewAttributes")
    if container is None:
        return
    for attribute in _children(container, "calculatedViewAttribute"):
        if len(parsed.calculated_columns) >= MAX_CALCULATED_COLUMNS:
            parsed.truncated = True
            return
        formula = _child(attribute, "formula")
        parsed.calculated_columns.append(
            {
                "name": _text(attribute.get("id")) or "",
                "formula": _expression(formula.text) if formula is not None else None,
                "datatype": _text(attribute.get("datatype")),
                "length": _text(attribute.get("length")),
                "expression_language": _text(attribute.get("expressionLanguage")),
                "node": node_id or None,
            }
        )


def _read_logical_model(root: ElementTree.Element, parsed: ParsedCalcView) -> None:
    """The semantic layer: which columns the view publishes, and how each is aggregated."""
    model = _child(root, "logicalModel")
    if model is None:
        return
    parsed.root["final_node"] = _text(model.get("id"))

    for container_tag, child_tag, role in (
        ("attributes", "attribute", "attribute"),
        ("calculatedAttributes", "calculatedAttribute", "attribute"),
        ("baseMeasures", "measure", "measure"),
        ("measures", "measure", "measure"),
        ("calculatedMeasures", "measure", "measure"),
    ):
        container = _child(model, container_tag)
        if container is None:
            continue
        for element in _children(container, child_tag):
            if len(parsed.attributes) >= MAX_SEMANTIC_COLUMNS:
                parsed.truncated = True
                return
            descriptions = _child(element, "descriptions")
            # Explicitly `is not None`: an Element with no children is *falsy* in ElementTree, and
            # <keyMapping/> is always childless - so `a or b` silently discarded every key mapping
            # and reported attributes with no origin. Caught by a test asserting the origin node.
            mapping = _child(element, "keyMapping")
            if mapping is None:
                mapping = _child(element, "measureMapping")
            formula = _child(element, "formula")
            parsed.attributes.append(
                {
                    "name": _text(element.get("id")) or "",
                    "role": role,
                    "description": _text(descriptions.get("defaultDescription"))
                    if descriptions is not None
                    else None,
                    "aggregation": _text(element.get("aggregationType")),
                    "measure_type": _text(element.get("measureType")),
                    "is_key": element.get("key") == "true",
                    "origin_node": _text(mapping.get("columnObjectName"))
                    if mapping is not None
                    else None,
                    "origin_column": _text(mapping.get("columnName"))
                    if mapping is not None
                    else None,
                    "formula": _expression(formula.text) if formula is not None else None,
                    "calculated": container_tag.startswith("calculated"),
                }
            )

    _record_unread_content(model, parsed)


#: Semantic-layer containers this grammar reads. Anything else is reported - but only when it holds
#: something; see :func:`_record_unread_content`.
_KNOWN_MODEL_ELEMENTS = frozenset(
    {
        "descriptions",
        "attributes",
        "calculatedAttributes",
        "baseMeasures",
        "measures",
        "calculatedMeasures",
    }
)


#: Presentation-only subtrees. They describe where a box sits in the modeller, never what the view
#: computes, so their contents must not make a container look substantive.
_PRESENTATION_TAGS = frozenset(
    {"layout", "shapes", "shape", "descriptions", "informationModelLayout"}
)


def _carries_content(element: ElementTree.Element) -> bool:
    """Whether an element holds anything meaningful, ignoring presentation and empty placeholders.

    Two coarser tests were both wrong, and each produced a permanently useless gap list:

    * *listing every unknown tag* reported ``restrictedMeasures``, ``localDimensions`` and
      ``sharedDimensions`` on every view - measured on 25 modelled views, all three present and all
      three empty in every one;
    * *"has children"* then reported ``privateDataFoundation`` on every view, because it always
      carries ``<tableProxies/>``, ``<joins/>`` and a layout block - placeholders and geometry, no
      logic.

    So the test is whether any descendant actually says something: an attribute or text. A populated
    ``privateDataFoundation`` (a real data foundation with joins) still reports, which is the point.
    """
    stack = [element]
    while stack:
        current = stack.pop()
        if current is not element:
            if current.attrib:
                return True
            if (current.text or "").strip():
                return True
        stack.extend(child for child in current if child.tag not in _PRESENTATION_TAGS)
    return bool((element.text or "").strip())


def _record_unread_content(model: ElementTree.Element, parsed: ParsedCalcView) -> None:
    """Note semantic-layer elements that carry content this grammar does not read.

    An entry here means a real modelling feature went unread, which is a gap worth acting on. That
    only holds if the field stays quiet otherwise - see :func:`_carries_content` for the two ways of
    getting this wrong that both filled it with noise on every single view.
    """
    for element in model:
        if element.tag in _KNOWN_MODEL_ELEMENTS or element.tag in _PRESENTATION_TAGS:
            continue
        if _carries_content(element) and element.tag not in parsed.unrecognised_elements:
            parsed.unrecognised_elements.append(element.tag)


def _read_parameters(root: ElementTree.Element, parsed: ParsedCalcView) -> None:
    """Input parameters and variables.

    Measured shape: ``localVariables/variable``, where ``parameter="true"`` marks an *input
    parameter* and its absence marks a *variable*. This release has no ``inputParameter`` element at
    all, so a reader looking for one reports every view as parameterless.
    """
    container = _child(root, "localVariables")
    if container is None:
        return
    for element in _children(container, "variable"):
        if len(parsed.parameters) >= MAX_PARAMETERS:
            parsed.truncated = True
            return
        descriptions = _child(element, "descriptions")
        properties = _child(element, "variableProperties")
        selection = _child(properties, "selection") if properties is not None else None
        parsed.parameters.append(
            {
                "name": _text(element.get("id")) or "",
                "is_input_parameter": element.get("parameter") == "true",
                "description": _text(descriptions.get("defaultDescription"))
                if descriptions is not None
                else None,
                "datatype": _text(properties.get("datatype")) if properties is not None else None,
                "length": _text(properties.get("length")) if properties is not None else None,
                "mandatory": (properties.get("mandatory") == "true")
                if properties is not None
                else None,
                "selection_type": _text(selection.get("type")) if selection is not None else None,
            }
        )
