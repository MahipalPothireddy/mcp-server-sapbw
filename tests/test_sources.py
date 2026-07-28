"""Tests for source-system topology and the extractor-enhancement inventory.

Offline against a scripted landscape. Synthetic logical-system and DataSource names only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.sources import SourcesRepository

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
    "source_system": "RSBASIDOC",
    "extractor": "ROOSOURCE",
}

# LOGSYS -> number of DataSources extracting from it (from RSDS).
_USAGE = [("ERPCLNT100", 40), ("BWSELF", 6), ("FILESYS", 2), ("GHOSTCLNT", 1)]
# Registry rows: SLOGSYS, SRCTYPE, OBJSTAT. GHOSTCLNT is deliberately absent (unregistered).
_REGISTRY = [
    ("ERPCLNT100", "3", "ACT"),  # dictionary-documented
    ("BWSELF", "M", "ACT"),  # advisory: not in the domain
    ("FILESYS", "F", "ACT"),  # dictionary-documented
]
# Enhancement inventory
_FIELD_COUNTS = [("DS_SALES", 7), ("DS_FIN", 2)]
_FIELDS = [
    ("DS_SALES", "ZZ_A"),
    ("DS_SALES", "ZZ_B"),
    ("DS_FIN", "ZZ_C"),
]
_DS_DETAIL = [
    ("DS_SALES", "ERPCLNT100", "D", "ADD", "SD"),  # DATASOURCE, LOGSYS, TYPE, DELTA, APPLNM
    ("DS_FIN", "ERPCLNT100", "M", "", "FI"),
]
_EXTRACTORS = [
    ("DS_SALES", "ADD", "ZEXT", "F1"),  # OLTPSOURCE, DELTA, EXTRACTOR, EXMETHOD
]


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "RSBASIDOC" in sql:
            return list(_REGISTRY)
        if "ROOSOURCE" in sql:
            return list(_EXTRACTORS)
        if "RSDSSEGFD" in sql:
            if "TOTAL_COUNT" in sql:
                return [(9,)]
            if "FIELDNM" in sql and "COUNT(*)" not in sql:
                return list(_FIELDS)
            return list(_FIELD_COUNTS)
        if "RSDS" in sql:
            if "COUNT(DISTINCT DATASOURCE)" in sql:
                return list(_USAGE)
            return list(_DS_DETAIL)
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


def _repo(present: set[str] | None = None) -> SourcesRepository:
    return SourcesRepository(ScriptedConnection(), _capability(present))


# --- topology -----------------------------------------------------------------------------


def test_topology_lists_systems_by_datasource_usage() -> None:
    topology = _repo().get_topology()
    assert not isinstance(topology, UnsupportedResult)
    assert [s.logical_system for s in topology.systems] == [
        "ERPCLNT100",
        "BWSELF",
        "FILESYS",
        "GHOSTCLNT",
    ]
    assert topology.total_datasources == 49
    assert topology.systems[0].datasource_count == 40


def test_dictionary_backed_and_advisory_decodes_are_distinguished() -> None:
    """A code the domain documents must not carry the same authority as one it omits."""
    topology = _repo().get_topology()
    assert not isinstance(topology, UnsupportedResult)
    by_name = {s.logical_system: s for s in topology.systems}
    erp = by_name["ERPCLNT100"]
    assert erp.kind == "sap_r3"
    assert erp.kind_confidence == "dictionary"  # SRCTYPE '3' is in the domain
    self_sys = by_name["BWSELF"]
    assert self_sys.kind == "self"
    assert self_sys.kind_confidence == "advisory"  # 'M' is not in the domain
    assert any("does not document" in c for c in topology.caveats)


def test_unregistered_logical_system_is_flagged() -> None:
    """A DataSource pointing at a system the registry doesn't know is the BDLS-gap signature."""
    topology = _repo().get_topology()
    assert not isinstance(topology, UnsupportedResult)
    ghost = next(s for s in topology.systems if s.logical_system == "GHOSTCLNT")
    assert ghost.registered is False
    assert ghost.kind == "unknown"
    assert topology.unregistered_count == 1
    assert any("BDLS" in c for c in topology.caveats)


def test_registry_absent_is_declared() -> None:
    topology = _repo(present={"datasource", "datasource_field"}).get_topology()
    assert not isinstance(topology, UnsupportedResult)
    assert all(s.registered is False for s in topology.systems)
    assert any("registry (RSBASIDOC) is unavailable" in c for c in topology.caveats)


def test_topology_unsupported_without_datasource_table() -> None:
    assert isinstance(_repo(present=set()).get_topology(), UnsupportedResult)


# --- enhancement inventory ----------------------------------------------------------------


def test_enhancements_ranked_with_delta_and_extractor_detail() -> None:
    inventory = _repo().enhancement_inventory()
    assert not isinstance(inventory, UnsupportedResult)
    assert [e.datasource for e in inventory.enhanced] == ["DS_SALES", "DS_FIN"]  # most fields first
    sales = inventory.enhanced[0]
    assert sales.customer_field_count == 7
    assert sales.customer_fields == ["ZZ_A", "ZZ_B"]
    assert sales.logical_system == "ERPCLNT100"
    assert sales.request_type == "transaction data"  # RSDS.TYPE 'D'
    assert sales.delta_method == "ADD"
    assert sales.extraction_method == "F1"
    assert sales.extractor == "ZEXT"
    assert inventory.enhanced_count == 2
    assert inventory.total_datasources == 9


def test_enhancement_names_the_connector_and_states_its_limits() -> None:
    inventory = _repo().enhancement_inventory()
    assert not isinstance(inventory, UnsupportedResult)
    assert inventory.connector_required == "ECC"
    assert any("do not reveal what the exit code does" in c for c in inventory.caveats)
    assert any("not reachable over the BW database connection" in c for c in inventory.caveats)


def test_enhancement_truncation_is_reported() -> None:
    inventory = _repo().enhancement_inventory(limit=1)
    assert not isinstance(inventory, UnsupportedResult)
    assert inventory.truncated is True
    assert len(inventory.enhanced) == 1
    assert inventory.enhanced_count == 2  # the full count is still reported


def test_missing_extractor_table_is_declared() -> None:
    inventory = _repo(present={"datasource", "datasource_field"}).enhancement_inventory()
    assert not isinstance(inventory, UnsupportedResult)
    assert inventory.enhanced[0].extractor is None
    assert any("ROOSOURCE is unavailable" in c for c in inventory.caveats)
