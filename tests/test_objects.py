"""The canonical object model, and the guarantee that the subsystem vocabularies stay aligned.

The drift this prevents was real and invisible: ``LineageNodeType`` called a basic InfoCube ``cube``
while every other surface called it ``infocube``, and no test noticed - the suite passed either way,
so a caller correlating a lineage node against a described object had to know the two words meant
one thing. The alignment test below walks each vocabulary's own ``Literal`` and fails when a value
does not normalise to a canonical type, so a new spelling has to be a decision rather than an
accident.
"""

from __future__ import annotations

import typing

from mcp_server_sapbw.models.lineage import LineageNode, LineageNodeType
from mcp_server_sapbw.models.objects import (
    PROVIDER_TYPES,
    TLOGO_TO_TYPE,
    TYPE_ALIASES,
    BwObjectRef,
    BwObjectType,
    normalise_object_type,
    object_id,
)
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.providers import (
    Provider,
    ProviderType,
    SearchHit,
    SearchObjectType,
)
from mcp_server_sapbw.models.transformations import EndpointKind


def _values(alias: object) -> list[str]:
    return [str(v) for v in typing.get_args(alias)]


_CANONICAL_VALUES = set(_values(BwObjectType))


# --- normalisation ------------------------------------------------------------------------


def test_canonical_values_normalise_to_themselves() -> None:
    for value in sorted(_CANONICAL_VALUES):
        assert normalise_object_type(value) == value


def test_tlogo_codes_decode() -> None:
    assert normalise_object_type("CUBE") == "infocube"
    assert normalise_object_type("ODSO") == "dso"
    assert normalise_object_type("HCPR") == "compositeprovider"
    assert normalise_object_type("ELEM") == "query_element"
    # Case-insensitive, because these arrive from several columns with inconsistent padding.
    assert normalise_object_type(" cube ") == "infocube"


def test_the_old_lineage_spelling_still_normalises() -> None:
    """A caller holding a stored `cube` from an older response must not break."""
    assert normalise_object_type("cube") == "infocube"
    assert normalise_object_type("other") == "unknown"


def test_unrecognised_type_becomes_unknown_not_a_pass_through() -> None:
    """An unrecognised type used as a graph key would split one object across two nodes."""
    assert normalise_object_type("SomethingElse") == "unknown"
    assert normalise_object_type(None) == "unknown"
    assert normalise_object_type("") == "unknown"


# --- identity -----------------------------------------------------------------------------


def test_object_id_is_type_qualified_and_upper_cased() -> None:
    """BW names are only near-unique: a DSO and an InfoObject can share one."""
    assert object_id("dso", "sales_dso") == "dso:SALES_DSO"
    assert object_id("CUBE", "sales_cube") == "infocube:SALES_CUBE"
    assert object_id("dso", "SALES") != object_id("infoobject", "SALES")


def test_object_id_is_stable_across_spellings_of_the_same_type() -> None:
    assert object_id("cube", "X") == object_id("infocube", "X") == object_id("CUBE", "X")


def test_ref_exposes_its_id_and_provider_status() -> None:
    ref = BwObjectRef(object_type="adso", name="fin_adso")
    assert ref.id == "adso:FIN_ADSO"
    assert ref.is_provider is True
    assert BwObjectRef(object_type="transformation", name="TR1").is_provider is False


def test_ref_from_tlogo_keeps_the_raw_code() -> None:
    ref = BwObjectRef.from_tlogo("CUBE", "SALES_CUBE")
    assert (ref.object_type, ref.subtype, ref.id) == ("infocube", "CUBE", "infocube:SALES_CUBE")


def test_ref_from_an_undocumented_tlogo_code_stays_visible() -> None:
    """The canonical type degrades, but the code BW actually stored is not thrown away."""
    ref = BwObjectRef.from_tlogo("ZZZZ", "THING")
    assert (ref.object_type, ref.subtype) == ("unknown", "ZZZZ")


def test_id_is_serialised_so_a_client_sees_it() -> None:
    assert BwObjectRef(object_type="dso", name="A").model_dump()["id"] == "dso:A"


# --- the vocabularies stay aligned --------------------------------------------------------

_SUBSYSTEM_VOCABULARIES: dict[str, object] = {
    "ProviderType": ProviderType,
    "SearchObjectType": SearchObjectType,
    "LineageNodeType": LineageNodeType,
    "EndpointKind": EndpointKind,
}


def test_every_subsystem_value_normalises_to_a_canonical_type() -> None:
    unaligned: list[str] = []
    for name, alias in _SUBSYSTEM_VOCABULARIES.items():
        for value in _values(alias):
            if normalise_object_type(value) == "unknown" and value not in {"unknown", "other"}:
                unaligned.append(f"{name}.{value!r}")
    assert not unaligned, (
        "these subsystem object types do not map to a canonical BwObjectType, so a caller "
        f"correlating two surfaces cannot tell they mean the same object: {unaligned}. Add the "
        "value to BwObjectType, or an alias to TYPE_ALIASES, in models/objects.py."
    )


def test_the_infocube_spelling_is_now_the_same_everywhere() -> None:
    """The specific drift this model exists to fix: one object, one word, on every surface."""
    for name, alias in _SUBSYSTEM_VOCABULARIES.items():
        values = _values(alias)
        assert "cube" not in values, f"{name} still spells a basic InfoCube 'cube'"
        assert "infocube" in values, f"{name} cannot express a basic InfoCube"


def test_lineage_nodes_can_express_a_virtual_provider() -> None:
    """It previously arrived as a plain cube, discarding a distinction the provider list makes."""
    assert "virtualprovider" in _values(LineageNodeType)
    assert "infocube" in _values(LineageNodeType)


def test_carrying_models_expose_the_same_join_key() -> None:
    """The point of the canonical id: one InfoCube, one key, across three unrelated tools."""
    prov = Provenance(source_table="RSDCUBE")
    described = Provider(name="SALES_CUBE", object_type="infocube", provenance=prov)
    hit = SearchHit(name="SALES_CUBE", object_type="infocube", matched_on="name", provenance=prov)
    node = LineageNode(id="SALES_CUBE", object_type="infocube", name="SALES_CUBE", provenance=prov)
    assert described.ref is not None and hit.ref is not None and node.ref is not None
    assert described.ref.id == hit.ref.id == node.ref.id == "infocube:SALES_CUBE"


def test_lineage_node_keeps_its_internal_graph_key_separate() -> None:
    """Edges reference `id`; changing it would break stored graphs. `ref.id` is the join key."""
    node = LineageNode(
        id="SALES_CUBE",
        object_type="infocube",
        name="SALES_CUBE",
        provenance=Provenance(source_table="RSTRAN"),
    )
    assert node.id == "SALES_CUBE"
    assert node.ref is not None and node.ref.id == "infocube:SALES_CUBE"


def test_provider_ref_carries_the_raw_subtype() -> None:
    provider = Provider(
        name="SALES_CUBE",
        object_type="infocube",
        subtype="B",
        provenance=Provenance(source_table="RSDCUBE"),
    )
    assert provider.ref is not None and provider.ref.subtype == "B"


def test_provider_types_are_a_subset_of_the_canonical_vocabulary() -> None:
    assert PROVIDER_TYPES <= _CANONICAL_VALUES
    for value in _values(ProviderType):
        if value != "infoobject":  # an InfoObject is master data, not a provider
            assert value in PROVIDER_TYPES, f"{value} is a ProviderType but not in PROVIDER_TYPES"


def test_every_tlogo_target_is_canonical() -> None:
    for code, target in sorted(TLOGO_TO_TYPE.items()):
        assert target in _CANONICAL_VALUES, f"TLOGO {code} maps to non-canonical {target!r}"


def test_every_alias_target_is_canonical() -> None:
    for alias, target in sorted(TYPE_ALIASES.items()):
        assert target in _CANONICAL_VALUES, f"alias {alias!r} maps to non-canonical {target!r}"


def test_no_alias_shadows_a_canonical_value() -> None:
    """An alias for a canonical name is a second spelling - the thing this model removes."""
    shadowed = sorted(set(TYPE_ALIASES) & _CANONICAL_VALUES)
    assert not shadowed, f"aliases shadow canonical types: {shadowed}"
