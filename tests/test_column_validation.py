"""Column existence: validated at runtime, or permissively skipped when unmeasured (D45).

Table existence had been validated since the first build; column existence never was. A column the
connected release does not have reached the driver and came back as ``invalid column name: X: line 1
col 18`` - accurate, and useless for telling a release difference from a bug in this server.

It was not a theoretical risk. ``bw_list_business_areas`` **raised on every call against BW 7.50**
because ``RSDAREA`` has no ``PARENT_AREA`` (D48), and two earlier instances -
``RSTRAN.EXPERTROUTINE`` and the guessed ``RSPCPROCESSLOG`` timestamps - were each found by a human
running SQL by hand.

The hard part is not detecting a missing column, it is **not breaking the 59 legitimate
non-identifier column expressions** the server already selects. Those are measured from the source,
and the cases below use the real shapes rather than invented ones.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mcp_server_sapbw.core.dialect import DialectError, SqlDialect
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.semantics import _PARENT_COLUMN, SemanticsRepository

SCHEMA = "TESTSCHEMA"


def _capability(columns: frozenset[str] | None) -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            "info_area": TableStatus(
                logical_name="info_area",
                resolved_name="RSDAREA",
                present=True,
                schema_name=SCHEMA,
                columns=columns if columns is not None else frozenset(),
            )
        },
    )


#: The columns BW 7.50 actually has on RSDAREA, read from DD03L. INFOAREA_P is the parent link;
#: PARENT_AREA is the name the server used and this release does not carry.
_REAL = frozenset(
    {
        "INFOAREA",
        "OBJVERS",
        "OBJSTAT",
        "CONTREL",
        "CONTTIMESTMP",
        "OWNER",
        "BWAPPL",
        "INFOAREA_P",
        "INFOAREA_C",
        "INFOAREA_N",
        "TSTPNM",
        "TIMESTMP",
    }
)


def test_a_column_this_release_lacks_is_refused_before_the_driver_sees_it() -> None:
    """The exact case that made bw_list_business_areas dead on 7.50."""
    dialect = SqlDialect(_capability(_REAL))
    with pytest.raises(DialectError) as exc:
        dialect.build_select(columns=["INFOAREA", "PARENT_AREA"], from_logical="info_area")
    message = str(exc.value)
    assert "PARENT_AREA" in message, "the missing column must be named"
    assert "RSDAREA" in message, "so must the table"
    assert "BW 7.50" in message, "and the release, since that is what decides the answer"
    assert "release difference" in message, (
        "the message has to say which KIND of problem this is - the driver's own wording was "
        "indistinguishable from a malformed query"
    )


def test_the_column_that_does_exist_builds_normally() -> None:
    """The D48 fix: INFOAREA_P is the real parent link, confirmed by two other columns."""
    dialect = SqlDialect(_capability(_REAL))
    query = dialect.build_select(columns=["INFOAREA", "INFOAREA_P"], from_logical="info_area")
    assert "INFOAREA_P" in query.sql
    assert "OBJVERS = 'A'" in query.sql, "RSDAREA is versioned; the filter must still be injected"


@pytest.mark.parametrize(
    "expression",
    [
        "COUNT(*)",
        "MAX(DATUM)",
        "MIN(DATUM)",
        "SUM(MEMORY_SIZE_IN_TOTAL)",
        "COUNT(DISTINCT LOG_ID)",
        "DISTINCT CHAIN_ID",
        "LENGTH(CDATA)",
        "MAX(TIMESTAMP_ANF)",
        "V.DOMNAME",
        "T.DDTEXT",
    ],
)
def test_expressions_are_passed_through_not_validated(expression: str) -> None:
    """59 of the server's selected "columns" are not column names, and all of them must survive.

    Every shape here was extracted from the source by an AST scan, not invented: aggregates, a
    ``DISTINCT`` prefix, a length call and table-qualified names from an aliased join. Validating
    these would break every aggregate read in the server, which is a far worse failure than the one
    being fixed.
    """
    dialect = SqlDialect(_capability(_REAL))
    query = dialect.build_select(columns=[expression], from_logical="info_area")
    assert expression in query.sql


def test_an_unmeasured_column_list_validates_nothing() -> None:
    """A release whose DD03L could not be read must behave exactly as it did before this existed.

    The alternative - refusing every read because the dictionary was unreadable - turns one missing
    grant into a server that answers nothing. Same reasoning as the existence probe, which also
    declines to be fatal.
    """
    dialect = SqlDialect(_capability(None))
    query = dialect.build_select(
        columns=["INFOAREA", "A_COLUMN_THAT_DOES_NOT_EXIST"], from_logical="info_area"
    )
    assert "A_COLUMN_THAT_DOES_NOT_EXIST" in query.sql

    status = _capability(None).table("info_area")
    assert status is not None
    assert status.columns_known is False
    assert status.has_column("ANYTHING") is True, "unmeasured must mean permissive, not forbidden"


def test_a_dialect_with_no_capability_record_validates_nothing() -> None:
    """Tests and offline paths build a dialect without a capability record; it must still work."""
    query = SqlDialect().build_select(columns=["WHATEVER"], from_logical="info_area")
    assert "WHATEVER" in query.sql


def test_lower_case_and_mixed_case_names_are_left_alone() -> None:
    """Only bare UPPER-CASE identifiers are treated as column names.

    A lower-case token in a column list is far more likely to be part of an expression or an alias
    than a BW column, and BW column names are upper case throughout. Guessing wrong in that
    direction would refuse a legitimate read, so the check declines to judge.
    """
    dialect = SqlDialect(_capability(_REAL))
    for token in ("infoarea", "AsAlias", "count(*) as n"):
        query = dialect.build_select(columns=[token], from_logical="info_area")
        assert token in query.sql


# --- the structured pre-flight, and the instance that motivated it ---------------------------


class _NoRows:
    """A connection that returns nothing, so only the gating decision is under test."""

    def execute_select(self, sql: str, parameters: object = None) -> list[tuple[object, ...]]:
        return []


def _semantics(columns: frozenset[str] | None) -> SemanticsRepository:
    record = _capability(columns)
    # The reader also consults the texts table and the provider catalogue; absent is fine here,
    # because what is being tested is whether it declines before building the hierarchy statement.
    return SemanticsRepository(_NoRows(), record, None)


def test_a_release_without_the_parent_link_gets_a_structured_result_not_an_exception() -> None:
    """D45's user-facing half: the hierarchy is the point of this answer, so its absence is news.

    Before this, a release lacking the parent column produced ``QueryError(260, 'invalid column
    name...')`` out of the driver. That is an exception on a legitimate question, and its wording
    invites the reader to suspect the server rather than the release.
    """
    without_parent = frozenset(_REAL - {"INFOAREA_P"})
    result = _semantics(without_parent).business_areas()
    assert isinstance(result, UnsupportedResult)
    detail = result.detail or ""
    assert "INFOAREA_P" in detail, "name the column that is missing"
    assert "release difference" in detail, "and say that it is not a grant or an absent object"
    assert any("INFOAREA_P" in m for m in result.missing)


def test_the_business_area_reader_asks_for_the_column_that_exists_on_7_50() -> None:
    """D48, pinned. ``PARENT_AREA`` does not exist on BW 7.50 and the reader used it, so this tool
    raised on every call against the reference system and had never returned an answer there.

    The replacement was verified rather than guessed: across 471 active areas there are 29 roots, no
    parent naming a nonexistent area, no cycles and a maximum depth of 6 - and two other columns
    agree, with ``INFOAREA_C`` pointing back on all 150 resolvable rows and ``INFOAREA_N`` sharing a
    parent on all 320.
    """
    assert _PARENT_COLUMN == "INFOAREA_P"
    assert _PARENT_COLUMN in _REAL
    assert "PARENT_AREA" not in _REAL
    # And the real read must go through without the dialect refusing it.
    result = _semantics(_REAL).business_areas()
    assert not hasattr(result, "code"), "a release that has the column must not be declined"


# --- D49: the active-version filter, decided by the column set rather than a name prefix ------


def _versioned(logical: str, physical: str, *, has_objvers: bool) -> CapabilityRecord:
    columns = {"CHAIN_ID", "TYPE", "VARIANTE"} | ({"OBJVERS"} if has_objvers else set())
    return CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical,
                present=True,
                schema_name=SCHEMA,
                columns=frozenset(columns),
            )
        },
    )


def test_a_versioned_table_the_name_prefix_misses_is_still_filtered() -> None:
    """D49. Mission Rule 6 was enforced by a name prefix, and RSPC is not one of them.

    Measured on production: ``RSPCCHAIN`` holds 3,616 active rows of 15,960, and the load
    closure's step categories summed to **exactly double** the step count - 78 against 39 - because
    every step was read once active and once revised. Worse than the count: **129 chain steps exist
    in the revised version and not the active one, 111 of them load steps**, so a closure could
    report loads a chain does not perform.
    """
    dialect = SqlDialect(_versioned("chain_edges", "RSPCCHAIN", has_objvers=True))
    query = dialect.build_select(columns=["CHAIN_ID", "TYPE"], from_logical="chain_edges")
    assert "OBJVERS = 'A'" in query.sql, "RSPCCHAIN carries OBJVERS, so it must be filtered"


def test_a_prefix_matching_table_without_the_column_is_not_filtered() -> None:
    """The other direction the prefix got wrong, and why the hand-maintained exclusion list existed.

    Injecting the condition into a table that lacks the column generates SQL naming a column that is
    not there - the same failure mode as D48, arriving from the opposite side.
    """
    dialect = SqlDialect(_versioned("adso_keyfields", "RSOADSOKEYFIELDS", has_objvers=False))
    query = dialect.build_select(columns=["CHAIN_ID"], from_logical="adso_keyfields")
    assert "OBJVERS" not in query.sql


def test_an_explicit_version_condition_is_not_duplicated() -> None:
    """25 call sites already add the condition by hand; they must stay exactly as they were.

    Matching on the column name rather than the exact condition also protects a reader that filters
    several versions deliberately - it would be wrong to bolt ``= 'A'`` underneath that.
    """
    dialect = SqlDialect(_versioned("chain_edges", "RSPCCHAIN", has_objvers=True))
    query = dialect.build_select(
        columns=["CHAIN_ID"], from_logical="chain_edges", where=["OBJVERS = 'A'"]
    )
    assert query.sql.count("OBJVERS") == 1

    ranged = dialect.build_select(
        columns=["CHAIN_ID"], from_logical="chain_edges", where=["OBJVERS IN ('A','M')"]
    )
    assert "OBJVERS = 'A'" not in ranged.sql, "a deliberate multi-version read must not be narrowed"


def test_compare_versions_still_wins() -> None:
    """The documented escape hatch for the one case that legitimately wants every version."""
    dialect = SqlDialect(_versioned("chain_edges", "RSPCCHAIN", has_objvers=True))
    query = dialect.build_select(
        columns=["CHAIN_ID"], from_logical="chain_edges", compare_versions=True
    )
    assert "OBJVERS" not in query.sql


def test_an_unmeasured_column_set_falls_back_to_the_name_prefix() -> None:
    """A release whose DD03L could not be read keeps exactly the old behaviour, right and wrong.

    Reverting to the prefix is not an endorsement of it - it is refusing to change behaviour on a
    system where the better signal is unavailable.
    """
    record = _versioned("chain_edges", "RSPCCHAIN", has_objvers=True)
    status = record.table("chain_edges")
    assert status is not None
    status.columns = frozenset()
    dialect = SqlDialect(record)
    query = dialect.build_select(columns=["CHAIN_ID"], from_logical="chain_edges")
    # RSPCCHAIN matches no prefix, so the fallback leaves it unfiltered - the pre-D49 behaviour.
    assert "OBJVERS" not in query.sql

    versioned = _versioned("cube_header", "RSDCUBE", has_objvers=True)
    cube = versioned.table("cube_header")
    assert cube is not None
    cube.columns = frozenset()
    assert "OBJVERS = 'A'" in SqlDialect(versioned).build_select(
        columns=["INFOCUBE"], from_logical="cube_header"
    ).sql, "RSDCUBE matches a prefix, so the fallback still filters it"
