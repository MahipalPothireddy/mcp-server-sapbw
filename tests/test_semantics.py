"""Business areas: BW's InfoArea grouping, resolved without inventing anything.

The point of the subsystem is that BW already holds the answer to "what is this object for" and it
was being read and discarded. The point of these tests is that resolving it does not start guessing:
an object with no InfoArea stays unassigned, and an area code the hierarchy cannot place is reported
as unresolved rather than dropped or renamed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.semantics import SemanticsRepository

_TABLES = {
    "info_area": "RSDAREA",
    "info_area_text": "RSDAREAT",
    "dso_header": "RSDODSO",
    "adso_header": "RSOADSO",
    "cube_header": "RSDCUBE",
    "composite_header": "RSOHCPR",
}


def capability(*, absent: set[str] | None = None) -> CapabilityRecord:
    missing = absent or set()
    return CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema="SAPHANADB",
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=None if logical in missing else physical,
                present=logical not in missing,
                probe="absent" if logical in missing else "present",
            )
            for logical, physical in _TABLES.items()
        },
        discovered_at=datetime.now(UTC),
    )


class Conn:
    """Answers the three read shapes: hierarchy, texts, and grouped provider counts."""

    def __init__(
        self,
        hierarchy: list[tuple[str, str]],
        texts: list[tuple[str, str]],
        counts: dict[str, list[tuple[Any, int]]],
    ) -> None:
        self.hierarchy = hierarchy
        self.texts = texts
        self.counts = counts
        self.queries: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append(sql)
        if "RSDAREAT" in sql:
            return list(self.texts)
        if "RSDAREA" in sql:
            return list(self.hierarchy)
        for physical, rows in self.counts.items():
            if physical in sql:
                return list(rows)
        return []


def repo(conn: Conn, *, absent: set[str] | None = None) -> SemanticsRepository:
    return SemanticsRepository(conn, capability(absent=absent))


def simple() -> Conn:
    return Conn(
        hierarchy=[("SALES", ""), ("SALES_ORD", "SALES"), ("FIN", "")],
        texts=[("SALES", "Sales"), ("SALES_ORD", "Sales Orders"), ("FIN", "Finance")],
        counts={
            "RSDODSO": [("SALES_ORD", 3)],
            "RSOADSO": [("SALES_ORD", 2), ("FIN", 4)],
            "RSDCUBE": [],
            "RSOHCPR": [],
        },
    )


# --- resolution ---------------------------------------------------------------------------


def test_an_area_code_resolves_to_a_name() -> None:
    """The gap this closes: info_area reached callers as an opaque code."""
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    by_code = {a.area: a for a in result.areas}
    assert by_code["SALES_ORD"].name == "Sales Orders"
    assert by_code["FIN"].name == "Finance"


def test_the_hierarchy_becomes_a_root_to_leaf_path() -> None:
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    child = next(a for a in result.areas if a.area == "SALES_ORD")
    assert child.parent == "SALES"
    assert child.path == ["SALES", "SALES_ORD"]
    assert child.depth == 1
    root = next(a for a in result.areas if a.area == "SALES")
    assert root.parent is None
    assert root.path == ["SALES"]
    assert root.depth == 0


def test_providers_are_counted_by_type_per_area() -> None:
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    sales_ord = next(a for a in result.areas if a.area == "SALES_ORD")
    assert sales_ord.provider_counts == {"adso": 2, "dso": 3}
    assert sales_ord.provider_total == 5


def test_areas_are_ordered_by_weight_so_the_biggest_reads_first() -> None:
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert [a.area for a in result.areas][:2] == ["SALES_ORD", "FIN"]


def test_every_area_carries_provenance_and_a_declared_basis() -> None:
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    for area in result.areas:
        assert area.basis == "declared_infoarea"
        assert area.provenance and area.provenance[0].source_table == "RSDAREA"


# --- what it refuses to guess -------------------------------------------------------------


def test_a_provider_with_no_area_is_counted_not_attributed() -> None:
    """No naming inference: a wrong functional attribution routes a review to the wrong team."""
    conn = Conn(
        hierarchy=[("SALES", "")],
        texts=[("SALES", "Sales")],
        counts={"RSDODSO": [("", 7), ("SALES", 1)], "RSOADSO": [(None, 2)]},
    )
    result = repo(conn).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert result.unassigned_providers == 9
    assert all(a.area for a in result.areas)
    assert any("no business area is inferred" in c for c in result.caveats)


def test_an_area_the_hierarchy_cannot_place_is_reported_unresolved() -> None:
    """A missing name is not a missing assignment, so it must not be dropped."""
    conn = Conn(
        hierarchy=[("SALES", "")],
        texts=[("SALES", "Sales")],
        counts={"RSDODSO": [("GHOST", 4), ("SALES", 1)]},
    )
    result = repo(conn).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert result.unresolved_areas == ["GHOST"]
    # It is still reported as an area with its providers - dropping it would lose 4 objects.
    assert next(a for a in result.areas if a.area == "GHOST").provider_total == 4
    assert any("not a missing assignment" in c for c in result.caveats)


def test_an_area_with_nothing_assigned_is_named_rather_than_hidden() -> None:
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert "SALES" in result.empty_areas


def test_a_missing_text_leaves_the_name_empty_rather_than_echoing_the_code() -> None:
    """Echoing the code would make an undocumented area look documented."""
    conn = Conn(hierarchy=[("SALES", "")], texts=[], counts={"RSDODSO": [("SALES", 1)]})
    result = repo(conn).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert next(a for a in result.areas if a.area == "SALES").name is None


def test_a_cycle_in_the_hierarchy_does_not_loop() -> None:
    conn = Conn(
        hierarchy=[("A", "B"), ("B", "A")],
        texts=[],
        counts={"RSDODSO": [("A", 1)]},
    )
    result = repo(conn).business_areas()
    assert not isinstance(result, UnsupportedResult)
    path = next(a for a in result.areas if a.area == "A").path
    assert len(path) <= 3
    assert len(set(path)) == len(path)


# --- capability gating --------------------------------------------------------------------


def test_an_absent_hierarchy_table_is_unsupported_not_empty() -> None:
    result = repo(simple(), absent={"info_area"}).business_areas()
    assert isinstance(result, UnsupportedResult)
    assert "RSDAREA" in result.missing or "info_area" in result.missing


def test_absent_texts_degrade_to_codes_and_say_so() -> None:
    """The grouping still works without names; only the labels are gone."""
    result = repo(simple(), absent={"info_area_text"}).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert result.areas
    assert all(a.name is None for a in result.areas)
    assert any("without a name" in c for c in result.caveats)


def test_an_absent_provider_family_is_simply_not_counted() -> None:
    result = repo(simple(), absent={"cube_header", "composite_header"}).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert all("cube" not in a.provider_counts for a in result.areas)


def test_paging_reports_the_full_total() -> None:
    result = repo(simple()).business_areas(limit=1, offset=0)
    assert not isinstance(result, UnsupportedResult)
    assert len(result.areas) == 1
    assert result.total_count == 3
    assert result.limit == 1


def test_the_reply_states_that_an_area_is_a_modelling_grouping() -> None:
    """It reflects how the landscape was built, not who owns the data."""
    result = repo(simple()).business_areas()
    assert not isinstance(result, UnsupportedResult)
    assert any("modelling grouping" in c for c in result.caveats)
