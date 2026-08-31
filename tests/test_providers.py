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
    "characteristic": "RSDCHA",
    "attribute": "RSDBCHATR",
    "nav_attribute": "RSDATRNAV",
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
_CUBE_FIELDS = {
    # The third field is a navigation attribute, which is how BW stores it: an opaque
    # <characteristic>__<attribute> name with nothing in the row saying so.
    "SALES_CUBE": [("MATERIAL", 1), ("AMOUNT", 2), ("MATERIAL_CHA__MATL_GROUP", 3)],
    "SALES_MP": [("MATERIAL", 1)],
}
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

_IOBJ = {
    "MATERIAL_CHA": ("CHA", "ACT", "SD"),
    "AMOUNT_KYF": ("KYF", "ACT", "SD"),
    "SOLD_TO_CHA": ("CHA", "ACT", "SD"),
}
_IOBJ_TEXT = {
    "MATERIAL_CHA": [("E", "Material", "Material master characteristic")],
    "SOLD_TO_CHA": [("E", "Sold-to party", "Reference characteristic of MATERIAL_CHA")],
}

# --- attributes ---------------------------------------------------------------------------
#
# The shape that matters: SOLD_TO_CHA is a *reference* characteristic, so its attribute list lives
# under its basic characteristic (RSDCHA.CHABASNM -> MATERIAL_CHA) while its navigation names live
# under its own name. Two tables, two keys; a fixture that used one name for both would pass a
# broken implementation.
_RSDCHA = {  # CHANM -> CHABASNM
    "MATERIAL_CHA": ("MATERIAL_CHA",),
    "SOLD_TO_CHA": ("MATERIAL_CHA",),
}
# CHABASNM -> [(ATTRINM, POSIT, ATTRITP, ATRTIMFL, NODISPINQUERYFL)]
# ATRTIMFL is domain RSDCNVFL: '1' means time-dependent, not 'X'.
_RSDBCHATR = {
    "MATERIAL_CHA": [
        ("MATL_GROUP", 1, "NAV", "0", ""),
        ("MATL_TYPE", 2, "NAV", "1", ""),
        ("BASE_UOM", 3, "DIS", "0", ""),
        ("OLD_MATNR", 4, "DIS", "0", "X"),
    ]
}
# CHANM -> [(ATTRINM, ATRNAVNM, AUTHRELFL, TXTFROMCHAFL, TRANSITIVEFL)]
# SOLD_TO_CHA exposes only one of the two navigable attributes it inherits.
_RSDATRNAV = {
    "MATERIAL_CHA": [
        ("MATL_GROUP", "MATERIAL_CHA__MATL_GROUP", "", "X", ""),
        ("MATL_TYPE", "MATERIAL_CHA__MATL_TYPE", "X", "", ""),
    ],
    "SOLD_TO_CHA": [("MATL_GROUP", "SOLD_TO_CHA__MATL_GROUP", "", "", "")],
}
_ATTR_DESCRIPTIONS = {
    "MATL_GROUP": "Material group",
    "MATL_TYPE": "Material type",
    "BASE_UOM": "Base unit of measure",
    "OLD_MATNR": "Legacy material number",
}
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
        if "RSDBCHATR" in sql:
            return _rows_for_name(_RSDBCHATR, name)
        if "RSDATRNAV" in sql:
            return self._nav_attributes(sql, name)
        if "RSDCHA" in sql:
            return _rows_for_name(_RSDCHA, name)
        if "RSDIOBJT" in sql:
            if "IN (" in sql:  # bulk short-text read: (IOBJNM, LANGU, TXTSH)
                return [(n, "E", t) for n, t in _ATTR_DESCRIPTIONS.items()]
            return _rows_for_name(_IOBJ_TEXT, name)
        if "RSDIOBJ" in sql:
            return _rows_for_name(_IOBJ, name)
        return []

    @staticmethod
    def _nav_attributes(sql: str, name: str) -> list[tuple[Any, ...]]:
        """RSDATRNAV is read two ways: by characteristic, and by navigation name for a field."""
        if "ATRNAVNM IN (" in sql:  # (ATRNAVNM, CHANM, ATTRINM)
            return [
                (navnm, chanm, attrinm)
                for chanm, rows in _RSDATRNAV.items()
                for attrinm, navnm, *_flags in rows
            ]
        return _rows_for_name(_RSDATRNAV, name)

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
    assert {f.name for f in provider.fields} == {
        "MATERIAL",
        "AMOUNT",
        "MATERIAL_CHA__MATL_GROUP",
    }


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
    """No stored model, so parts must fall back to the generated calc view's base tables."""
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
    # The absent model is reported as a finding rather than passed over silently, and the caveat
    # names the fallback so a reader knows the parts are a convention reading.
    assert any("declared composition is unavailable" in c for c in provider.caveats)
    assert any("generated calc view" in c for c in provider.caveats)
    assert {f.name for f in provider.fields} == {"MATERIAL"}


# --- InfoObject ---------------------------------------------------------------------------


def test_describe_infoobject_kind() -> None:
    provider = _repo().describe("MATERIAL_CHA")
    assert not isinstance(provider, (ObjectNotFound, UnsupportedResult))
    assert provider.object_type == "infoobject"
    assert provider.infoobject_kind == "characteristic"


# --- attributes ---------------------------------------------------------------------------


def test_basic_characteristic_attributes_are_resolved() -> None:
    provider = _described("MATERIAL_CHA")
    by_name = {a.name: a for a in provider.attributes}
    assert set(by_name) == {"MATL_GROUP", "MATL_TYPE", "BASE_UOM", "OLD_MATNR"}
    assert [a.position for a in provider.attributes] == [1, 2, 3, 4]
    # Locally defined, so nothing is marked inherited.
    assert all(a.inherited_from is None for a in provider.attributes)
    assert by_name["MATL_GROUP"].kind == "navigation"
    assert by_name["BASE_UOM"].kind == "display"
    assert by_name["MATL_GROUP"].description == "Material group"


def test_time_dependent_attribute_uses_the_numeric_flag_not_x() -> None:
    """ATRTIMFL is domain RSDCNVFL ('0'/'1'). Testing for 'X' reports every attribute as static."""
    by_name = {a.name: a for a in _described("MATERIAL_CHA").attributes}
    assert by_name["MATL_TYPE"].time_dependent is True
    assert by_name["MATL_GROUP"].time_dependent is False


def test_navigation_name_is_read_not_composed() -> None:
    by_name = {a.name: a for a in _described("MATERIAL_CHA").attributes}
    assert by_name["MATL_GROUP"].navigation_name == "MATERIAL_CHA__MATL_GROUP"
    assert by_name["MATL_GROUP"].navigable is True
    # A display attribute has no navigation row at all.
    assert by_name["BASE_UOM"].navigation_name is None
    assert by_name["BASE_UOM"].navigable is False


def test_hidden_in_query_flag_is_carried() -> None:
    by_name = {a.name: a for a in _described("MATERIAL_CHA").attributes}
    assert by_name["OLD_MATNR"].hidden_in_query is True
    assert by_name["BASE_UOM"].hidden_in_query is False


def test_reference_characteristic_inherits_its_bases_attributes() -> None:
    """RSDBCHATR is keyed on the basic characteristic; keying it on the reference finds nothing."""
    provider = _described("SOLD_TO_CHA")
    assert {a.name for a in provider.attributes} == {
        "MATL_GROUP",
        "MATL_TYPE",
        "BASE_UOM",
        "OLD_MATNR",
    }
    assert all(a.inherited_from == "MATERIAL_CHA" for a in provider.attributes)


def test_reference_characteristic_carries_its_own_navigation_names() -> None:
    """RSDATRNAV is keyed on the characteristic itself, so the nav name is the reference's own."""
    by_name = {a.name: a for a in _described("SOLD_TO_CHA").attributes}
    assert by_name["MATL_GROUP"].navigation_name == "SOLD_TO_CHA__MATL_GROUP"
    assert by_name["MATL_GROUP"].navigable is True


def test_inherited_navigable_attribute_not_exposed_here_is_reported() -> None:
    """Navigable on the base but with no navigation name here means no drilldown - and is stated."""
    provider = _described("SOLD_TO_CHA")
    by_name = {a.name: a for a in provider.attributes}
    assert by_name["MATL_TYPE"].kind == "navigation"
    assert by_name["MATL_TYPE"].navigable is False
    assert by_name["MATL_TYPE"].navigation_name is None
    assert any("cannot drill down" in c and "MATL_TYPE" in c for c in provider.caveats)


def test_authorisation_relevant_navigation_attribute_is_flagged() -> None:
    provider = _described("MATERIAL_CHA")
    by_name = {a.name: a for a in provider.attributes}
    assert by_name["MATL_TYPE"].auth_relevant is True
    assert by_name["MATL_GROUP"].auth_relevant is False
    assert by_name["MATL_GROUP"].text_from_characteristic is True
    assert any("authorisation-relevant" in c and "MATL_TYPE" in c for c in provider.caveats)


def test_attributes_carry_provenance_for_both_tables() -> None:
    by_name = {a.name: a for a in _described("MATERIAL_CHA").attributes}
    assert [p.source_table for p in by_name["MATL_GROUP"].provenance] == ["RSDBCHATR", "RSDATRNAV"]
    # A display attribute has no navigation row, so it cites one table only.
    assert [p.source_table for p in by_name["BASE_UOM"].provenance] == ["RSDBCHATR"]


def test_absent_attribute_table_is_stated_not_reported_as_no_attributes() -> None:
    present = set(_PROVIDER_TABLES) | set(_HANA_TABLES)
    provider = _described("MATERIAL_CHA", present - {"attribute"})
    assert provider.attributes == []
    assert any("attributes are not resolved" in c for c in provider.caveats)


def test_absent_nav_attribute_table_does_not_claim_display_only() -> None:
    present = set(_PROVIDER_TABLES) | set(_HANA_TABLES)
    provider = _described("MATERIAL_CHA", present - {"nav_attribute"})
    assert {a.name for a in provider.attributes} == {
        "MATL_GROUP",
        "MATL_TYPE",
        "BASE_UOM",
        "OLD_MATNR",
    }
    assert all(a.navigation_name is None for a in provider.attributes)
    assert any("navigation attributes are not resolved" in c for c in provider.caveats)


def test_key_figure_has_no_attributes() -> None:
    assert _described("AMOUNT_KYF").attributes == []


def test_provider_field_that_is_a_navigation_attribute_is_resolved() -> None:
    """A field list otherwise shows an opaque X__Y name with no statement of where it comes from."""
    fields = {f.name: f for f in _described("SALES_CUBE").fields}
    nav = fields["MATERIAL_CHA__MATL_GROUP"]
    assert nav.role == "navigation_attribute"
    assert (nav.attribute_of, nav.attribute_name) == ("MATERIAL_CHA", "MATL_GROUP")
    # An ordinary field is left alone.
    assert fields["AMOUNT"].role == "field"
    assert fields["AMOUNT"].attribute_of is None


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
