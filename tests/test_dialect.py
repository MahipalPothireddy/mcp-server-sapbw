"""Tests for the SQL dialect builder (B1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mcp_server_sapbw.core.dialect import (
    LIKE_ESCAPE,
    DialectError,
    SqlDialect,
    like_term,
    needs_active_version,
    quote_ident,
)
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from tests.sqllike import matches_like


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema="SAPHANADB",
        hana_repo_style="sys_repo",
        discovered_at=datetime.now(UTC),
        tables={
            "rstran": TableStatus(
                logical_name="rstran", resolved_name="RSTRAN", present=True, schema_name="SAPHANADB"
            ),
            "chain": TableStatus(
                logical_name="chain",
                resolved_name="RSPCCHAIN",
                present=True,
                schema_name="SAPHANADB",
            ),
            "adso_header": TableStatus(logical_name="adso_header", present=False),
        },
    )


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("RSTRAN", True),
        ("RSTRANFIELD", True),
        ("RSDODSO", True),
        ("RSOADSO", True),
        ("RSZCOMPDIR", True),
        ("RSDS", True),
        ("RSPCCHAIN", False),
        ("TBTCO", False),
        ("DD02L", False),
        ("SYS", False),
    ],
)
def test_needs_active_version(table: str, expected: bool) -> None:
    assert needs_active_version(table) is expected


def test_quote_ident_escapes() -> None:
    assert quote_ident("RSTRAN") == '"RSTRAN"'
    assert quote_ident('we"ird') == '"we""ird"'


def test_build_select_qualifies_and_injects_active_version() -> None:
    dialect = SqlDialect(_capability())
    query = dialect.build_select(columns=["TRANID", "SOURCENAME"], from_logical="rstran")
    assert query.sql == 'SELECT TRANID, SOURCENAME FROM "SAPHANADB"."RSTRAN" WHERE OBJVERS = \'A\''


def test_build_select_no_active_version_for_unversioned_table() -> None:
    dialect = SqlDialect(_capability())
    query = dialect.build_select(columns=["CHAIN_ID"], from_logical="chain")
    assert "OBJVERS" not in query.sql
    assert query.sql == 'SELECT CHAIN_ID FROM "SAPHANADB"."RSPCCHAIN"'


def test_compare_versions_suppresses_active_version() -> None:
    dialect = SqlDialect(_capability())
    query = dialect.build_select(from_logical="rstran", compare_versions=True)
    assert "OBJVERS" not in query.sql


def test_where_and_params_carried() -> None:
    dialect = SqlDialect(_capability())
    query = dialect.build_select(
        columns=["TRANID"],
        from_logical="rstran",
        where=["SOURCENAME = ?"],
        params=["X"],
        order_by=["TRANID"],
    )
    assert query.sql == (
        'SELECT TRANID FROM "SAPHANADB"."RSTRAN" '
        "WHERE SOURCENAME = ? AND OBJVERS = 'A' ORDER BY TRANID"
    )
    assert query.parameters == ["X"]


def test_unavailable_table_raises() -> None:
    dialect = SqlDialect(_capability())
    with pytest.raises(DialectError):
        dialect.build_select(from_logical="adso_header")


def test_no_capability_uses_bare_quoted_name() -> None:
    dialect = SqlDialect()
    query = dialect.build_select(columns=["A"], from_logical="RSPCCHAIN")
    assert query.sql == 'SELECT A FROM "RSPCCHAIN"'


def test_paginate_appends_bound_limit_offset() -> None:
    dialect = SqlDialect(_capability())
    base = dialect.build_select(columns=["CHAIN_ID"], from_logical="chain")
    page = dialect.paginate(base, limit=50, offset=100)
    assert page.sql.endswith("LIMIT ? OFFSET ?")
    assert page.parameters == [50, 100]


def test_count_query_wraps() -> None:
    dialect = SqlDialect(_capability())
    base = dialect.build_select(columns=["CHAIN_ID"], from_logical="chain")
    count = dialect.count_query(base)
    assert count.sql == (
        'SELECT COUNT(*) AS TOTAL_COUNT FROM (SELECT CHAIN_ID FROM "SAPHANADB"."RSPCCHAIN")'
    )


# --- LIKE terms for caller-supplied name filters -----------------------------------------


def test_like_term_wraps_bare_pattern_and_upper_cases() -> None:
    term = like_term("  billing  ")
    assert term.value == "%BILLING%"
    assert term.escaped is False
    assert term.clause("TXTLG") == "TXTLG LIKE ?"  # nothing to escape -> no ESCAPE clause


def test_like_term_escapes_underscore_as_a_literal() -> None:
    """Regression: BW names are full of underscores; an unescaped '_' is a single-char wildcard."""
    term = like_term("SALES_D0")
    assert term.value == f"%SALES{LIKE_ESCAPE}_D0%"
    assert term.escaped is True
    assert term.clause("ODSOBJECT") == f"ODSOBJECT LIKE ? ESCAPE '{LIKE_ESCAPE}'"
    # substring match works, and the underscore does not match an arbitrary character
    assert matches_like(term.value, "SALES_D01")
    assert not matches_like(term.value, "SALESXD01")


def test_like_term_bare_pattern_is_a_substring_match() -> None:
    """Regression: a bare term used to be passed through, so it only matched a full name."""
    term = like_term("SALES_D0")
    assert matches_like(term.value, "PREFIX_SALES_D01_SUFFIX")


def test_like_term_passes_through_caller_wildcards() -> None:
    term = like_term("%sales\\_d0%")
    assert term.value == "%SALES\\_D0%"
    assert term.escaped is False  # caller authored the pattern; no ESCAPE clause is added
    assert term.clause("ADSONM") == "ADSONM LIKE ?"


def test_like_term_escapes_the_escape_character() -> None:
    term = like_term(f"A{LIKE_ESCAPE}B")
    assert term.value == f"%A{LIKE_ESCAPE * 2}B%"
    assert term.escaped is True
    assert matches_like(term.value, f"XA{LIKE_ESCAPE}BY")
