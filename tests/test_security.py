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
from mcp_server_sapbw.repositories.security import _OPERATORS, SecurityRepository

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
    "RSECTXT": ["AUTH", "TXTLG", "TXTSH", "LANGU"],
    "RSDCHA": ["CHANM", "AUTHRELFL"],
}

# **What BW 7.50 actually ships** — measured, not supposed (D53). Every RSEC* column except those of
# RSECUSERAUTH carries a TCT prefix, so the layout above exists on no system reachable from here.
# Both are exercised, because the point of resolving columns by role is that neither layout is
# privileged: a release naming them a third way should be one entry away from working.
_TCT_COLUMNS = {
    "RSECVAL": ["TCTAUTH", "OBJVERS", "TCTIOBJNM", "TCTSIGN", "TCTOPTION", "TCTLOW", "TCTHIGH"],
    "RSECHIE": [
        "TCTAUTH",
        "OBJVERS",
        "TCTIOBJNM",
        "TCTHIENM",
        "TCTNODE",
        "TCTATYPE",
        "TCTTLEVEL",
        "TCTHIEDATE",
        "TCTHDATE",
    ],
    "RSECUSERAUTH": ["UNAME", "AUTH"],
    "RSECTXT": ["TCTAUTH", "OBJVERS", "TCTLANGU", "TCTTXTSH", "TCTTXTMD", "TCTTXTLG"],
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
# AUTH, TXTLG, TXTSH, LANGU. Several languages per authorisation, as a real system has (nine on the
# reference system), so "which one is returned" is a decision the reader has to make rather than
# something the row order decides for it (D55). German is listed first and English last on purpose:
# a reader that keeps the last row wins by accident, and one that keeps the first is wrong.
#
# The last two rows are the cases measured on the reference system. 0BI_ALL has an empty long text
# in all nine languages and only a short text, so a reader resolving a single text column loses the
# description of the most important authorisation on the system. And that short text is the literal
# string "0BI_ALL" - a name echo, which is not a description and should not be returned as one.
_TEXT_ROWS = [
    ("AUTH_REGION", "Regionsbeschraenkung", "Region kurz", "D"),
    ("AUTH_REGION", "Restriction de region", "Region court", "F"),
    ("AUTH_REGION", "Region restriction", "Region short", "E"),
    ("AUTH_AGG", "Nur aggregiert", "Aggregiert", "D"),
    ("AUTH_AGG", "Aggregated only", "Aggregated", "E"),
    # No English row: a system holding only one other language should still get a description.
    ("AUTH_STAR", "Alles", "Alles kurz", "D"),
    # Long text blank, short text usable.
    ("AUTH_VAR", "", "Per-user company", "E"),
    # Long text blank, short text is just the technical name.
    ("0BI_ALL", "", "0BI_ALL", "E"),
]
# Mirrors _TEXT_COLUMNS["text"] in the repository: which physical columns can hold a text, in the
# order the reader prefers them, and which of the fixture's two texts each one carries.
_TEXT_PREFERENCE = ("TCTTXTLG", "TCTTXTMD", "TCTTXTSH", "TXTLG", "TXTSH", "TEXT")
_TEXT_ROLE = {
    "TCTTXTLG": "long",
    "TCTTXTMD": "medium",
    "TCTTXTSH": "short",
    "TXTLG": "long",
    "TXTSH": "short",
    "TEXT": "long",
}

_USER_ROWS = {
    "AUTH_REGION": [("ANALYST_ONE",), ("ANALYST_TWO",)],
    "0BI_ALL": [("POWER_USER",)],
}
# CHANM, AUTHRELFL — PROFIT_CENTRE is flagged relevant but no authorisation covers it.
_RELEVANT_ROWS = [("COST_CENTRE",), ("COMPANY",), ("PROFIT_CENTRE",)]


class ScriptedConnection:
    """Answers the repository's SQL from fixtures, recording what it was asked."""

    def __init__(
        self,
        *,
        unreadable: set[str] | None = None,
        layout: dict[str, list[str]] | None = None,
        assignments: dict[str, list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self.unreadable = unreadable or set()
        self.layout = layout if layout is not None else _COLUMNS
        self.assignments = _USER_ROWS if assignments is None else assignments
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
            return [(column,) for column in self.layout.get(wanted, [])]
        if "RSDCHA" in sql:
            assert "AUTHRELFL = 'X'" in sql
            return list(_RELEVANT_ROWS)
        if "RSECTXT" in sql:
            # The reader selects *every* text column the layout exposes, in preference order, plus
            # the language column if there is one. A fixed-width row would therefore feed the
            # language value in as a text candidate under the TCT layout, which has three text
            # columns rather than two, so the shape is derived from the layout instead.
            names = self.layout.get("RSECTXT", [])
            text_columns = [c for c in _TEXT_PREFERENCE if c in names]
            rows: list[tuple[Any, ...]] = []
            for auth, long_text, short_text, langu in _TEXT_ROWS:
                source = {"long": long_text, "short": short_text, "medium": ""}
                row: tuple[Any, ...] = (
                    auth,
                    *(source[_TEXT_ROLE[column]] for column in text_columns),
                )
                if any(c in names for c in ("TCTLANGU", "LANGU", "SPRAS")):
                    row = (*row, langu)
                rows.append(row)
            return rows
        if "RSECUSERAUTH" in sql:
            # The existence probe (D54): the repository has to tell an empty assignment table from
            # an unreadable one, so it counts before it reads.
            if "COUNT(*)" in sql:
                return [(sum(len(rows) for rows in self.assignments.values()),)]
            if "COUNT(DISTINCT" in sql and "IN (" not in sql:
                return [(3,)]
            if "COUNT(DISTINCT" in sql:
                return [(auth, len(rows)) for auth, rows in self.assignments.items()]
            wanted = str(parameters[0]) if parameters else ""
            return list(self.assignments.get(wanted, []))
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
    *,
    omit: set[str] | None = None,
    unreadable: set[str] | None = None,
    layout: dict[str, list[str]] | None = None,
    assignments: dict[str, list[tuple[Any, ...]]] | None = None,
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
    connection = ScriptedConnection(
        unreadable=unreadable, layout=layout, assignments=assignments
    )
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


# --- the layout BW actually ships (D53) -------------------------------------------------------


def test_the_real_bw_750_column_layout_is_read_rather_than_reported_unsupported() -> None:
    """The regression that matters most in this file: on BW 7.50 this subsystem was entirely dead.

    Every RSEC* column carries a TCT prefix — TCTAUTH, TCTIOBJNM, TCTLOW — and the candidate lists
    named only the unprefixed forms. require_security() then failed to resolve the authorisation and
    characteristic roles and returned "unsupported on this release", so all three security tools
    answered that against tables holding 443 active value rows across 296 authorisations. Nothing
    raised and no test failed, because returning an unsupported result is exactly what the code is
    meant to do when a column is genuinely missing. The premise was wrong, not the logic.

    Asserted against the same expectations as the unprefixed layout, because the whole point of
    resolving by role is that the answer must not depend on which names a release uses.
    """
    repo, _ = _repo(layout=_TCT_COLUMNS)
    assert repo.require_security() is None

    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, total, _ = listed
    assert total == len({row[0] for row in _VALUE_ROWS})
    region = next(s for s in summaries if s.name == "AUTH_REGION")
    assert region.characteristics == ["COMPANY", "COST_CENTRE"]

    auth = repo.get_authorisation("AUTH_REGION")
    assert not isinstance(auth, UnsupportedResult)
    ranges = {r.characteristic: r for r in auth.ranges}
    assert ranges["COST_CENTRE"].operator == "between"
    assert ranges["COST_CENTRE"].low == "1000"
    assert ranges["COMPANY"].sign == "include"


def test_an_unresolvable_layout_names_the_candidates_and_the_real_columns() -> None:
    """When it does go unsupported, the message has to be enough to fix it.

    D53 hid behind a message that named only the two columns it wanted. A reader seeing "AUTH/IOBJNM
    could not be resolved" has no way to tell a genuinely unsupported release from a wrong guess, so
    the actual column list travels with the failure.
    """
    repo, _ = _repo(layout={**_COLUMNS, "RSECVAL": ["SOMETHING_ELSE", "ANOTHER"]})
    unsupported = repo.require_security()
    assert unsupported is not None
    detail = unsupported.detail or ""
    assert "TCTAUTH" in detail and "AUTH" in detail, "the candidates tried must be named"
    assert "SOMETHING_ELSE" in detail, "the table's real columns must be named"
    assert "does NOT mean no authorisations exist" in detail


def test_negating_operators_decode_rather_than_falling_through_to_unknown() -> None:
    """RSZ_OPERATOR_DOMAIN declares ten values; five of them negate.

    Decoding 'NE' as "unknown" in a permission payload reports an *exclusion* as uninterpretable,
    which understates a restriction. Verified against the domain's declared value set, not recalled.
    """
    assert _OPERATORS["NE"] == "not_equal"
    assert _OPERATORS["NB"] == "not_between"
    assert _OPERATORS["NP"] == "not_pattern"
    assert _OPERATORS["GT"] == "greater_than"
    assert _OPERATORS["LT"] == "less_than"
    assert len(_OPERATORS) == 10, "the domain declares ten values on the reference system"


# --- an empty assignment table is not zero users (D54) ----------------------------------------


def test_an_empty_assignment_table_reports_unknown_not_zero_users() -> None:
    """The distinction that was missing: readable-and-empty is not the same as answered.

    On the reference system RSECUSERAUTH holds no rows at all, while the role side holds 1,063
    grants
    of S_RS_AUTH/BIAUTH across 487 authorisations for 1,805 users with roles. Assignment is simply
    recorded somewhere this server does not read. Reporting users_with_any_authorisation=0 and an
    empty unrestricted-user list with nothing to qualify them says "nobody is governed" and "nobody
    is unrestricted" at the same time — both unfounded, and the second is the reassuring one.
    """
    repo, _ = _repo(assignments={})
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.users_with_any_authorisation is None, "0 would be a claim; None is the truth"
    assert any("UNAVAILABLE rather than zero" in c for c in overview.caveats)
    assert any("AGR_1251" in c for c in overview.caveats), (
        "the caveat should name where the assignments actually live, or the reader is left stuck"
    )

    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, _, _ = listed
    assert all(s.user_count is None for s in summaries)


def test_a_populated_assignment_table_still_reports_its_counts() -> None:
    """The fix must not make the working case unknown too."""
    repo, _ = _repo()
    overview = repo.overview()
    assert not isinstance(overview, UnsupportedResult)
    assert overview.users_with_any_authorisation == 3
    assert not any("UNAVAILABLE rather than zero" in c for c in overview.caveats)


# --- one description per authorisation, chosen not stumbled upon (D55) ------------------------


def test_the_description_is_picked_by_language_rather_than_by_row_order() -> None:
    """Nine languages per authorisation on the reference system; the dict kept whichever came last.

    Same query, same data, different answer depending on how the database ordered the rows. The
    fixture lists English last for AUTH_REGION and omits it entirely for AUTH_STAR, so a reader that
    takes the first row, the last row, or only English each fails a different assertion here.
    """
    repo, _ = _repo()
    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, _, _ = listed
    by_name = {s.name: s for s in summaries}
    assert by_name["AUTH_REGION"].description == "Region restriction"
    assert by_name["AUTH_AGG"].description == "Aggregated only"
    # No English row exists for this one, so a description in some language beats none at all.
    assert by_name["AUTH_STAR"].description == "Alles"


def test_a_release_without_a_language_column_still_returns_a_description() -> None:
    """The language role is optional: resolving it is an improvement, not a new requirement."""
    repo, _ = _repo(layout={**_COLUMNS, "RSECTXT": ["AUTH", "TXTLG"]})
    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, _, _ = listed
    assert next(s for s in summaries if s.name == "AUTH_REGION").description is not None


def test_a_blank_long_text_falls_back_to_the_shorter_one() -> None:
    """Measured on the catch-all, which is the worst possible object to lose a description for.

    SAP ships 0BI_ALL with an empty long text in all nine languages and only its short text filled.
    Resolving one text column therefore returned no description for the single authorisation whose
    meaning a reader most needs, while 470 others read fine - invisible at any aggregate level.
    """
    repo, _ = _repo()
    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, _, _ = listed
    assert next(s for s in summaries if s.name == "AUTH_VAR").description == "Per-user company"


def test_unreadable_authorisations_leave_coverage_unknown_not_uncovered() -> None:
    """The fault that survives D53's fix, and the worse of the two failure modes (D57).

    Three security tools returned a visible "unsupported" when RSECVAL could not be read.
    This one
    took a different path: it swallowed the gap into an empty covered-set, so every authorisation-
    relevant characteristic in the query looked uncovered and drew the caveat "without a catch-all
    the query returns no data". Reproduced on production during the D53 fix: a query whose six
    authorisation-relevant characteristics are all covered had all six reported as uncovered.

    A withheld grant is the realistic trigger, not a release quirk, so it is exercised as one.
    """
    repo, _ = _repo(unreadable={"RSECVAL"})
    exposure = repo.query_exposure(
        compuid="QUID",
        compid="Q_TEST",
        providers=["PROV"],
        characteristics=["COST_CENTRE", "COMPANY"],
    )
    assert exposure.auth_relevant_characteristics == ["COMPANY", "COST_CENTRE"]
    assert exposure.uncovered_characteristics == [], (
        "an unreadable authorisation table must not be reported as nothing being covered"
    )
    assert any("UNKNOWN - not uncovered" in c for c in exposure.caveats)
    assert not any("returns no data" in c for c in exposure.caveats)


def test_readable_authorisations_still_surface_a_genuine_coverage_gap() -> None:
    """The fix must not silence the real finding: PROFIT_CENTRE is relevant, nothing covers it."""
    repo, _ = _repo()
    exposure = repo.query_exposure(
        compuid="QUID",
        compid="Q_TEST",
        providers=["PROV"],
        characteristics=["COST_CENTRE", "PROFIT_CENTRE"],
    )
    assert exposure.uncovered_characteristics == ["PROFIT_CENTRE"]
    assert any("returns no data" in c for c in exposure.caveats)


def test_a_text_that_only_repeats_the_technical_name_is_not_a_description() -> None:
    """0BI_ALL's short text is the literal string '0BI_ALL'.

    Falling back across text columns makes that reachable, which is worse than the gap it fixes
    if it
    is passed through: the caller cannot tell a real description from an echo. So it is still
    None here - now because the text was read and judged worthless, not because the read missed it.
    """
    repo, _ = _repo()
    listed = repo.list_authorisations(limit=50)
    assert not isinstance(listed, UnsupportedResult)
    summaries, _, _ = listed
    assert next(s for s in summaries if s.name == "0BI_ALL").description is None


def test_catch_all_is_not_labelled_generated_despite_its_prefix() -> None:
    """0BI_ALL carries the SAP prefix but is not a generated authorisation."""
    repo, _ = _repo()
    assert repo._origin("0BI_ALL") == "maintained"
    assert repo._origin("0BI_SOMETHING") == "generated"


def test_a_name_that_says_nothing_about_origin_is_unknown_not_maintained() -> None:
    """The heuristic reads a naming convention, so silence is silence (D56).

    This assertion used to read ``== "maintained"``, and the change is the point. BW records no flag
    for whether a program maintains an authorisation, so the only evidence is the name - and where
    the name carries none, "maintained" is a claim rather than a reading. It is also the claim that
    does harm: it invites someone to hand-edit an authorisation that a DAP run overwrites. Measured
    on the reference system, the convention matched 1 of 296 authorisations, so the old default
    asserted hand-maintenance for 295 of them on no evidence at all.
    """
    repo, _ = _repo()
    assert repo._origin("AUTH_REGION") == "unknown"
    assert repo._origin("LOCAL_AUTH_ONE") == "unknown"


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
