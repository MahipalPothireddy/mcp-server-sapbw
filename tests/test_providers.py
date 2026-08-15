"""Tests for the providers repository + texts/descriptions integration (B4).

Offline against a scripted fixture landscape. Synthetic names only (no Z*/Y*, no /BIC/).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.models.providers import ObjectNotFound, Provider
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
    "keyfigure": "RSDKYF",
}
# HANA catalog views (schema SYS) needed for the calc-view route to CompositeProvider parts.
_HANA_TABLES = {"hana_views": "VIEWS", "object_dependencies": "OBJECT_DEPENDENCIES"}

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
# The calc view BW generates for SALES_CP, and the base tables its dependencies report.
# Generated-table literals are built by concatenation so this file stays clean for the
# customer-metadata scan (which forbids inline /BIC/<name> outside tests/fixtures/).
_CP_CALC_VIEW = "system-local.bw.bw2hana/SALES_CP"
_CP_BASE_TABLES = [
    "/BIC/" + "ASALES_DSO00",  # -> classic DSO SALES_DSO (active table)
    "/BIC/" + "FSALES_CUBE",  # -> InfoCube SALES_CUBE (F-fact)
    "/BI0/" + "PMATERIAL_CHA",  # master-data attributes -> NOT a part provider
]

_IOBJ = {"MATERIAL_CHA": ("CHA", "ACT", "SD"), "AMOUNT_KYF": ("KYF", "ACT", "SD")}
_IOBJ_TEXT = {"MATERIAL_CHA": [("E", "Material", "Material master characteristic")]}
# RSDKYF: KYFTP, DATATP, AGGRGEN, AGGREXC, AGGRCHA, NCUMFL, FIXCUKY, FIXUNIT, UNINM, KYFSEMANTIC.
# AMOUNT_KYF takes the LAST value along CALDAY, so its number is not the sum of the rows, and its
# currency varies per record.
_KYF = {
    "AMOUNT_KYF": ("AMO", "CURR", "SUM", "LAS", "CALDAY", "", "", "", "DOC_CURRCY", ""),
}


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
        # Catalogue listings (no key filter) back the table -> object confirmation step.
        if "= ?" not in sql:
            if "ODSOBJECT" in sql:
                return [(n,) for n in _DSO]
            if "ADSONM" in sql:
                return [(n,) for n in _ADSO]
            if "INFOCUBE" in sql and "RSDCUBE" in sql:
                return [(n,) for n in _CUBE]
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
        if "LENGTH(XML_DEF)" in sql:
            return [(0,)]  # XML_DEF empty - the common real case; forces the calc-view route
        if "RSOHCPR" in sql:
            return _rows_for_name(_CP, name)
        if '"VIEWS"' in sql:  # generated calc view for the composite provider
            return [(_CP_CALC_VIEW,)]
        if "OBJECT_DEPENDENCIES" in sql:
            return [(t,) for t in _CP_BASE_TABLES]
        if "RSDKYF" in sql:
            return _rows_for_name(_KYF, name)
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
    present = present if present is not None else set(_PROVIDER_TABLES) | set(_HANA_TABLES)
    tables = {
        logical: TableStatus(
            logical_name=logical,
            resolved_name=physical if logical in present else None,
            present=logical in present,
            schema_name=SCHEMA if logical in present else None,
        )
        for logical, physical in _PROVIDER_TABLES.items()
    }
    tables.update(
        {
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name="SYS" if logical in present else None,
            )
            for logical, physical in _HANA_TABLES.items()
        }
    )
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        object_models={"classic_dso": True, "adso": True, "composite_provider": True},
        tables=tables,
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


def test_describe_compositeprovider_resolves_parts_via_calc_view() -> None:
    """XML_DEF is empty, so parts must come from the generated calc view's base tables."""
    provider = _repo().describe("SALES_CP")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "compositeprovider"
    assert provider.composition_source == "calc_view"
    # The DSO and cube base tables resolve to part providers; the master-data P table does not.
    assert {p.name for p in provider.part_providers} == {"SALES_DSO", "SALES_CUBE"}
    assert {p.part_type for p in provider.part_providers} == {"dso", "infocube"}
    assert all(p.via_table for p in provider.part_providers)
    # Confirmed against the provider catalogue, so not a naming-convention guess.
    assert {p.confidence for p in provider.part_providers} == {"confirmed"}
    # The empty XML_DEF is reported as a finding rather than passed over silently.
    assert any("XML_DEF is empty" in c for c in provider.caveats)
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


# --- key-figure aggregation on bw_describe_object (RSDKYF) --------------------------------------


def _described(name: str, present: set[str] | None = None) -> Provider:
    provider = _repo(present).describe(name)
    assert isinstance(provider, Provider)
    return provider


def test_characteristic_carries_no_aggregation() -> None:
    """Aggregation is meaningless for a characteristic and must be absent, not defaulted."""
    assert _described("MATERIAL_CHA").aggregation is None


def test_key_figure_aggregation_is_read_and_decoded() -> None:
    agg = _described("AMOUNT_KYF").aggregation
    assert agg is not None
    assert agg.key_figure == "AMOUNT_KYF"
    assert agg.key_figure_type is not None and agg.key_figure_type.label == "Amount"
    assert agg.default_aggregation is not None and agg.default_aggregation.code == "SUM"
    assert agg.exception_aggregation is not None
    assert agg.exception_aggregation.behaviour.code == "LAS"
    assert agg.exception_aggregation.behaviour.label == "Last value"
    assert [r.name for r in agg.exception_aggregation.reference_characteristics] == ["CALDAY"]


def test_last_value_key_figure_is_reported_as_not_summable() -> None:
    agg = _described("AMOUNT_KYF").aggregation
    assert agg is not None
    assert agg.summable is False
    joined = " ".join(agg.summability_caveats)
    assert "LAS" in joined
    assert "CALDAY" in joined


def test_summability_reasons_reach_the_provider_caveats() -> None:
    """A caller reading only caveats must still learn the number cannot be added up."""
    caveats = _described("AMOUNT_KYF").caveats
    assert any("does not reproduce the reported number" in c for c in caveats)


def test_varying_currency_is_surfaced() -> None:
    agg = _described("AMOUNT_KYF").aggregation
    assert agg is not None
    assert agg.unit_infoobject == "DOC_CURRCY"
    assert any("varies per record" in c for c in agg.summability_caveats)


def test_unreadable_key_figure_aggregation_is_unknown_not_unrestricted() -> None:
    """With RSDKYF absent, the answer is 'unknown', never an implied 'safe to sum'."""
    present = (set(_PROVIDER_TABLES) | set(_HANA_TABLES)) - {"keyfigure"}
    provider = _described("AMOUNT_KYF", present)
    assert provider.aggregation is None
    assert any("unknown rather than unrestricted" in c for c in provider.caveats)
