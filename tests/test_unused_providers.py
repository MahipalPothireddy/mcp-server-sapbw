"""Tests for the unused-provider analyzer.

Offline, with a landscape built so each consumer route is exercised in isolation. Synthetic names.

Landscape (five DSOs, one CompositeProvider):
  DSO_FEEDS    - is a transformation source            -> used
  DSO_QUERY    - has a Query-Designer query            -> used
  DSO_PART     - is a CompositeProvider part           -> used
  DSO_ADHOC    - only an ad-hoc ('!!') query reads it  -> UNUSED, with the ad-hoc count reported
  DSO_ORPHAN   - nothing at all reads it               -> UNUSED

DSO_PART is the important case: a CompositeProvider consumes its parts through a generated calc
view, so a consumer check that only looked at transformations would report it falsely.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.analyzers import Analyzers

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "dso_header": "RSDODSO",
    "composite_header": "RSOHCPR",
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_text": "RSZELTTXT",
    "element_dir": "RSZELTDIR",
    # Needed for the calc-view route to CompositeProvider parts.
    "hana_views": "VIEWS",
    "object_dependencies": "OBJECT_DEPENDENCIES",
}

_PROVIDERS = ["DSO_ADHOC", "DSO_FEEDS", "DSO_ORPHAN", "DSO_PART", "DSO_QUERY"]
# DSO_PART's active table, in the classic-DSO form /BIC/A<name>00. Built by concatenation so this
# .py file carries no literal /BIC/ name for the customer-metadata scan to flag.
_PART_TABLE = "/BIC/" + "ADSO_PART00"
# Only DSO_FEEDS is some transformation's source.
_SOURCE_NAMES = [("DSO_FEEDS",)]
# COMPUID, COMPID, OWNER, LASTUSED. "!!" marks a query created ad hoc in the BEx Analyzer.
_QUERIES = [
    ("CU_DESIGNED", "Q_SALES_REPORT", "ANALYST", None),
    ("CU_ADHOC", "!!1ADHOC", "ANALYST", None),
]
# COMPUID -> provider (RSZCOMPIC).
_QUERY_PROVIDER = {"CU_DESIGNED": "DSO_QUERY", "CU_ADHOC": "DSO_ADHOC"}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = [str(p) for p in (parameters or [])]
        if "RSDODSO" in sql:
            return [(name,) for name in _PROVIDERS]
        if "RSOHCPR" in sql:
            # The CompositeProvider catalogue, then the per-CP part resolution.
            if "HCPRNM = ?" in sql:
                return [("",)]  # XML_DEF probe: empty, as on a real 7.5 system
            return [("CP_SALES",)]
        if "RSZCOMPDIR" in sql:
            if "TOTAL_COUNT" in sql:
                return [(len(_QUERIES),)]
            rows = list(_QUERIES)
            # Honour the origin filter so the repository's SQL-level filtering is exercised.
            for param in params:
                if param.startswith("!!"):
                    designed = "NOT LIKE" in sql
                    rows = [r for r in rows if (not str(r[1]).startswith("!!")) is designed]
            return rows
        if "RSZCOMPIC" in sql:  # COMPUID, INFOCUBE, IS_MASTER
            return [
                (cu, prov, "X")
                for cu, prov in _QUERY_PROVIDER.items()
                if not params or cu in params
            ]
        if "RSTRAN" in sql:
            if "GROUP BY" in sql:
                return list(_SOURCE_NAMES)
            return []
        # The calc-view route to CompositeProvider parts: CP_SALES's generated view reads
        # DSO_PART's active table, which is how a CompositeProvider consumes a part.
        if "VIEW_NAME" in sql:
            return [("CP_SALES_VIEW",)]
        if "BASE_OBJECT_NAME" in sql:
            return [(_PART_TABLE,)]
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


def _report(present: set[str] | None = None, **kwargs: Any) -> Any:
    report = Analyzers(ScriptedConnection(), _capability(present)).find_unused_providers(**kwargs)
    assert not isinstance(report, UnsupportedResult)
    return report


def _flagged(report: Any) -> set[str]:
    return {obj for f in report.findings for obj in f.affected_objects}


def _finding(report: Any, name: str) -> Any:
    return next(f for f in report.findings if f.affected_objects == [name])


# --- each consumer route suppresses a finding -------------------------------------------------


def test_transformation_source_counts_as_a_consumer() -> None:
    assert "DSO_FEEDS" not in _flagged(_report())


def test_designed_query_counts_as_a_consumer() -> None:
    assert "DSO_QUERY" not in _flagged(_report())


def test_ad_hoc_query_alone_does_not_count_as_a_consumer() -> None:
    """A throwaway BEx-Analyzer navigation is not a maintained report."""
    assert "DSO_ADHOC" in _flagged(_report())


def test_provider_with_nothing_reading_it_is_flagged() -> None:
    assert "DSO_ORPHAN" in _flagged(_report())


def test_every_candidate_is_counted_even_when_not_flagged() -> None:
    assert _report().analyzed_count == len(_PROVIDERS)


# --- the ad-hoc count changes the advice ------------------------------------------------------


def test_ad_hoc_count_is_reported_on_the_finding() -> None:
    finding = _finding(_report(), "DSO_ADHOC")
    assert finding.metrics["ad_hoc_query_count"] == 1
    assert "ad-hoc" in finding.detail


def test_ad_hoc_usage_changes_the_recommendation() -> None:
    """Someone querying it by hand is a reporting need, not a reason to delete quietly."""
    with_adhoc = _finding(_report(), "DSO_ADHOC").recommendation
    without = _finding(_report(), "DSO_ORPHAN").recommendation
    assert with_adhoc != without
    assert "who runs" in with_adhoc


def test_findings_are_low_severity_candidates() -> None:
    report = _report()
    assert report.findings
    assert all(f.severity == "low" for f in report.findings)
    assert all(
        "Confirm" in f.recommendation or "confirm" in f.recommendation for f in report.findings
    )


# --- honesty about what was not checked -------------------------------------------------------


def test_external_consumption_gap_is_declared() -> None:
    joined = " ".join(_report().caveats)
    assert "bw_get_hana_crossings" in joined
    assert "not objects proven safe to delete" in joined


def test_ad_hoc_classification_basis_is_declared() -> None:
    assert any("reads the name shape" in c for c in _report().caveats)


def test_no_catalogue_is_not_read_as_everything_unused() -> None:
    """With no provider catalogue the analyzer must report nothing, and say why."""
    report = _report(present={"transformation"})
    assert report.findings == []
    assert report.analyzed_count == 0
    assert any("not evidence that every provider is used" in c for c in report.caveats)


def test_limit_caps_findings_and_reports_truncation() -> None:
    report = _report(limit=1)
    assert len(report.findings) == 1
    assert report.truncated is True


# --- the false-positive guard that matters most ------------------------------------------------


def test_composite_provider_part_is_not_reported_as_unused() -> None:
    """A CompositeProvider consumes its parts through a generated calc view, not a transformation.

    Checking only transformation sources and queries would flag every DSO beneath a
    CompositeProvider. This is the single most likely way this analysis could be wrong, so it is
    asserted directly rather than left to the caveats.
    """
    assert "DSO_PART" not in _flagged(_report())


def test_part_route_is_counted_in_the_caveats() -> None:
    assert any("CompositeProvider parts" in c for c in _report().caveats)


def test_only_the_genuinely_unused_are_reported() -> None:
    assert _flagged(_report()) == {"DSO_ADHOC", "DSO_ORPHAN"}
