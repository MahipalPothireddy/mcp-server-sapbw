"""Analysis-authorisation (row-level security) repository.

Reads the ``RSEC*`` family: authorisation value ranges, hierarchy-node authorisations, descriptions
and user assignment, joined against the characteristics BW flags authorisation-relevant.

Three design constraints, each enforced here rather than left to operator discipline:

**Never cached.** Every other repository routes expensive per-object reads through the SQLite cache.
This one does not, at any tier. The payload is permission data rather than structure, so persisting
it widens the blast radius of the cache file; and a permission set changes when someone joins, moves
or leaves, where a stale answer to "who can see this" is worse than a slow one.

**Columns are discovered, not assumed.** The ``RSEC*`` column layout is not something to
take from memory across BW 7.4/7.5/BW4, so every column read here is resolved against
``DD03L`` first, and a table whose essential columns are absent yields a structured gap.
Hardcoding a column name that a release does not have turns a portable tool into a stack
trace.

**Silence never reads as safety.** A locked-down reporting user routinely cannot read these
tables - they *are* the authorisation model. Unreadable is reported as unreadable, never as
"no authorisations exist", which would invert the finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.cache import SqliteCache
from ..core.capabilities import SupportsSelect, unsupported_result
from ..models.capability import CapabilityRecord
from ..models.completeness import COMPLETE, BoundHit, Completeness, bounded
from ..models.provenance import UnsupportedResult
from ..models.security import (
    AnalysisAuth,
    AnalysisAuthSummary,
    AuthHierarchyNode,
    AuthOrigin,
    AuthRelevantCharacteristic,
    AuthValueRange,
    QueryAuthExposure,
    RangeOperator,
    RangeSign,
    SecurityOverview,
    SpecialValue,
)
from .base import Repository
from .texts import DEFAULT_LANGUAGE, lang_rank

#: The catch-all authorisation SAP ships. A holder is unrestricted.
CATCH_ALL = "0BI_ALL"

#: Special values inside a range, decoded rather than passed through raw. ':' is the one that gets
#: misread most often: it permits aggregated access only, which is not the same as no access.
_SPECIAL_VALUES: dict[str, SpecialValue] = {
    "*": "all",
    ":": "aggregation_only",
    "#": "unassigned",
}

# RSECVAL sign/option codes, decoded. An unrecognised code is reported 'unknown', never guessed.
#
# Both maps were checked against this system's dictionary rather than recalled: the sign column sits
# on RALDB_SIGN, which declares exactly I and E, and the option column sits on RSZ_OPERATOR_DOMAIN,
# which declares ten values. Both are registered in the domain-drift registry so a release that
# changes either is reported instead of silently decoding to 'unknown'.
_SIGNS: dict[str, RangeSign] = {"I": "include", "E": "exclude"}
_OPERATORS: dict[str, RangeOperator] = {
    "EQ": "equal",
    "NE": "not_equal",
    "BT": "between",
    "NB": "not_between",
    "GE": "greater_equal",
    "GT": "greater_than",
    "LE": "less_equal",
    "LT": "less_than",
    "CP": "pattern",
    "NP": "not_pattern",
}

# Candidate column names per logical role, most likely first. Resolved against the connected system
# at runtime, so a release that names a column differently is handled by adding a candidate here.
#
# **The TCT-prefixed names are not alternatives, they are what BW actually ships** (D53). Every role
# below previously listed only the unprefixed name - AUTH, IOBJNM, LOW - and on BW 7.50 not one of
# them exists: the columns are TCTAUTH, TCTIOBJNM, TCTLOW. Because require_security() fails closed
# when the authorisation and characteristic roles cannot be resolved, the effect was not a wrong
# answer but a *dead subsystem*: all three security tools returned "unsupported on this release"
# against tables holding 443 active value rows across 296 authorisations. Nothing raised, because
# returning an unsupported result is what the code is supposed to do when a column is missing. That
# is the same failure shape as D48 - a capability check working exactly as designed on top of a
# wrong premise - and it is why the unprefixed names are kept rather than replaced: they are
# unverified on any system reachable from here, and dropping them would trade one guess for another.
_VALUE_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("TCTAUTH", "AUTH"),
    "characteristic": ("TCTIOBJNM", "IOBJNM", "CHANM"),
    "sign": ("TCTSIGN", "SIGNCH", "SIGN"),
    "operator": ("TCTOPTION", "OPTIONCH", "OPTION"),
    "low": ("TCTLOW", "LOW"),
    "high": ("TCTHIGH", "HIGH"),
}
# RSECHIE. 'node_type' resolves to TCTATYPE, whose domain RSSAUTHHIERTYPE declares how far access
# reaches below the node (0 selected nodes only, 1 subtree, 2 subtree to an absolute level, 3 whole
# hierarchy, 4 subtree to a relative level). The raw code is passed through rather than decoded:
# this table is empty on the only system available to verify against, so a decode here would be an
# untested claim in a permission payload.
#
# validity_from / validity_to are deliberately left unresolvable on BW 7.50. The table's two date
# columns, TCTHIEDATE and TCTHDATE, are *not* a from/to pair - TCTHIEDATE is the hierarchy key date
# that TCTACOMPM compares against ("Name, Version Identical and Key Date Less Than or Equal to").
# Mapping a key date onto a validity range would have produced a confident, wrong statement about
# when a permission applies, so the roles stay unresolved and the caveat reports them missing.
_HIERARCHY_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("TCTAUTH", "AUTH"),
    "characteristic": ("TCTIOBJNM", "IOBJNM", "CHANM"),
    "hierarchy": ("TCTHIENM", "HIENM"),
    "node": ("TCTNODE", "NODENAME"),
    "node_type": ("TCTATYPE", "NODETYPE"),
    "level": ("TCTTLEVEL", "TLEVEL", "LEVEL"),
    "validity_from": ("DATEFROM",),
    "validity_to": ("DATETO",),
}
# RSECUSERAUTH is the one table in this group that does use unprefixed names.
_USER_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("AUTH", "TCTAUTH"),
    "user": ("UNAME", "BNAME"),
}
# 'language' is its own role because without it the description was picked arbitrarily (D55): this
# system holds nine languages per authorisation, so a dict built in row order returned whichever the
# database happened to emit last.
_TEXT_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("TCTAUTH", "AUTH"),
    "text": ("TCTTXTLG", "TCTTXTMD", "TCTTXTSH", "TXTLG", "TXTSH", "TEXT"),
    "language": ("TCTLANGU", "LANGU", "SPRAS"),
}

#: Row budget for the landscape-wide shape scan of RSECVAL. Hitting it is reported, not hidden.
_MAX_SCAN_ROWS = 50_000
#: Per-authorisation range budget.
_MAX_RANGES = 2000
_MAX_AUTHS = 5000
_MAX_USERS = 5000
#: Languages allowed for per authorisation when reading the text table. The reference system holds
#: nine; the headroom is deliberate, because hitting this cap costs descriptions on the tail of a
#: page rather than raising.
_MAX_TEXT_LANGUAGES = 40

# A leading '$' marks a variable reference in an authorisation value: the effective scope resolves
# per user at runtime, so metadata cannot state it.
_VARIABLE_PREFIX = "$"

_NEVER_CACHED_CAVEAT = (
    "authorisation data is read live and never cached: a permission set changes when someone "
    "joins, moves or leaves, so a stale answer here would be worse than a slow one"
)

#: Said when the assignment table is readable but holds nothing (D54).
#:
#: An empty table and an unreadable one were treated as different problems and only the second was
#: caveated. So a system where RSECUSERAUTH holds no rows reported
#: ``users_with_any_authorisation = 0`` and ``unrestricted_users = []`` with nothing to qualify
#: them - two claims that read as
#: "nobody is governed" and "nobody is unrestricted", which cannot both be reassuring and in this
#: case were both unfounded. On the reference system that table is empty while the role side holds
#: 1,063 grants of S_RS_AUTH/BIAUTH across 487 authorisations, for 1,805 users holding roles. Zero
#: was not a finding about access; it was a measurement that had not been taken.
_ASSIGNMENT_EMPTY_CAVEAT = (
    "the analysis-authorisation -> user assignment table is present but holds no rows, so user "
    "counts and the unrestricted-user list are UNAVAILABLE rather than zero. Do not read this as "
    "'nobody holds an authorisation'. BW also grants analysis authorisations through role "
    "maintenance (authorisation object S_RS_AUTH, field BIAUTH, stored in AGR_1251), which this "
    "server does not read; on a system configured that way the assignment table is legitimately "
    "empty while thousands of grants exist."
)
_ASSIGNMENT_UNREADABLE_CAVEAT = (
    "the authorisation -> user assignment table is not readable with this connection, so user "
    "counts and the unrestricted-user list are unavailable (not empty)"
)

#: Length of an ABAP date literal (YYYYMMDD).
_DATE_LENGTH = 8


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _special(value: str | None) -> SpecialValue:
    return _SPECIAL_VALUES.get((value or "").strip(), "literal")


def _as_date(value: Any) -> date | None:
    text = _clean(value)
    if text is None or len(text) != _DATE_LENGTH or not text.isdigit() or text == "00000000":
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


@dataclass(frozen=True)
class _Columns:
    """Resolved physical column names for one table, by logical role.

    ``None`` for a role means the release does not have that column, which is reported as a
    limitation on the affected facts rather than substituted with a guess.
    """

    resolved: dict[str, str | None]

    def __getitem__(self, role: str) -> str | None:
        return self.resolved.get(role)

    def has(self, *roles: str) -> bool:
        return all(self.resolved.get(role) is not None for role in roles)

    def selected(self, roles: Sequence[str]) -> tuple[list[str], list[str]]:
        """``(roles_present, physical_columns)`` for the roles this release actually has."""
        present = [role for role in roles if self.resolved.get(role) is not None]
        return present, [str(self.resolved[role]) for role in present]


class SecurityRepository(Repository):
    """Analysis authorisations, their assignment, and the coverage gaps they leave."""

    def __init__(
        self,
        connection: SupportsSelect,
        capability: CapabilityRecord,
        cache: SqliteCache | None = None,
    ) -> None:
        """``cache`` is accepted for interface symmetry and deliberately ignored.

        Callers pass ``None``, and a caller that passes a cache by mistake still gets no
        caching: the decision that permission data does not go to disk is enforced here, not
        at the call site.
        """
        super().__init__(connection, capability, None)
        self._column_cache: dict[str, set[str]] = {}
        # Tri-state, resolved at most once per instance: True rows present, False readable but
        # empty, None unreadable. In-memory only - this is still permission data.
        self._assignment_rows: bool | None = None
        self._assignment_probed = False

    # --- availability --------------------------------------------------------------------

    def require_security(self) -> UnsupportedResult | None:
        """``RSECVAL`` plus its essential columns: without both there is nothing to read."""
        unsupported = self.require("auth_values")
        if unsupported is not None:
            return unsupported
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        if not columns.has("auth", "characteristic"):
            table = self.physical("auth_values")
            unresolved = [
                f"{role} (tried {'/'.join(_VALUE_COLUMNS[role])})"
                for role in ("auth", "characteristic")
                if not columns.has(role)
            ]
            available = sorted(self._table_columns("auth_values"))
            return unsupported_result(
                self.capability,
                [f"{table}.{role.split(' ')[0]}" for role in unresolved],
                detail=(
                    # The candidate names and the real column list are both named on purpose. When
                    # this fired on BW 7.50 (D53) the message said only "AUTH/IOBJNM could not be
                    # resolved", which told the reader nothing about what the table *does* have, so
                    # a dead subsystem looked like an unsupported release. Whoever sees this next
                    # should be able to fix it by reading the message.
                    f"{table} exists but these column roles could not be resolved on this "
                    f"release: {'; '.join(unresolved)}. The table's actual columns are: "
                    f"{', '.join(available) if available else '(could not be read)'}. Add the "
                    "correct name to the candidate list in repositories/security.py to support "
                    "this release. This is a documented gap: it does NOT mean no authorisations "
                    "exist."
                ),
            )
        return None

    # --- listing (shape only, never values) ----------------------------------------------

    def list_authorisations(
        self, *, limit: int = 100, offset: int = 0, include_generated: bool = True
    ) -> tuple[list[AnalysisAuthSummary], int, bool] | UnsupportedResult:
        """Authorisations by shape: which characteristics, how many ranges, catch-all or not.

        Returns ``(page, total, scan_truncated)``. Deliberately excludes concrete values: a
        landscape-wide question should not incidentally place a permission dump in the caller's
        context, and ``get_authorisation`` is the opt-in for that.
        """
        unsupported = self.require_security()
        if unsupported is not None:
            return unsupported
        shapes, scan_truncated = self._scan_shapes()

        names = sorted(shapes)
        texts = self._descriptions(names)
        hierarchies = self._hierarchy_counts(names)
        users = self._user_counts(names)
        summaries: list[AnalysisAuthSummary] = []
        for auth in names:
            shape = shapes[auth]
            origin = self._origin(auth)
            if origin == "generated" and not include_generated:
                continue
            summaries.append(
                AnalysisAuthSummary(
                    name=auth,
                    description=texts.get(auth),
                    origin=origin,
                    characteristics=sorted(shape["chars"]),
                    range_count=int(shape["ranges"]),
                    hierarchy_node_count=hierarchies.get(auth, 0),
                    grants_everything=auth == CATCH_ALL or bool(shape["all"]),
                    variable_driven_characteristics=sorted(shape["variables"]),
                    user_count=users.get(auth) if users is not None else None,
                    provenance=self.provenance("auth_values", {"AUTH": auth}),
                )
            )
        return summaries[offset : offset + limit], len(summaries), scan_truncated

    # --- one authorisation, with values (opt-in) ------------------------------------------

    def get_authorisation(self, name: str) -> AnalysisAuth | UnsupportedResult:
        """One authorisation in full, including its value ranges.

        The only path that returns concrete permission values; the result is labelled
        ``contains_data_values=True`` so that is visible in the payload itself.
        """
        unsupported = self.require_security()
        if unsupported is not None:
            return unsupported
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        roles, physical = columns.selected(
            ["characteristic", "sign", "operator", "low", "high"],
        )
        auth_column = str(columns["auth"])
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=physical,
                    from_logical="auth_values",
                    where=[f"{auth_column} = ?"],
                    params=[name],
                    order_by=[str(columns["characteristic"])],
                ),
                limit=_MAX_RANGES + 1,
            )
        )
        truncated = len(rows) > _MAX_RANGES
        ranges: list[AuthValueRange] = []
        grants_all = bool(rows)
        for row in rows[:_MAX_RANGES]:
            values = dict(zip(roles, row, strict=False))
            characteristic = _clean(values.get("characteristic"))
            if characteristic is None:
                continue
            low_text, high_text = _clean(values.get("low")), _clean(values.get("high"))
            special = _special(low_text)
            if special != "all":
                grants_all = False
            ranges.append(
                AuthValueRange(
                    characteristic=characteristic,
                    sign=_SIGNS.get((_clean(values.get("sign")) or "").upper(), "unknown"),
                    operator=_OPERATORS.get(
                        (_clean(values.get("operator")) or "").upper(), "unknown"
                    ),
                    low=low_text,
                    high=high_text,
                    special=special,
                    is_variable=(low_text or "").startswith(_VARIABLE_PREFIX),
                    provenance=self.provenance(
                        "auth_values", {"AUTH": name, "IOBJNM": characteristic}
                    ),
                )
            )

        caveats = [
            "this payload contains permission data (which values a user may see), not structural "
            "metadata; it is never cached and should be handled accordingly",
            _NEVER_CACHED_CAVEAT,
        ]
        missing_roles = [
            role for role in ("sign", "operator", "low", "high") if not columns.has(role)
        ]
        if missing_roles:
            caveats.append(
                f"column(s) {', '.join(missing_roles)} are not present on this release, so those "
                "range attributes are reported as unknown rather than guessed"
            )
        variable_ranges = [r for r in ranges if r.is_variable]
        if variable_ranges:
            caveats.append(
                f"{len(variable_ranges)} range(s) are driven by a variable, so the effective scope "
                "resolves per user at runtime and cannot be read from metadata"
            )
        if any(r.special == "aggregation_only" for r in ranges):
            caveats.append(
                "a ':' range grants aggregated access only: the user can see a total but not the "
                "individual rows behind it. This is not the same as no access."
            )
        assignment = self._assignment_state()
        if assignment is None:
            caveats.append(_ASSIGNMENT_UNREADABLE_CAVEAT)
        elif assignment is False:
            caveats.append(_ASSIGNMENT_EMPTY_CAVEAT)
        if truncated:
            caveats.append(f"range list capped at {_MAX_RANGES}")

        return AnalysisAuth(
            name=name,
            description=self._descriptions([name]).get(name),
            origin=self._origin(name),
            ranges=ranges,
            hierarchy_nodes=self._hierarchy_nodes(name),
            grants_everything=name == CATCH_ALL or grants_all,
            assigned_users=self._users_of(name),
            completeness=(
                bounded("row_cap", scope="ranges", limit=_MAX_RANGES) if truncated else COMPLETE
            ),
            caveats=caveats,
            provenance=self.provenance("auth_values", {"AUTH": name}),
        )

    # --- landscape posture ----------------------------------------------------------------

    def overview(self, *, limit: int = 200) -> SecurityOverview | UnsupportedResult:
        """The row-level security posture: catch-alls, unrestricted users, coverage gaps."""
        unsupported = self.require_security()
        if unsupported is not None:
            return unsupported

        listed = self.list_authorisations(limit=limit, offset=0)
        if isinstance(listed, UnsupportedResult):
            return listed
        summaries, total, scan_truncated = listed

        catch_all = [s.name for s in summaries if s.grants_everything]
        relevant = self._auth_relevant_characteristics()
        for entry in relevant:
            entry.authorisation_count = sum(
                1 for s in summaries if entry.characteristic in s.characteristics
            )
            entry.covered = entry.authorisation_count > 0
        uncovered = [e.characteristic for e in relevant if not e.covered]

        caveats = [_NEVER_CACHED_CAVEAT]
        assignment = self._assignment_state()
        if assignment is None:
            caveats.append(_ASSIGNMENT_UNREADABLE_CAVEAT)
        elif assignment is False:
            caveats.append(_ASSIGNMENT_EMPTY_CAVEAT)
        if not relevant:
            caveats.append(
                "no authorisation-relevant characteristics could be read (RSDCHA.AUTHRELFL "
                "absent or unreadable), so coverage gaps could not be assessed - treat as "
                "unknown, not clean"
            )
        if uncovered:
            caveats.append(
                f"{len(uncovered)} characteristic(s) are flagged authorisation-relevant but no "
                "authorisation covers them: every query touching those returns no data for a user "
                "without a catch-all. This is a live configuration fault, not a style issue."
            )
        if catch_all:
            caveats.append(
                f"{len(catch_all)} authorisation(s) grant everything; their holders are "
                "unrestricted and should not be counted as governed."
            )
        if scan_truncated:
            caveats.append(
                f"the value scan stopped at {_MAX_SCAN_ROWS} rows, so authorisations beyond that "
                "point are not represented in these counts"
            )
        return SecurityOverview(
            authorisation_count=total,
            generated_count=sum(1 for s in summaries if s.origin == "generated"),
            maintained_count=sum(1 for s in summaries if s.origin == "maintained"),
            catch_all_authorisations=catch_all,
            unrestricted_users=self._users_of_many(catch_all),
            auth_relevant_characteristics=relevant,
            uncovered_characteristics=uncovered,
            users_with_any_authorisation=self._distinct_user_count(),
            # Two different bounds were being merged into one bool (D6), and they mean opposite
            # things about the *findings*: a page limit leaves the counts correct and shows fewer
            # authorisations, while a truncated value scan makes the counts themselves - including
            # `uncovered_characteristics`, which is the live configuration fault this tool exists to
            # surface - a lower bound. Reported separately so a caller can tell which it is.
            completeness=Completeness(
                bounds=[
                    *(
                        [BoundHit(bound="row_cap", scope="value_scan", limit=_MAX_SCAN_ROWS)]
                        if scan_truncated
                        else []
                    ),
                    *(
                        [BoundHit(bound="page_limit", scope="authorisations", limit=len(summaries))]
                        if total > len(summaries)
                        else []
                    ),
                ]
            ),
            caveats=caveats,
            provenance=self.provenance("auth_values", {}),
        )

    # --- per-query exposure ---------------------------------------------------------------

    def query_exposure(
        self, *, compuid: str, compid: str | None, providers: list[str], characteristics: list[str]
    ) -> QueryAuthExposure:
        """Which authorisation-relevant characteristics a query is subject to.

        Reports the characteristics in play, never who sees what: that needs a per-user value join,
        which this deliberately does not do.
        """
        relevant = {e.characteristic for e in self._auth_relevant_characteristics()}
        in_play = sorted(relevant & {c.strip().upper() for c in characteristics})
        # `None` means coverage could not be assessed, which is not the same as nothing being
        # covered (D57). The difference is the whole finding: treating an unreadable RSECVAL as an
        # empty one declares every characteristic in the query uncovered and attaches "without a
        # catch-all the query returns no data" - a live configuration fault asserted from
        # an absence of data. Demonstrated while fixing D53: on a subject query whose six
        # authorisation-relevant
        # characteristics are all covered, the unreadable path called all six uncovered. That is
        # worse than the three tools D53 silenced, because a gap is visible and this was not.
        covered = self._characteristics_with_authorisations()
        uncovered = [char for char in in_play if char not in covered] if covered is not None else []
        caveats = [
            "lists the characteristics that make this query user-specific; it does not "
            "resolve what any individual sees, which needs a per-user value join",
            "a query restricted on an authorisation-relevant characteristic returns different rows "
            "per user, so two people comparing figures can both be right",
        ]
        if covered is None and in_play:
            caveats.append(
                "the authorisation values could not be read, so whether anything grants these "
                "characteristics is UNKNOWN - not uncovered. Coverage was not assessed here; use "
                "bw_access_report to find out whether this is a grant or a release limitation."
            )
        if uncovered:
            caveats.append(
                f"{len(uncovered)} characteristic(s) in this query are authorisation-relevant "
                "but no authorisation grants any value: without a catch-all the query returns "
                "no data"
            )
        return QueryAuthExposure(
            compuid=compuid,
            compid=compid,
            providers=providers,
            auth_relevant_characteristics=in_play,
            uncovered_characteristics=uncovered,
            user_specific_result=bool(in_play),
            caveats=caveats,
            provenance=self.provenance("auth_values", {"COMPUID": compuid}),
        )

    # --- shape scan -----------------------------------------------------------------------

    def _scan_shapes(self) -> tuple[dict[str, dict[str, Any]], bool]:
        """Scan RSECVAL once and reduce it to per-authorisation shape (no values retained)."""
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        roles, physical = columns.selected(["auth", "characteristic", "low", "high"])
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=physical,
                    from_logical="auth_values",
                    order_by=[str(columns["auth"])],
                ),
                limit=_MAX_SCAN_ROWS + 1,
            )
        )
        truncated = len(rows) > _MAX_SCAN_ROWS
        shapes: dict[str, dict[str, Any]] = {}
        for row in rows[:_MAX_SCAN_ROWS]:
            values = dict(zip(roles, row, strict=False))
            auth = _clean(values.get("auth"))
            if auth is None:
                continue
            shape = shapes.setdefault(
                auth, {"chars": set(), "ranges": 0, "all": True, "variables": set()}
            )
            shape["ranges"] += 1
            characteristic = _clean(values.get("characteristic"))
            if characteristic is None:
                continue
            shape["chars"].add(characteristic)
            low_text, high_text = _clean(values.get("low")), _clean(values.get("high"))
            if (low_text or "").startswith(_VARIABLE_PREFIX):
                shape["variables"].add(characteristic)
            # "grants everything" only holds if *every* range is '*'.
            if _special(low_text) != "all" and _special(high_text) != "all":
                shape["all"] = False
        return shapes, truncated

    # --- helpers --------------------------------------------------------------------------

    def _columns(self, logical: str, roles: dict[str, tuple[str, ...]]) -> _Columns:
        """Resolve logical column roles to physical names present on this release, via DD03L."""
        available = self._table_columns(logical)
        return _Columns(
            {
                role: next((c for c in candidates if c in available), None)
                for role, candidates in roles.items()
            }
        )

    def _table_columns(self, logical: str) -> set[str]:
        if logical in self._column_cache:
            return self._column_cache[logical]
        # Prefer the column set the capability resolver already measured at connect (D45). It is the
        # same dictionary read, done once for the whole server instead of once per table per
        # instance, and it means this subsystem no longer needs DD03L to be separately grantable:
        # before, a connection without it resolved no roles and the security tools went dead for a
        # reason that had nothing to do with the RSEC* tables. The live read stays as the fallback
        # for tables the resolver could not measure.
        status = self.capability.table(logical)
        if status is not None and status.present and status.columns_known:
            measured = {str(column).strip().upper() for column in status.columns}
            if measured:
                self._column_cache[logical] = measured
                return measured
        columns: set[str] = set()
        if self.capability.is_available(logical) and self.capability.is_available("dict_columns"):
            try:
                rows = self.select(
                    self.dialect.paginate(
                        self.dialect.build_select(
                            columns=["FIELDNAME"],
                            from_logical="dict_columns",
                            where=["TABNAME = ?"],
                            params=[self.physical(logical)],
                            order_by=["FIELDNAME"],  # capped read; see D8
                        ),
                        limit=1000,
                    )
                )
                columns = {str(r[0]).strip().upper() for r in rows if _clean(r[0])}
            except Exception:
                columns = set()
        self._column_cache[logical] = columns
        return columns

    def _origin(self, auth: str) -> AuthOrigin:
        """Generated authorisations are program/DAP-maintained: a manual edit is lost on next run.

        BW records no flag for this, so it is read from the naming convention SAP's own generation
        uses - and where the name says nothing, the answer is ``"unknown"`` rather than
        ``"maintained"`` (D56). Falling back to "maintained" made an assertion out of an absence of
        evidence, and the failure was total rather than marginal: on the reference system the
        convention matched exactly one of 296 authorisations, so 295 were reported as
        hand-maintained purely because their names did not look generated. Someone acting on that
        would edit an authorisation a program overwrites on its next run. ``AuthOrigin`` already had
        "unknown" available; nothing needed inventing, only using.
        """
        upper = auth.upper()
        if upper == CATCH_ALL:
            return "maintained"
        if upper.startswith(("0BI_", "!")) or upper.endswith("_GEN"):
            return "generated"
        return "unknown"

    def _descriptions(self, auths: list[str]) -> dict[str, str]:
        if not auths or not self.capability.is_available("auth_text"):
            return {}
        columns = self._columns("auth_text", _TEXT_COLUMNS)
        if not columns.has("auth", "text"):
            return {}
        # The text table is keyed by language, so reading it without one returns a row per language
        # per authorisation and the last one written to the dict wins (D55). On the reference system
        # that is nine rows each, which made the description non-deterministic: same query, same
        # data, different answer depending on how the database ordered the result. Ranked with the
        # same rule the texts repository uses - preferred, then English, then whatever exists - so a
        # system holding only German still gets a description rather than nothing.
        # Every text column this release has, in preference order, not just the first one.
        # Resolving a
        # single column lost the description whenever the preferred one was blank, and the case that
        # exposed it is the one that matters most: SAP ships 0BI_ALL - the catch-all, the
        # single most
        # important authorisation on any system - with all nine of its long texts empty and only the
        # short text filled. One authorisation in 471 here, but a system populating short texts by
        # convention would lose all of them, which is the portability version of the same bug.
        available = self._table_columns("auth_text")
        text_columns = [c for c in _TEXT_COLUMNS["text"] if c in available]
        language_column = columns["language"]
        selected = [str(columns["auth"]), *text_columns]
        if language_column is not None:
            selected.append(language_column)
        placeholders = ", ".join("?" for _ in auths)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=selected,
                        from_logical="auth_text",
                        where=[f"{columns['auth']} IN ({placeholders})"],
                        params=list(auths),
                        order_by=[str(columns["auth"])],  # capped read; see D8
                    ),
                    # One row per language, so the cap has to allow for all of them or the last
                    # authorisations in the page silently lose their descriptions.
                    limit=_MAX_AUTHS * _MAX_TEXT_LANGUAGES,
                )
            )
        except Exception:
            return {}
        language_index = 1 + len(text_columns)
        best: dict[str, tuple[int, str]] = {}
        for row in rows:
            auth = _clean(row[0])
            if auth is None:
                continue
            text = next(
                (value for value in (_clean(cell) for cell in row[1:language_index]) if value),
                None,
            )
            # A description that merely repeats the technical name is not a description. This is the
            # quality rule the description subsystem applies everywhere else, and 0BI_ALL is exactly
            # the case it exists for: its short text is the literal string "0BI_ALL". Dropping it
            # keeps the answer None - but now for a measured reason rather than because the read
            # happened to look at the wrong column.
            if text is None or text.upper() == auth.upper():
                continue
            has_language = language_column is not None and len(row) > language_index
            language = _clean(row[language_index]) if has_language else None
            rank = lang_rank(language, DEFAULT_LANGUAGE) if language is not None else 1
            current = best.get(auth)
            if current is None or rank < current[0]:
                best[auth] = (rank, text)
        return {auth: text for auth, (_rank, text) in best.items()}

    def _hierarchy_nodes(self, auth: str) -> list[AuthHierarchyNode]:
        if not self.capability.is_available("auth_hierarchy"):
            return []
        columns = self._columns("auth_hierarchy", _HIERARCHY_COLUMNS)
        if not columns.has("auth", "characteristic"):
            return []
        roles, physical = columns.selected(
            [
                "characteristic",
                "hierarchy",
                "node",
                "node_type",
                "level",
                "validity_from",
                "validity_to",
            ],
        )
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=physical,
                        from_logical="auth_hierarchy",
                        where=[f"{columns['auth']} = ?"],
                        params=[auth],
                        order_by=list(physical),  # capped read; see D8
                    ),
                    limit=_MAX_RANGES,
                )
            )
        except Exception:
            return []
        nodes: list[AuthHierarchyNode] = []
        for row in rows:
            values = dict(zip(roles, row, strict=False))
            characteristic = _clean(values.get("characteristic"))
            if characteristic is None:
                continue
            nodes.append(
                AuthHierarchyNode(
                    characteristic=characteristic,
                    hierarchy=_clean(values.get("hierarchy")),
                    node=_clean(values.get("node")),
                    node_type=_clean(values.get("node_type")),
                    level=_clean(values.get("level")),
                    validity_from=_as_date(values.get("validity_from")),
                    validity_to=_as_date(values.get("validity_to")),
                    provenance=self.provenance("auth_hierarchy", {"AUTH": auth}),
                )
            )
        return nodes

    def _hierarchy_counts(self, auths: list[str]) -> dict[str, int]:
        if not auths or not self.capability.is_available("auth_hierarchy"):
            return {}
        columns = self._columns("auth_hierarchy", _HIERARCHY_COLUMNS)
        if not columns.has("auth"):
            return {}
        auth_column = str(columns["auth"])
        placeholders = ", ".join("?" for _ in auths)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[auth_column, "COUNT(*)"],
                        from_logical="auth_hierarchy",
                        where=[f"{auth_column} IN ({placeholders})"],
                        params=list(auths),
                        group_by=[auth_column],
                        order_by=[auth_column],  # capped read; see D8
                    ),
                    limit=_MAX_AUTHS,
                )
            )
        except Exception:
            return {}
        return {str(a).strip(): int(n or 0) for a, n in rows if _clean(a)}

    def _assignment_state(self) -> bool | None:
        """``True`` rows present, ``False`` readable but empty, ``None`` unreadable.

        The middle state is the one that existed without being represented (D54). Every user fact
        this repository reports is derived from one table, and an empty one cannot distinguish "no
        user holds an authorisation" from "assignment is recorded somewhere this server does not
        read". Those have opposite meanings for a reader assessing access, so the difference is
        measured once and carried into the caveats rather than collapsing into zero.
        """
        if self._assignment_probed:
            return self._assignment_rows
        self._assignment_probed = True
        columns = self._user_columns()
        if columns is None:
            self._assignment_rows = None
            return None
        try:
            # Counted rather than read: "does any row exist" needs no rows, and a capped read would
            # have to state an order (D8) to be deterministic, which is meaningless for an existence
            # probe. The aggregate also keeps a permission table's contents out of the process.
            rows = self.select(
                self.dialect.build_select(
                    columns=["COUNT(*)"],
                    from_logical="auth_user",
                )
            )
        except Exception:
            self._assignment_rows = None
            return None
        if not rows or rows[0][0] is None:
            self._assignment_rows = None
            return None
        self._assignment_rows = int(rows[0][0]) > 0
        return self._assignment_rows

    def _users_of(self, auth: str) -> list[str]:
        columns = self._user_columns()
        if columns is None or self._assignment_state() is not True:
            return []
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[str(columns["user"])],
                        from_logical="auth_user",
                        where=[f"{columns['auth']} = ?"],
                        params=[auth],
                        order_by=[str(columns["user"])],
                    ),
                    limit=_MAX_USERS,
                )
            )
        except Exception:
            return []
        return sorted({str(r[0]).strip() for r in rows if _clean(r[0])})

    def _users_of_many(self, auths: list[str]) -> list[str]:
        found: set[str] = set()
        for auth in auths[:20]:  # bounded: a longer catch-all list is itself the finding
            found.update(self._users_of(auth))
        return sorted(found)

    def _user_counts(self, auths: list[str]) -> dict[str, int] | None:
        """``None`` when assignment cannot be read, so "unknown" is never rendered as zero.

        An empty table counts as "cannot be read" here: it yields no row for any authorisation, so
        every count would come back absent anyway, and returning a mapping would assert that the
        question was answered.
        """
        columns = self._user_columns()
        if not auths or columns is None or self._assignment_state() is not True:
            return None
        auth_column, user_column = str(columns["auth"]), str(columns["user"])
        placeholders = ", ".join("?" for _ in auths)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[auth_column, f"COUNT(DISTINCT {user_column})"],
                        from_logical="auth_user",
                        where=[f"{auth_column} IN ({placeholders})"],
                        params=list(auths),
                        group_by=[auth_column],
                        order_by=[auth_column],  # capped read; see D8
                    ),
                    limit=_MAX_AUTHS,
                )
            )
        except Exception:
            return None
        return {str(a).strip(): int(n or 0) for a, n in rows if _clean(a)}

    def _distinct_user_count(self) -> int | None:
        """``None`` rather than 0 when the table is empty: 0 would be a claim, None is the truth."""
        columns = self._user_columns()
        if columns is None or self._assignment_state() is not True:
            return None
        try:
            rows = self.select(
                self.dialect.build_select(
                    columns=[f"COUNT(DISTINCT {columns['user']})"],
                    from_logical="auth_user",
                )
            )
        except Exception:
            return None
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def _user_columns(self) -> _Columns | None:
        if not self.capability.is_available("auth_user"):
            return None
        columns = self._columns("auth_user", _USER_COLUMNS)
        return columns if columns.has("auth", "user") else None

    def _auth_relevant_characteristics(self) -> list[AuthRelevantCharacteristic]:
        """Characteristics BW flags authorisation-relevant (``RSDCHA.AUTHRELFL``)."""
        if not self.capability.is_available("characteristic"):
            return []
        if "AUTHRELFL" not in self._table_columns("characteristic"):
            return []  # not present on this release; the caller reports it as unknown
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["CHANM"],
                        from_logical="characteristic",
                        where=["AUTHRELFL = 'X'"],
                        params=[],
                        order_by=["CHANM"],
                    ),
                    limit=_MAX_AUTHS,
                )
            )
        except Exception:
            return []
        return [
            AuthRelevantCharacteristic(
                characteristic=str(r[0]).strip().upper(),
                provenance=self.provenance("characteristic", {"CHANM": str(r[0]).strip()}),
            )
            for r in rows
            if _clean(r[0])
        ]

    def _characteristics_with_authorisations(self) -> set[str] | None:
        """Characteristics some authorisation grants, or ``None`` when that cannot be read.

        The ``None`` is the point (D57). Returning an empty set on an unreadable table is
        indistinguishable from "no authorisation covers anything", and the caller turns that into a
        finding that every characteristic is uncovered - which reads as a serious misconfiguration
        and would send someone to fix a system that is correctly configured.
        """
        unsupported = self.require_security()
        if unsupported is not None:
            return None
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[f"DISTINCT {columns['characteristic']}"],
                        from_logical="auth_values",
                        # Ordered because the scan is capped. This set is compared against the
                        # authorisation-relevant characteristics to find uncovered ones, so an
                        # arbitrary slice would report a different coverage gap on each run (D8).
                        order_by=[str(columns["characteristic"])],
                    ),
                    limit=_MAX_AUTHS,
                )
            )
        except Exception:
            # The only read in this file that was unguarded, and the realistic failure is a withheld
            # grant rather than a missing table: the authorisation group is the one that
            # mission Section 10
            # names as first to withhold, so a connection that can see RSECVAL in the dictionary but
            # not select from it is the expected case, not an edge one. Unguarded, that propagated a
            # raw database error out of a tool whose whole contract is to degrade into a stated gap.
            return None
        return {str(r[0]).strip().upper() for r in rows if _clean(r[0])}
