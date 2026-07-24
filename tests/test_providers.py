"""Tests for the providers repository + texts/descriptions integration (B4).

Offline against a scripted fixture landscape. Synthetic names only (no Z*/Y*, no /BIC/).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.models.providers import ObjectNotFound
from mcp_server_sapbw.repositories.providers import ProvidersRepository

SCHEMA = "TESTSCHEMA"
_PROVIDER_TABLES = {
    "dso_header": "RSDODSO",
    "dso_field": "RSDODSOIOBJ",
    "dso_text": "RSDODSOT",
    "adso_header": "RSOADSO",
    "adso_text": "RSOADSOT",
    "adso_keyfields": "RSOADSOKEYFIELDS",
    "cube_header": "RSDCUBE",
    "cube_field": "RSDCUBEIOBJ",
    "cube_text": "RSDCUBET",
    "multiprovider_part": "RSDCUBEMULTI",
    "composite_header": "RSOHCPR",
    "composite_text": "RSOHCPRT",
    "infoobject": "RSDIOBJ",
    "infoobject_text": "RSDIOBJT",
}

# --- synthetic landscape ------------------------------------------------------------------

_DSO = {"SALES_DSO": ("", "SALES", "DEVUSER", "SD")}  # ODSOTYPE, INFOAREA, OWNER, BWAPPL
_DSO_FIELDS = {"SALES_DSO": [("DOC", 1, "X"), ("ITEM", 2, "X"), ("AMOUNT", 3, "")]}
_DSO_TEXT = {"SALES_DSO": [("E", "Sales orders", "Daily sales order line items")]}

_ADSO = {"FIN_ADSO": ("FIN", "DEVUSER", "FI")}  # INFOAREA, OWNER, BWAPPL
_ADSO_KEYS = {"FIN_ADSO": [("GL_ACCOUNT", 1)]}  # FIELDNM, POSIT
_ADSO_TEXT_OBJ = {"FIN_ADSO": [("E", "Financial postings advanced store", "tooltip")]}
_ADSO_TEXT_FIELDS = {"FIN_ADSO": [("GL_ACCOUNT", "E", "G/L Account"), ("AMOUNT", "E", "Amount")]}

# CUBETYPE, OBJSTAT, INFOAREA, OWNER, BWAPPL
_CUBE = {
    "SALES_CUBE": ("B", "ACT", "SALES", "DEVUSER", "SD"),
    "SALES_MP": ("M", "ACT", "SALES", "DEVUSER", "SD"),
}
_CUBE_FIELDS = {"SALES_CUBE": [("MATERIAL", 1), ("AMOUNT", 2)], "SALES_MP": [("MATERIAL", 1)]}
_CUBE_TEXT = {"SALES_CUBE": [("E", "Sales cube", "Sales InfoCube transaction data")]}
_MP_PARTS = {"SALES_MP": [("SALES_CUBE", 1), ("SALES_DSO", 2)]}

_CP = {"SALES_CP": ("ACT", "SALES", "DEVUSER", "SD")}  # OBJSTAT, INFOAREA, OWNER, BWAPPL
_CP_TEXT_OBJ = {"SALES_CP": [("E", "Sales composite provider view", "tip")]}
_CP_TEXT_FIELDS = {"SALES_CP": [("MATERIAL", "E", "Material")]}

_IOBJ = {"MATERIAL_CHA": ("CHA", "ACT", "SD"), "AMOUNT_KYF": ("KYF", "ACT", "SD")}
_IOBJ_TEXT = {"MATERIAL_CHA": [("E", "Material", "Material master characteristic")]}


def _rows_for_name(table: dict[str, Any], name: str) -> list[tuple[Any, ...]]:
    value = table.get(name)
    if value is None:
        return []
    return [value] if isinstance(value, tuple) else list(value)


class ScriptedConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Sequence[Any] | None]] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append((sql, parameters))
        params = list(parameters or [])
        name = str(params[-1]) if params else ""
        return self._route(sql, name)

    def _route(self, sql: str, name: str) -> list[tuple[Any, ...]]:
        # Order matters: check the more specific physical names before their prefixes.
        if "RSDODSOIOBJ" in sql:
            return _rows_for_name(_DSO_FIELDS, name)
        if "RSDODSOT" in sql:
            return _rows_for_name(_DSO_TEXT, name)
        if "RSDODSO" in sql:
            return _rows_for_name(_DSO, name)
        if "RSOADSOKEYFIELDS" in sql:
            return _rows_for_name(_ADSO_KEYS, name)
        if "RSOADSOT" in sql:
            return self._hana_text(sql, name, _ADSO_TEXT_OBJ, _ADSO_TEXT_FIELDS)
        if "RSOADSO" in sql:
            return _rows_for_name(_ADSO, name)
        return self._route_cube_and_below(sql, name)

    def _route_cube_and_below(self, sql: str, name: str) -> list[tuple[Any, ...]]:
        if "RSDCUBEIOBJ" in sql:
            return _rows_for_name(_CUBE_FIELDS, name)
        if "RSDCUBEMULTI" in sql:
            return _rows_for_name(_MP_PARTS, name)
        if "RSDCUBET" in sql:
            return _rows_for_name(_CUBE_TEXT, name)
        if "RSDCUBE" in sql:
            return _rows_for_name(_CUBE, name)
        if "RSOHCPRT" in sql:
            return self._hana_text(sql, name, _CP_TEXT_OBJ, _CP_TEXT_FIELDS)
        if "RSOHCPR" in sql:
            return _rows_for_name(_CP, name)
        if "RSDIOBJT" in sql:
            return _rows_for_name(_IOBJ_TEXT, name)
        if "RSDIOBJ" in sql:
            return _rows_for_name(_IOBJ, name)
        return []

    @staticmethod
    def _hana_text(
        sql: str,
        name: str,
        obj_table: dict[str, Any],
        field_table: dict[str, Any],
    ) -> list[tuple[Any, ...]]:
        # object-level rows use TRIM(COLNAME) = '' ; field-level use TRIM(COLNAME) <> ''
        if "<>" in sql:
            return _rows_for_name(field_table, name)
        return _rows_for_name(obj_table, name)


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_PROVIDER_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        object_models={"classic_dso": True, "adso": True, "composite_provider": True},
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _PROVIDER_TABLES.items()
        },
    )


def _repo(present: set[str] | None = None) -> ProvidersRepository:
    return ProvidersRepository(ScriptedConnection(), _capability(present))


# --- classic DSO --------------------------------------------------------------------------


def test_describe_dso() -> None:
    provider = _repo().describe("SALES_DSO")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "dso"
    assert provider.info_area == "SALES"
    assert [f.name for f in provider.fields] == ["DOC", "ITEM", "AMOUNT"]
    assert provider.key_field_names == ["DOC", "ITEM"]
    assert provider.provenance  # present
    # a good stored description is returned verbatim and labelled 'stored'
    assert provider.description is not None
    assert provider.description.origin == "stored"
    assert provider.description.quality_flag == "ok"
    assert provider.description.description_long == "Daily sales order line items"
    assert provider.fields[0].provenance.source_table == "RSDODSOIOBJ"


# --- advanced DSO -------------------------------------------------------------------------


def test_describe_adso_hana_text_and_keys() -> None:
    provider = _repo().describe("FIN_ADSO")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "adso"
    field_names = {f.name for f in provider.fields}
    assert {"GL_ACCOUNT", "AMOUNT"} <= field_names
    gl = next(f for f in provider.fields if f.name == "GL_ACCOUNT")
    assert gl.is_key is True
    assert gl.description == "G/L Account"
    assert provider.key_field_names == ["GL_ACCOUNT"]
    assert provider.description is not None
    assert provider.description.description_short == "Financial postings advanced store"


# --- cube / MultiProvider -----------------------------------------------------------------


def test_describe_infocube() -> None:
    provider = _repo().describe("SALES_CUBE")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "infocube"
    assert provider.subtype == "B"
    assert provider.active is True
    assert {f.name for f in provider.fields} == {"MATERIAL", "AMOUNT"}


def test_describe_multiprovider_parts_and_generated_description() -> None:
    provider = _repo().describe("SALES_MP")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "multiprovider"
    assert provider.composition_source == "relational"
    assert [p.name for p in provider.part_providers] == ["SALES_CUBE", "SALES_DSO"]
    # SALES_MP has no stored text -> generated, clearly labelled (mission Rule 7)
    assert provider.description is not None
    assert provider.description.origin == "generated"
    assert provider.description.quality_flag == "missing"
    assert "MultiProvider" in (provider.description.description_short or "")


# --- CompositeProvider --------------------------------------------------------------------


def test_describe_compositeprovider_defers_xml_composition() -> None:
    provider = _repo().describe("SALES_CP")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "compositeprovider"
    assert provider.composition_source == "none"
    assert provider.part_providers == []
    assert any("XML_DEF" in c for c in provider.caveats)
    assert {f.name for f in provider.fields} == {"MATERIAL"}


# --- InfoObject ---------------------------------------------------------------------------


def test_describe_infoobject_kind_and_caveat() -> None:
    provider = _repo().describe("MATERIAL_CHA")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "infoobject"
    assert provider.infoobject_kind == "characteristic"
    assert any("attributes" in c.lower() for c in provider.caveats)


# --- resolution / gating ------------------------------------------------------------------


def test_autodetect_finds_dso() -> None:
    provider = _repo().describe("SALES_DSO", object_type=None)
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "dso"


def test_explicit_type_multiprovider() -> None:
    provider = _repo().describe("SALES_MP", object_type="multiprovider")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "multiprovider"


def test_object_not_found() -> None:
    result = _repo().describe("DOES_NOT_EXIST")
    assert isinstance(result, ObjectNotFound)
    assert result.status == "not_found"
    assert "dso" in result.searched_types


def test_unsupported_when_type_tables_absent() -> None:
    # cube tables absent -> explicit cube-family request is unsupported
    repo = _repo(present=set(_PROVIDER_TABLES) - {"cube_header", "cube_field"})
    result = repo.describe("SALES_CUBE", object_type="infocube")
    assert isinstance(result, UnsupportedResult)
    assert result.status == "unsupported_on_release"
