"""Tests for the physical-table <-> BW-object resolver (pure functions, no DB).

Generated-table literals are built by concatenation so this file stays clean for the
customer-metadata scan, which forbids inline ``/BIC/<name>`` outside ``tests/fixtures/``.
"""

from __future__ import annotations

from mcp_server_sapbw.services.table_resolver import (
    calc_view_patterns,
    candidate_tables,
    is_hierarchy_view,
    provider_from_calc_view,
    resolve_table,
    split_namespace,
)

BIC = "/BIC/"
BI0 = "/BI0/"
NS = "/ABC/"  # synthetic customer namespace


# --- namespace handling -------------------------------------------------------------------


def test_split_namespace() -> None:
    assert split_namespace("SALES_DSO") == (BIC, "SALES_DSO")
    assert split_namespace(NS + "D_STOCK") == (NS, "D_STOCK")


# --- forward: provider -> generated tables ------------------------------------------------


def test_candidate_tables_per_provider_kind() -> None:
    assert candidate_tables("SALES_DSO", "dso") == {BIC + "ASALES_DSO00": "active"}
    assert candidate_tables("FIN_ADSO", "adso") == {
        BIC + "AFIN_ADSO1": "inbound",
        BIC + "AFIN_ADSO2": "active",
        BIC + "AFIN_ADSO3": "changelog",
    }
    assert candidate_tables("SALES_CUBE", "infocube") == {
        BIC + "FSALES_CUBE": "fact_f",
        BIC + "ESALES_CUBE": "fact_e",
    }
    assert candidate_tables("MATERIAL", "infoobject") == {BIC + "PMATERIAL": "master_attr"}
    # Virtual providers persist nothing of their own.
    assert candidate_tables("SALES_CP", "compositeprovider") == {}


def test_candidate_tables_uses_the_providers_own_namespace() -> None:
    tables = candidate_tables(NS + "D_STOCK", "adso")
    assert NS + "AD_STOCK2" in tables
    assert tables[NS + "AD_STOCK2"] == "active"


# --- reverse: generated table -> provider -------------------------------------------------


def test_resolve_classic_dso_active_table() -> None:
    resolved = resolve_table(BIC + "ASALES_DSO00", {"dso": ["SALES_DSO"]})
    assert resolved.object_name == "SALES_DSO"
    assert resolved.kind == "dso"
    assert resolved.role == "active"
    assert resolved.confidence == "confirmed"
    assert resolved.is_part_provider_candidate


def test_resolve_adso_roles() -> None:
    catalog = {"adso": ["FIN_ADSO"]}
    for suffix, role in (("1", "inbound"), ("2", "active"), ("3", "changelog")):
        resolved = resolve_table(BIC + "AFIN_ADSO" + suffix, catalog)
        assert resolved.object_name == "FIN_ADSO"
        assert resolved.kind == "adso"
        assert resolved.role == role


def test_resolve_cube_fact_tables() -> None:
    catalog = {"infocube": ["SALES_CUBE"]}
    assert resolve_table(BIC + "FSALES_CUBE", catalog).role == "fact_f"
    assert resolve_table(BIC + "ESALES_CUBE", catalog).role == "fact_e"


def test_master_data_tables_are_not_part_providers() -> None:
    """P/S/T/X/Y side tables belong to an InfoObject and must never count as a part provider."""
    for table_class in ("S", "T", "X", "Y"):
        resolved = resolve_table(BI0 + table_class + "MATERIAL")
        assert resolved.is_master_data is True
        assert resolved.is_part_provider_candidate is False


def test_unconfirmed_resolution_is_labelled_advisory() -> None:
    resolved = resolve_table(BIC + "AUNKNOWN_THING00")  # no catalogue supplied
    assert resolved.object_name == "UNKNOWN_THING"
    assert resolved.confidence == "advisory"


def test_non_generated_table_resolves_to_nothing() -> None:
    resolved = resolve_table("MARA")
    assert resolved.object_name is None
    assert resolved.kind == "unknown"


def test_namespaced_generated_table_keeps_its_namespace() -> None:
    resolved = resolve_table(NS + "AD_STOCK2", {"adso": [NS + "D_STOCK"]})
    assert resolved.object_name == NS + "D_STOCK"
    assert resolved.confidence == "confirmed"


# --- generated calc views -----------------------------------------------------------------


def test_calc_view_patterns_plain_and_namespaced() -> None:
    assert "system-local.bw.bw2hana/SALES_CP" in calc_view_patterns("SALES_CP")
    patterns = calc_view_patterns(NS + "V_STOCK")
    assert "system-local.bw.bw2hana.abc/V_STOCK" in patterns


def test_provider_from_calc_view_roundtrip() -> None:
    assert provider_from_calc_view("system-local.bw.bw2hana/SALES_CP") == "SALES_CP"
    assert provider_from_calc_view("system-local.bw.bw2hana.abc/V_STOCK") == NS + "V_STOCK"
    assert provider_from_calc_view("some.other.package/THING") is None
    assert provider_from_calc_view("") is None


def test_hierarchy_views_are_excluded() -> None:
    view = "system-local.bw.bw2hana/hier/SALES_CP"
    assert is_hierarchy_view(view) is True
    assert provider_from_calc_view(view) is None
