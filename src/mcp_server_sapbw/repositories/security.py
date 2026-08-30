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
_SIGNS: dict[str, RangeSign] = {"I": "include", "E": "exclude"}
_OPERATORS: dict[str, RangeOperator] = {
    "EQ": "equal",
    "BT": "between",
    "GE": "greater_equal",
    "LE": "less_equal",
    "CP": "pattern",
}

# Candidate column names per logical role, most likely first. Resolved against DD03L at runtime.
_VALUE_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("AUTH",),
    "characteristic": ("IOBJNM", "CHANM"),
    "sign": ("SIGNCH", "SIGN"),
    "operator": ("OPTIONCH", "OPTION"),
    "low": ("LOW",),
    "high": ("HIGH",),
}
_HIERARCHY_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("AUTH",),
    "characteristic": ("IOBJNM", "CHANM"),
    "hierarchy": ("HIENM",),
    "node": ("NODENAME", "NIOBJNM"),
    "node_type": ("NIOBJNM", "NODETYPE"),
    "level": ("TLEVEL", "LEVEL"),
    "validity_from": ("DATEFROM",),
    "validity_to": ("DATETO",),
}
_USER_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("AUTH",),
    "user": ("UNAME", "BNAME"),
}
_TEXT_COLUMNS: dict[str, tuple[str, ...]] = {
    "auth": ("AUTH",),
    "text": ("TXTLG", "TXTSH", "TEXT"),
}

#: Row budget for the landscape-wide shape scan of RSECVAL. Hitting it is reported, not hidden.
_MAX_SCAN_ROWS = 50_000
#: Per-authorisation range budget.
_MAX_RANGES = 2000
_MAX_AUTHS = 5000
_MAX_USERS = 5000

# A leading '$' marks a variable reference in an authorisation value: the effective scope resolves
# per user at runtime, so metadata cannot state it.
_VARIABLE_PREFIX = "$"

_NEVER_CACHED_CAVEAT = (
    "authorisation data is read live and never cached: a permission set changes when someone "
    "joins, moves or leaves, so a stale answer here would be worse than a slow one"
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

    # --- availability --------------------------------------------------------------------

    def require_security(self) -> UnsupportedResult | None:
        """``RSECVAL`` plus its essential columns: without both there is nothing to read."""
        unsupported = self.require("auth_values")
        if unsupported is not None:
            return unsupported
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        if not columns.has("auth", "characteristic"):
            table = self.physical("auth_values")
            return unsupported_result(
                self.capability,
                [f"{table}.AUTH/IOBJNM"],
                detail=(
                    f"{table} exists but its authorisation and characteristic columns could "
                    "not be resolved from DD03L on this release, so no authorisation can be "
                    "read reliably. This is a documented gap: it does NOT mean no "
                    "authorisations exist."
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
        if not self.capability.is_available("auth_user"):
            caveats.append(
                "the authorisation -> user assignment table is not readable with this "
                "connection, so assigned_users is unavailable (not empty)"
            )
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
            truncated=truncated,
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
        if not self.capability.is_available("auth_user"):
            caveats.append(
                "the authorisation -> user assignment table is not readable with this "
                "connection, so user counts and the unrestricted-user list are unavailable "
                "(not empty)"
            )
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
            truncated=scan_truncated or total > len(summaries),
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
        covered = self._characteristics_with_authorisations()
        uncovered = [char for char in in_play if char not in covered]
        caveats = [
            "lists the characteristics that make this query user-specific; it does not "
            "resolve what any individual sees, which needs a per-user value join",
            "a query restricted on an authorisation-relevant characteristic returns different rows "
            "per user, so two people comparing figures can both be right",
        ]
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
        uses. Nothing further is asserted.
        """
        upper = auth.upper()
        if upper == CATCH_ALL:
            return "maintained"
        if upper.startswith(("0BI_", "!")) or upper.endswith("_GEN"):
            return "generated"
        return "maintained"

    def _descriptions(self, auths: list[str]) -> dict[str, str]:
        if not auths or not self.capability.is_available("auth_text"):
            return {}
        columns = self._columns("auth_text", _TEXT_COLUMNS)
        if not columns.has("auth", "text"):
            return {}
        placeholders = ", ".join("?" for _ in auths)
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[str(columns["auth"]), str(columns["text"])],
                        from_logical="auth_text",
                        where=[f"{columns['auth']} IN ({placeholders})"],
                        params=list(auths),
                        order_by=[str(columns["auth"])],  # capped read; see D8
                    ),
                    limit=_MAX_AUTHS,
                )
            )
        except Exception:
            return {}
        return {str(a).strip(): str(t).strip() for a, t in rows if _clean(a) and _clean(t)}

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

    def _users_of(self, auth: str) -> list[str]:
        columns = self._user_columns()
        if columns is None:
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
        """``None`` when assignment cannot be read, so "unknown" is never rendered as zero."""
        columns = self._user_columns()
        if not auths or columns is None:
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
        columns = self._user_columns()
        if columns is None:
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

    def _characteristics_with_authorisations(self) -> set[str]:
        unsupported = self.require_security()
        if unsupported is not None:
            return set()
        columns = self._columns("auth_values", _VALUE_COLUMNS)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[f"DISTINCT {columns['characteristic']}"],
                    from_logical="auth_values",
                    # Ordered because the scan is capped. This set is compared against the
                    # authorisation-relevant characteristics to find uncovered ones, so an arbitrary
                    # slice would report a different coverage gap on each run (D8).
                    order_by=[str(columns["characteristic"])],
                ),
                limit=_MAX_AUTHS,
            )
        )
        return {str(r[0]).strip().upper() for r in rows if _clean(r[0])}
