"""Offline tests for the analysis-authorisation repository.

Synthetic names only. The behaviours worth pinning down are the ones where a plausible
implementation would be quietly wrong:

* listing returns *shape*, never values, so a landscape-wide question cannot leak a permission dump;
* ``:`` means aggregated access only, not no access — the decode most often got wrong;
* an unreadable assignment table yields ``None``, never ``0``, so unknown is not read as nobody;
* a characteristic flagged authorisation-relevant that nothing covers is surfaced, because that
  silently returns no data to every user without a catch-all;
* column names are resolved from DD03L, so a release with a different layout degrades rather than
  raising.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.security import SecurityRepository

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "auth_values": "RSECVAL",
    "auth_hierarchy": "RSECHIE",
    "auth_user": "RSECUSERAUTH",
    "auth_text": "RSECTXT",
    "characteristic": "RSDCHA",
    "dict_columns": "DD03L",
}

# The column layout DD03L reports. Deliberately exercised rather than assumed by the repository.
_COLUMNS = {
    "RSECVAL": ["AUTH", "IOBJNM", "SIGNCH", "OPTIONCH", "LOW", "HIGH"],
    "RSECHIE": ["AUTH", "IOBJNM", "HIENM", "NODENAME", "TLEVEL", "DATEFROM", "DATETO"],
    "RSECUSERAUTH": ["AUTH", "UNAME"],
    "RSECTXT": ["AUTH", "TXTLG"],
    "RSDCHA": ["CHANM", "AUTHRELFL"],
}

# AUTH, IOBJNM, SIGNCH, OPTIONCH, LOW, HIGH
# AUTH_REGION restricts two characteristics; AUTH_AGG grants aggregated access only; AUTH_STAR is a
# catch-all by value; AUTH_VAR resolves per user through a variable.
_VALUE_ROWS = [
    ("AUTH_REGION", "COST_CENTRE", "I", "BT", "1000", "1999"),
    ("AUTH_REGION", "COMPANY", "I", "EQ", "DE01", ""),
    ("AUTH_AGG", "COST_CENTRE", "I", "EQ", ":", ""),
    ("AUTH_STAR", "COST_CENTRE", "I", "EQ", "*", ""),
    ("AUTH_VAR", "COMPANY", "I", "EQ", "$USER_COMPANY", ""),
    ("0BI_ALL", "COST_CENTRE", "I", "EQ", "*", ""),
]
_TEXT_ROWS = [("AUTH_REGION", "Region restriction"), ("AUTH_AGG", "Aggregated only")]
_USER_ROWS = {
    "AUTH_REGION": [("ANALYST_ONE",), ("ANALYST_TWO",)],
    "0BI_ALL": [("POWER_USER",)],
}
# CHANM, AUTHRELFL — PROFIT_CENTRE is flagged relevant but no authorisation covers it.
_RELEVANT_ROWS = [("COST_CENTRE",), ("COMPANY",), ("PROFIT_CENTRE",)]


class ScriptedConnection:
    """Answers the repository's SQL from fixtures, recording what it was asked."""

    def __init__(self, *, unreadable: set[str] | None = None) -> None:
        self.unreadable = unreadable or set()
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        for table in self.unreadable:
            if table in sql:
                raise RuntimeError(f"not authorized to read {table}")

        if "DD03L" in sql:
            wanted = str(parameters[0]) if parameters else ""
            return [(column,) for column in _COLUMNS.get(wanted, [])]
        if "RSDCHA" in sql:
            assert "AUTHRELFL = 'X'" in sql
            return list(_RELEVANT_ROWS)
        if "RSECTXT" in sql:
            return list(_TEXT_ROWS)
        if "RSECUSERAUTH" in sql:
            if "COUNT(DISTINCT" in sql and "IN (" not in sql:
                return [(3,)]
            if "COUNT(DISTINCT" in sql:
                return [(auth, len(rows)) for auth, rows in _USER_ROWS.items()]
            wanted = str(parameters[0]) if parameters else ""
            return list(_USER_ROWS.get(wanted, []))
        if "RSECHIE" in sql:
            if "COUNT(*)" in sql:
                return [("AUTH_REGION", 2)]
            return [("COST_CENTRE", "HIER_CC", "NODE_A", "1", "20240101", "99991231")]
        if "RSECVAL" in sql:
            if "DISTINCT" in sql:
                return [(row[1],) for row in _VALUE_ROWS]
            # NB: pagination binds LIMIT/OFFSET, so a non-empty `parameters` does not imply a
            # filtered read. The WHERE clause is what distinguishes the two.
            if "AUTH = ?" in sql:
                wanted = str(parameters[0]) if parameters else ""
                return [row[1:] for row in _VALUE_ROWS if row[0] == wanted]
            return [(row[0], row[1], row[4], row[5]) for row in _VALUE_ROWS]  # shape scan
        return []


def _repo(
    *, omit: set[str] | None = None, unreadable: set[str] | None = None
) -> tuple[SecurityRepository, ScriptedConnection]:
    omit = omit or set()
    tables = {
        logical: TableStatus(
            logical_name=logical,
            resolved_name=physical,
            present=logical not in omit,
            schema_name=SCHEMA,
        )
        for logical, physical in _TABLES.items()
    }
    capability = CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables=tables,
    )
    connection = ScriptedConnection(unreadable=unreadable)
    return SecurityRepository(connection, capability), connection


# --- values stay out of the listing ---------------------------------------------------------


def test_listing_returns_shape_without_any_concrete_values() -> None:
    """A landscape-wide question must not place permission data in the caller's context."""
    repo, _ = _repo()
    result = repo.list_authorisations(limit=50)
    assert not isinstance(result, UnsupportedResult)
    summaries, total, truncated = result
    assert total == len(_VALUE_ROWS) - 1  # six rows across five distinct authorisations
    assert truncated is False

    serialised = "".join(s.model_dump_json() for s in summaries)
    for value in ("1000", "1999", "DE01", "$USER_COMPANY"):
        assert value not in serialised, f"concrete value {value} leaked into the listing"

    region = next(s for s in summaries if s.name == "AUTH_REGION")
    assert region.characteristics == ["COMPANY", "COST_CENTRE"]
    assert region.range_count == 2


def test_only_the_single_object_tool_returns_values() -> None:
    repo, _ = _repo()
    auth = repo.get_authorisation("AUTH_REGION")
    assert not isinstance(auth, UnsupportedResult)
    assert auth.contains_data_values is True
    ranges = {r.characteristic: r for r in auth.ranges}
    assert ranges["COST_CENTRE"].low == "1000"
    assert ranges["COST_CENTRE"].high == "1999"
    assert ranges["COST_CENTRE"].operator == "between"
    assert ranges["COMPANY"].sign == "include"


# --- decoding the values BW overloads --------------------------------------------------------


def test_colon_is_aggregated_access_not_no_access() -> None:
    """':' permits a total but not the rows behind it. Reading it as no access inverts the fact."""
    repo, _ = _repo()
    auth = repo.get_authorisation("AUTH_AGG")
    assert not isinstance(auth, UnsupportedResult)
    assert auth.ranges[0].special == "aggregation_only"
    assert auth.grants_everything is False
    assert any("aggregated access only" in c for c in auth.caveats)


def test_star_range_is_recognised_as_granting_everything() -> None:
    repo, _ = _repo()
    auth = repo.get_authorisation("AUTH_STAR")
    assert not isinstance(auth, UnsupportedResult)
    assert auth.ranges[0].special == "all"
    assert auth.grants_everything is True


def test_variable_driven_range_is_flagged_as_per_user() -> None:
    """A '$' value resolves per user at runtime; metadata cannot state the effective scope."""
    repo, _ = _repo()
    auth = repo.get_authorisation("AUTH_VAR")
    assert not isinstance(auth, UnsupportedResult)
    assert auth.ranges[0].is_variable is True
    assert any("resolves per user" in c for c in auth.caveats)

    listing = repo.list_authorisations(limit=50)
    assert not isinstance(listing, UnsupportedResult)
    var_auth = next(s for s in listing[0] if s.name == "AUTH_VAR")
    assert var_auth.variable_driven_characteristics == ["COMPANY"]


def test_catch_all_holder_is_reported_as_unrestricted() -> None:
    repo, _ = _repo()
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert "0BI_ALL" in overview.catch_all_authorisations
    assert "POWER_USER" in overview.unrestricted_users
    assert any("unrestricted" in c for c in overview.caveats)


def test_catch_all_is_not_labelled_generated_despite_its_prefix() -> None:
    """0BI_ALL carries the SAP prefix but is not a generated authorisation."""
    repo, _ = _repo()
    assert repo._origin("0BI_ALL") == "maintained"
    assert repo._origin("0BI_SOMETHING") == "generated"
    assert repo._origin("AUTH_REGION") == "maintained"


# --- coverage gaps ---------------------------------------------------------------------------


def test_auth_relevant_characteristic_with_no_authorisation_is_surfaced() -> None:
    """The live fault: every query touching it returns nothing to a user without a catch-all."""
    repo, _ = _repo()
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.uncovered_characteristics == ["PROFIT_CENTRE"]
    assert any("configuration fault" in c for c in overview.caveats)

    covered = {e.characteristic: e for e in overview.auth_relevant_characteristics}
    assert covered["COST_CENTRE"].covered is True
    assert covered["PROFIT_CENTRE"].covered is False


def test_query_exposure_reports_characteristics_not_people() -> None:
    repo, _ = _repo()
    exposure = repo.query_exposure(
        compuid="UID_ONE",
        compid="QRY_SALES",
        providers=["PROV_SALES"],
        characteristics=["COST_CENTRE", "CALMONTH"],
    )
    assert exposure.auth_relevant_characteristics == ["COST_CENTRE"]
    assert exposure.user_specific_result is True
    assert "CALMONTH" not in exposure.auth_relevant_characteristics
    assert not any("POWER_USER" in c for c in exposure.caveats)


def test_query_without_auth_relevant_characteristics_is_not_user_specific() -> None:
    repo, _ = _repo()
    exposure = repo.query_exposure(
        compuid="UID_TWO", compid="QRY_STOCK", providers=[], characteristics=["CALMONTH"]
    )
    assert exposure.auth_relevant_characteristics == []
    assert exposure.user_specific_result is False


# --- degradation: unknown must never render as clean ------------------------------------------


def test_unreadable_assignment_table_yields_none_not_zero() -> None:
    """ "Cannot see the assignment" and "nobody holds it" are different answers."""
    repo, _ = _repo(unreadable={"RSECUSERAUTH"})
    result = repo.list_authorisations(limit=50)
    assert not isinstance(result, UnsupportedResult)
    assert all(s.user_count is None for s in result[0])

    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.users_with_any_authorisation is None


def test_absent_assignment_table_is_called_out_in_caveats() -> None:
    repo, _ = _repo(omit={"auth_user"})
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.users_with_any_authorisation is None
    assert any("not readable" in c for c in overview.caveats)


def test_absent_rsecval_is_unsupported_not_an_empty_answer() -> None:
    """Silence must not read as safety: no table means unknown, not "no authorisations"."""
    repo, _ = _repo(omit={"auth_values"})
    assert isinstance(repo.list_authorisations(), UnsupportedResult)
    assert isinstance(repo.get_authorisation("AUTH_REGION"), UnsupportedResult)
    assert isinstance(repo.overview(), UnsupportedResult)


def test_unresolvable_columns_report_a_gap_rather_than_raising() -> None:
    """A release whose RSECVAL layout differs degrades to a documented gap."""
    repo, _ = _repo(unreadable={"DD03L"})
    result = repo.require_security()
    assert isinstance(result, UnsupportedResult)
    assert "does NOT mean no authorisations exist" in result.detail


def test_missing_authrelfl_reports_unknown_coverage_not_clean() -> None:
    repo, _ = _repo(unreadable={"RSDCHA"})
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.auth_relevant_characteristics == []
    assert any("not clean" in c for c in overview.caveats)


# --- the cache boundary ------------------------------------------------------------------------


def test_repository_states_that_nothing_here_is_cached() -> None:
    repo, _ = _repo()
    assert repo._cache is None
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert any("never cached" in c for c in overview.caveats)


def test_hierarchy_node_authorisation_is_read() -> None:
    repo, _ = _repo()
    auth = repo.get_authorisation("AUTH_REGION")
    assert not isinstance(auth, UnsupportedResult)
    assert auth.hierarchy_nodes[0].hierarchy == "HIER_CC"
    assert auth.hierarchy_nodes[0].node == "NODE_A"
    assert auth.hierarchy_nodes[0].validity_to is not None
