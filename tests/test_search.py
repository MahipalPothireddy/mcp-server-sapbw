"""Tests for the cross-object search repository (B4), offline against scripted fixtures.

Synthetic names only (no Z*/Y*, no /BIC/).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.search import SearchRepository

SCHEMA = "TESTSCHEMA"
_SEARCH_TABLES = {
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "dso_header": "RSDODSO",
    "dso_text": "RSDODSOT",
    "adso_header": "RSOADSO",
    "adso_text": "RSOADSOT",
    "cube_header": "RSDCUBE",
    "cube_text": "RSDCUBET",
    "composite_header": "RSOHCPR",
    "composite_text": "RSOHCPRT",
    "infoobject": "RSDIOBJ",
    "infoobject_text": "RSDIOBJT",
}

# --- synthetic landscape (name -> description) --------------------------------------------

_CHAINS = ["SALES_LOAD", "FIN_LOAD"]
_CHAIN_TEXT = {"SALES_LOAD": "Sales master data load chain"}
_DSO = ["SALES_DSO"]
_DSO_TEXT = {"SALES_DSO": "Sales orders detailed store"}
_ADSO = ["SALES_ADSO"]
_ADSO_TEXT = {"SALES_ADSO": "Sales advanced datastore object"}
_CUBE = {"SALES_CUBE": "B", "SALES_MP": "M"}  # name -> CUBETYPE
_CUBE_TEXT = {"SALES_CUBE": "Sales reporting cube data"}
_CP = ["SALES_CP"]
_CP_TEXT = {"SALES_CP": "Sales composite provider view"}
_IOBJ = ["SALES_KYF"]
_IOBJ_TEXT = {"SALES_KYF": "Sales key figure amount"}


def _term(like: str) -> str:
    return like.strip("%").upper()


def _match(like: str, value: str) -> bool:
    return _term(like) in value.upper()


def _names(items: list[str], like: str) -> list[tuple[Any, ...]]:
    return [(n,) for n in items if _match(like, n)]


def _descs(table: dict[str, str], like: str) -> list[tuple[Any, ...]]:
    return [(n, d) for n, d in table.items() if _match(like, d)]


class ScriptedConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Sequence[Any] | None]] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append((sql, parameters))
        params = list(parameters or [])
        like = str(params[0]) if params else "%%"
        if "RSPCCHAINT" in sql:
            return _descs(_CHAIN_TEXT, like)
        if "RSPCCHAINATTR" in sql:
            return _names(_CHAINS, like)
        if "RSDODSOT" in sql:
            return _descs(_DSO_TEXT, like)
        if "RSDODSO" in sql:
            return _names(_DSO, like)
        if "RSOADSOT" in sql:
            return _descs(_ADSO_TEXT, like)
        if "RSOADSO" in sql:
            return _names(_ADSO, like)
        if "RSDCUBET" in sql:
            return _descs(_CUBE_TEXT, like)
        if "RSDCUBE" in sql:
            if "IN (" in sql:  # _cube_types batch lookup
                return [(n, _CUBE[n]) for n in params if n in _CUBE]
            return [(n, t) for n, t in _CUBE.items() if _match(like, n)]
        if "RSOHCPRT" in sql:
            return _descs(_CP_TEXT, like)
        if "RSOHCPR" in sql:
            return _names(_CP, like)
        if "RSDIOBJT" in sql:
            return _descs(_IOBJ_TEXT, like)
        if "RSDIOBJ" in sql:
            return _names(_IOBJ, like)
        return []


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical,
                present=True,
                schema_name=SCHEMA,
            )
            for logical, physical in _SEARCH_TABLES.items()
        },
    )


def _repo() -> SearchRepository:
    return SearchRepository(ScriptedConnection(), _capability())


def test_search_by_name_spans_all_types() -> None:
    hits, total = _repo().search("SALES")
    by_type = {h.object_type: h for h in hits}
    assert {
        "chain",
        "dso",
        "adso",
        "infocube",
        "multiprovider",
        "compositeprovider",
        "infoobject",
    } <= set(by_type)
    # cube family is split by CUBETYPE
    assert by_type["infocube"].name == "SALES_CUBE"
    assert by_type["multiprovider"].name == "SALES_MP"
    assert total == len(hits)
    assert all(h.provenance for h in hits)


def test_name_match_takes_precedence_over_description() -> None:
    # SALES_DSO matches by name AND its description contains "SALES"; name must win.
    hits, _ = _repo().search("SALES")
    dso = next(h for h in hits if h.object_type == "dso")
    assert dso.matched_on == "name"


def test_description_only_match_resolves_cube_type() -> None:
    # "reporting" appears only in SALES_CUBE's description, not in any technical name.
    hits, total = _repo().search("reporting")
    assert total == 1
    hit = hits[0]
    assert hit.name == "SALES_CUBE"
    assert hit.object_type == "infocube"  # resolved via CUBETYPE batch lookup
    assert hit.matched_on == "description"
    assert hit.description_short == "Sales reporting cube data"
    assert not isinstance(hit.provenance, list)
    assert hit.provenance.source_table == "RSDCUBET"


def test_object_types_filter() -> None:
    hits, total = _repo().search("SALES", object_types=["dso"])
    assert total == 1
    assert hits[0].object_type == "dso"
    assert hits[0].name == "SALES_DSO"


def test_object_types_filter_multiprovider_only() -> None:
    hits, _ = _repo().search("SALES", object_types=["multiprovider"])
    assert {h.name for h in hits} == {"SALES_MP"}


def test_pagination() -> None:
    page1, total = _repo().search("SALES", limit=3, offset=0)
    page2, _ = _repo().search("SALES", limit=3, offset=3)
    assert len(page1) == 3
    assert total > 3
    assert {h.name for h in page1}.isdisjoint({h.name for h in page2})


def test_no_description_match_when_disabled() -> None:
    _hits, total = _repo().search("reporting", match_descriptions=False)
    assert total == 0
