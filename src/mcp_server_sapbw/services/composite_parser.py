"""Read a CompositeProvider's stored model: its inputs, and their field mappings.

A pure function over the XML BW stores on ``RSOHCPR``. No database access, so it is fully testable
against a fixture and the repository above it owns every read.

**The shape, as measured.** All 108 active CompositeProviders on a production system parse with this
grammar, with no failures and no input left undecoded:

.. code-block:: text

    <Composite:compositeView name="SALES_CP" withHanaModel="true" defaultNode="#///U1">
      <viewNode xsi:type="View:Union" name="U1">
        <element xsi:type="BwCore:BwElement" name="0BILL_NUM" infoObjectName="0BILL_NUM"/>
        <input xsi:type="Composite:CompositeInput" alias="U1.ADSO.1" selectAll="false">
          <entity>SALES_ADSO.composite#//</entity>
          <mapping xsi:type="Type:ElementMapping" targetName="0BILL_NUM" sourceName="0BILL_NUM"/>
        </input>
      </viewNode>
      <viewNode xsi:type="View:JoinNode" name="J1">
        <input alias="J1.CALC.1"><entity>PKG/SUB/CV_MASTER.calculationview#/</entity>...</input>
        <input alias="U1"><viewNode xsi:type="View:Union">#///U1</viewNode>...</input>
      </viewNode>
    </Composite:compositeView>

**A stacked provider references its own nodes.** The second ``input`` above names no entity: its
child is a ``viewNode`` reference (``#///U1``) pointing at another node of the same model. That is
an edge *inside* the CompositeProvider, not a part provider, and the distinction is load-bearing
twice over - 40 of 226 inputs on the measured system are of this kind, so treating them as parts
reports 40 unresolved objects that do not exist, and a field-level walk that stops at one silently
loses every part beneath it. Inputs are therefore attributed to their owning node and an internal
reference recorded as such, which is what lets ``CompositeModel.resolve_field`` cross the stack.

Three decodings, each verified against the catalogue that owns the object rather than assumed:

``entity`` text
    ``<name>.<kind>#<path>``. The suffix separates a BW object (``composite``) from a calculation
    view (``calculationview``); 141 and 45 respectively on the measured system.
``$NS$NAME``
    How the model writes a namespaced BW name, because ``/`` is a path separator in this document.
    Decoded to ``/NS/NAME``, which is what the catalogues hold.
``alias``
    ``<node>.<KIND>.<n>``, where the middle segment is BW's own kind code. Confirmed live:
    ADSO 72/72 in RSOADSO, CUBE 2/2 in RSDCUBE, ODSO 2/2 in RSDODSO, IOBJ 10/10 in RSDIOBJ,
    HCPR 1/1 in RSOHCPR, CALC 2/2 in _SYS_BIC. ``FBPA`` appeared twice and matched no catalogue, so
    it decodes to ``unknown`` carrying the code - a part type guessed wrong sends a change review to
    the wrong object, which is worse than admitting the code is not decoded here.

A calculation-view part needs one further step. The model writes its package path with slashes
(``PKG/SUB/LEAF/CV_NAME``) while the ``_SYS_BIC`` runtime view name uses dots for the package and a
slash only before the object (``PKG.SUB.LEAF/CV_NAME``). Joining on the model's form finds nothing;
verified both ways against SYS.VIEWS.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

from ..models.composite import CompositePartKind

# Bounds. A CompositeProvider is a modelling artefact, so these sit far above anything measured
# (the widest on the reference system has 8 inputs, 983 mappings and 317 elements) and exist so a
# pathological definition cannot produce an unbounded reply.
MAX_INPUTS = 64
MAX_MAPPINGS_PER_INPUT = 4000
MAX_ELEMENTS = 4000
#: Largest definition fetched. The measured maximum is 273 KB; a BW-generated document an order of
#: magnitude past that costs the caller more than the answer is worth.
MAX_DEFINITION_BYTES = 8 * 1024 * 1024

_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

#: A document type declaration in a BW model is never legitimate and is the precondition for every
#: entity-expansion and external-entity attack, so its presence ends the parse. Same rule as the
#: calculation-view parser, for the same reason.
_REFUSE_DTD = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

#: ``<name>.<kind>#<path>`` out of an ``entity`` reference. Non-greedy on the name so a name
#: containing a dot keeps everything up to the *last* dot before the kind.
_ENTITY_REF = re.compile(r"^(?P<name>.+)\.(?P<kind>[A-Za-z]+)#")

#: BW's kind code in an input alias -> the canonical part type. Only codes confirmed against the
#: catalogue that owns them on a live system are here; anything else stays ``unknown`` on purpose.
_ALIAS_KIND: dict[str, CompositePartKind] = {
    "ADSO": "adso",
    "CUBE": "infocube",
    "ODSO": "dso",
    "DSO": "dso",
    "IOBJ": "infoobject",
    "HCPR": "compositeprovider",
    "CALC": "calcview",
}

#: The entity suffix that means the reference is a calculation view rather than a BW object. Used to
#: correct the alias where the two disagree, since the suffix is the more specific statement.
_CALC_VIEW_SUFFIX = "calculationview"

#: ``xsi:type`` on a mapping -> what kind of mapping it is. ``ConstantElementMapping`` is the one
#: that must not be read as a field: it has no source field, and inventing one would put a field
#: name on a hard-coded value.
_MAPPING_KINDS: dict[str, str] = {
    "Type:ElementMapping": "element",
    "Type:ConstantElementMapping": "constant",
}

#: ``$NS$NAME`` needs a leading marker and a closing one, so two ``$`` at minimum.
_NAMESPACE_MARKERS = 2
#: ``<node>.<KIND>.<n>`` - three dot-separated segments, and the kind is the middle one.
_ALIAS_SEGMENTS = 3


class CompositeParseError(Exception):
    """The model could not be read. Carries a reason fit to return to a caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ParsedComposite:
    """What one model yielded. A plain carrier; the repository maps it to the pydantic models."""

    __slots__ = (
        "bounds",
        "default_node",
        "element_count",
        "inputs",
        "node_name",
        "node_type",
        "nodes",
        "root",
    )

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self.node_type: str | None = None
        self.node_name: str | None = None
        self.default_node: str | None = None
        self.nodes: list[dict[str, Any]] = []
        self.element_count: int = 0
        self.inputs: list[dict[str, Any]] = []
        #: ``(scope, limit)`` per bound that actually bound. Named rather than a single flag: an
        #: input list cut short and an element count cut short send a caller to different places.
        self.bounds: list[tuple[str, int]] = []

    def note_bound(self, scope: str, limit: int) -> None:
        """Record a bound once, so a repeated hit does not multiply the report."""
        if not any(existing == scope for existing, _limit in self.bounds):
            self.bounds.append((scope, limit))


def _tag(element: ElementTree.Element) -> str:
    """The local name, with any namespace stripped."""
    return element.tag.rpartition("}")[2]


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def decode_entity_ref(reference: str | None) -> tuple[str, str] | None:
    """``(object name, reference kind)`` out of an ``entity`` reference, or ``None``.

    The name is returned in the form the BW catalogues hold it, which means undoing the ``$NS$``
    encoding the model uses because ``/`` is a path separator in this document.
    """
    cleaned = _text(reference)
    if cleaned is None:
        return None
    matched = _ENTITY_REF.match(cleaned)
    if matched is None:
        return None
    name = matched.group("name").strip()
    if name.startswith("$") and name.count("$") >= _NAMESPACE_MARKERS:
        namespace, _, rest = name[1:].partition("$")
        if namespace and rest:
            name = f"/{namespace}/{rest}"
    return (name, matched.group("kind").lower()) if name else None


def alias_kind_code(alias: str | None) -> str | None:
    """BW's kind code out of an input alias (``U1.ADSO.1`` -> ``ADSO``)."""
    cleaned = _text(alias)
    if cleaned is None:
        return None
    parts = cleaned.split(".")
    if len(parts) < _ALIAS_SEGMENTS or not parts[1].strip():
        return None
    return parts[1].strip().upper()


def decode_node_ref(reference: str | None) -> str | None:
    """A node name out of an internal reference (``#///U1`` -> ``U1``, ``defaultNode`` likewise)."""
    cleaned = _text(reference)
    if cleaned is None:
        return None
    name = cleaned.rpartition("/")[2].strip()
    return name or None


def runtime_view_name(model_name: str) -> str | None:
    """The ``_SYS_BIC`` runtime name for a calc-view reference written in the model's own form.

    ``PKG/SUB/CV_NAME`` -> ``PKG.SUB/CV_NAME``. The package separator differs between the two
    forms, and a join on the model's form matches nothing (verified both ways against SYS.VIEWS).
    """
    cleaned = _text(model_name)
    if cleaned is None or "/" not in cleaned:
        return None
    package, _, view = cleaned.rpartition("/")
    if not package or not view:
        return None
    return f"{package.replace('/', '.')}/{view}"


def parse_composite_model(definition: str) -> ParsedComposite:
    """Read a ``Composite:compositeView`` model. Raises :class:`CompositeParseError` on refusal."""
    if not definition or not definition.strip():
        raise CompositeParseError("the stored CompositeProvider model is empty")
    if _REFUSE_DTD.search(definition):
        raise CompositeParseError(
            "the model carries a document type declaration, which a CompositeProvider definition "
            "never does and which is the precondition for entity-expansion attacks; it was not "
            "parsed"
        )
    try:
        root = ElementTree.fromstring(definition)
    except ElementTree.ParseError as exc:
        raise CompositeParseError(f"the model is not well-formed XML: {exc}") from exc
    if not _tag(root).endswith("compositeView"):
        raise CompositeParseError(
            f"the model's root element is {_tag(root)!r}, not a compositeView, so this is not a "
            "CompositeProvider definition this parser recognises"
        )

    parsed = ParsedComposite()
    parsed.root = {
        "name": _text(root.get("name")),
        "schema_version": _text(root.get("schemaVersion")),
        "with_hana_model": (root.get("withHanaModel") or "").strip().lower() == "true",
    }
    parsed.default_node = decode_node_ref(root.get("defaultNode"))
    _read_nodes(root, parsed)
    _count_elements(root, parsed)
    return parsed


def _count_elements(root: ElementTree.Element, parsed: ParsedComposite) -> None:
    """How many elements the provider publishes, across every node."""
    count = 0
    for element in root.iter():
        if _tag(element) == "element":
            count += 1
            if count >= MAX_ELEMENTS:
                parsed.note_bound("elements", MAX_ELEMENTS)
                break
    parsed.element_count = count


def _read_nodes(root: ElementTree.Element, parsed: ParsedComposite) -> None:
    """Every view node and its inputs, with each input attributed to its owning node.

    Attribution is what makes a stacked provider readable. A join node's inputs and the union node's
    inputs are different sets, and flattening them loses which combination produces which field -
    so the field walk would have to guess, and a union of two objects would be indistinguishable
    from a join of them.

    A ``viewNode`` element that carries no ``name`` is an internal *reference* to a node rather than
    a node declaration, and is skipped here; ``_read_one_input`` reads it as the input's target.
    """
    for element in root.iter():
        if _tag(element) != "viewNode":
            continue
        node_name = _text(element.get("name"))
        if node_name is None:
            continue  # a reference, handled by the input that owns it
        node_type = _text(element.get(_XSI_TYPE))
        parsed.nodes.append({"name": node_name, "node_type": node_type})
        # The first declared node is reported as the top one where the model names no default. Both
        # are recorded, so a caller never has to rely on document order.
        if parsed.node_name is None:
            parsed.node_name, parsed.node_type = node_name, node_type
        for child in element:
            if _tag(child) != "input":
                continue
            if len(parsed.inputs) >= MAX_INPUTS:
                parsed.note_bound("inputs", MAX_INPUTS)
                return
            entry = _read_one_input(child, node_name)
            if entry["mapping_count"] > len(entry["mappings"]):
                parsed.note_bound("mappings_per_input", MAX_MAPPINGS_PER_INPUT)
            parsed.inputs.append(entry)
    if parsed.default_node is not None:
        for node in parsed.nodes:
            if node["name"] == parsed.default_node:
                parsed.node_name, parsed.node_type = node["name"], node["node_type"]
                break


def _read_one_input(element: ElementTree.Element, node_name: str) -> dict[str, Any]:
    alias = _text(element.get("alias")) or ""
    entity_ref: str | None = None
    internal_node: str | None = None
    mappings: list[dict[str, Any]] = []
    declared = 0
    for child in element:
        kind = _tag(child)
        if kind == "entity" and entity_ref is None:
            entity_ref = _text(child.text)
        elif kind == "viewNode" and internal_node is None:
            # An input whose target is another node of this same model - the stacked case.
            internal_node = decode_node_ref(child.text)
        elif kind == "mapping":
            declared += 1
            if len(mappings) >= MAX_MAPPINGS_PER_INPUT:
                continue
            mapped = _read_mapping(child)
            if mapped is not None:
                mappings.append(mapped)
    decoded = decode_entity_ref(entity_ref)
    code = alias_kind_code(alias)
    part_name = decoded[0] if decoded else ""
    part_kind: CompositePartKind = _ALIAS_KIND.get(code or "", "unknown")
    # The entity suffix is the more specific statement of the two: it says what kind of *document*
    # is being referenced, where the alias only carries BW's shorthand. Where they disagree the
    # suffix wins, so a calc view referenced under an unexpected alias is still typed correctly.
    if decoded is not None and decoded[1] == _CALC_VIEW_SUFFIX:
        part_kind = "calcview"
    return {
        "node_name": node_name,
        "alias": alias,
        "part_name": part_name,
        "part_kind": part_kind,
        "alias_kind": code,
        "entity_ref": entity_ref,
        "internal_node": internal_node,
        "runtime_view_name": runtime_view_name(part_name) if part_kind == "calcview" else None,
        "select_all": (element.get("selectAll") or "").strip().lower() == "true",
        "mappings": mappings,
        "mapping_count": declared,
    }


def _read_mapping(element: ElementTree.Element) -> dict[str, Any] | None:
    """One ``target <- source`` field mapping, or ``None`` when it names no target.

    A mapping with no target cannot be joined to anything, so it is dropped rather than carried as
    an entry with an empty key that a lookup would then match by accident.
    """
    target = _text(element.get("targetName"))
    if target is None:
        return None
    raw_type = _text(element.get(_XSI_TYPE))
    kind = _MAPPING_KINDS.get(raw_type or "", "unknown")
    source = _text(element.get("sourceName"))
    return {
        "target_field": target,
        # A constant mapping has no source field. Carrying one would put a field name on a value
        # that was hard-coded in the model, and a field-level trace would then follow it upward.
        "source_field": None if kind == "constant" else source,
        "mapping_kind": kind,
        "raw_type": raw_type,
    }
