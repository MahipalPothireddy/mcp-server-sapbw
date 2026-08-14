"""BEx query repository (B7, mission Section 6).

Reads the query directory (RSZCOMPDIR), the query's own description via the RSZELTTXT/COMPUID join
(a query is itself an element), the element tree (RSZELTXREF parent->child), element definitions
(RSZELTDIR), restrictions (RSZRANGE), and variables (RSZGLOBV) with their processing type. Code
values (DEFTP / LAYTP / VPROCTP / VARTYP / RSZTYPEFLAG) were decoded live from DD07T in B7. All RSZ*
tables are OBJVERS-versioned (auto ``OBJVERS='A'`` via the dialect).
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..core.dialect import quote_ident
from ..models.provenance import UnsupportedResult
from ..models.queries import (
    ElementRole,
    ElementType,
    FieldLineageHop,
    FieldLineagePath,
    Query,
    QueryElement,
    QueryElementEdge,
    QueryLineage,
    QueryOrigin,
    QueryOriginFilter,
    QuerySummary,
    QueryUsage,
    QueryVariable,
    Restriction,
    VariableKind,
    VariableProcessingType,
)
from ..services.lineage import LineageService
from .base import Repository

_DEFTP_TO_TYPE: dict[str, ElementType] = {
    "REP": "query",
    "SEL": "restricted_key_figure",
    "CKF": "calculated_key_figure",
    "FML": "formula",
    "VAR": "variable",
    "STR": "structure",
    "SOB": "filter",
    "CEL": "cell",
    "ATR": "attribute",
    "NIL": "none",
}
_LAYTP_TO_ROLE: dict[str, ElementRole] = {
    "ROW": "rows",
    "COL": "columns",
    "FIX": "filter",
    "FLT": "free",
    "VAR": "variable",
    "CEL": "cell",
    "MBR": "structure_member",
    "NAV": "navigation",
    "AGG": "aggregated",
}
# SAP's technical-name prefix for a query created ad hoc in the BEx Analyzer rather than in Query
# Designer. See models.queries.QueryOrigin for what this does and does not establish.
_AD_HOC_PREFIX = "!!"


def classify_origin(compid: str | None) -> QueryOrigin:
    """Classify a query by the shape of its technical name. Name-based, never a stored flag."""
    return "ad_hoc" if compid is not None and compid.startswith(_AD_HOC_PREFIX) else "designed"


_VPROCTP_TO_TYPE: dict[str, VariableProcessingType] = {
    "1": "replacement_path",
    "3": "customer_exit",
    "4": "sap_exit",
    "5": "user_entry",
    "6": "authorization",
    "7": "hana_exit",
}
_VARTYP_TO_KIND: dict[str, VariableKind] = {
    "1": "characteristic",
    "2": "hierarchy_node",
    "3": "text",
    "4": "formula",
    "5": "hierarchy",
}
_VARIABLE_FLAG = "3"  # RSZTYPEFLAG: LOW/HIGH holds a variable reference
_MAX_ELEMENTS = 500
_MAX_DEPTH = 10
_TS_DIGITS = 8  # leading YYYYMMDD of an RSTIMESTMP decimal
_LANGUAGE = "E"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp_to_date(value: Any) -> date | None:
    """Parse the leading YYYYMMDD of an RSTIMESTMP decimal to a date."""
    if value is None:
        return None
    try:
        text = str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    digits = text.split(".", maxsplit=1)[0]
    if len(digits) < _TS_DIGITS or not digits.isdigit() or digits[:_TS_DIGITS] == "00000000":
        return None
    try:
        return datetime.strptime(digits[:_TS_DIGITS], "%Y%m%d").date()
    except ValueError:
        return None


_MAX_LINEAGE_IOBJ = 100


class QueriesRepository(Repository):
    """BEx query header, element tree, restrictions, variables, usage, and field lineage."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._lineage = LineageService(connection, capability, cache)

    # --- listing -------------------------------------------------------------------------

    def list_queries(
        self,
        *,
        provider: str | None = None,
        owner: str | None = None,
        origin: QueryOriginFilter = "all",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[QuerySummary], int] | UnsupportedResult:
        unsupported = self.require("query_dir")
        if unsupported is not None:
            return unsupported

        where = ["OBJSTAT = 'ACT'"]
        params: list[Any] = []
        # RSZCOMPDIR lists all reusable components; restrict to actual queries (root DEFTP='REP').
        rep_filter = self._query_only_filter()
        if rep_filter:
            where.append(rep_filter)
        if owner:
            where.append("OWNER = ?")
            params.append(owner)
        # Parameterised rather than an inline literal. "!" is not a LIKE metacharacter and is not
        # the dialect's escape character, so the pattern needs no escaping.
        if origin == "designed":
            where.append("COMPID NOT LIKE ?")
            params.append(f"{_AD_HOC_PREFIX}%")
        elif origin == "ad_hoc":
            where.append("COMPID LIKE ?")
            params.append(f"{_AD_HOC_PREFIX}%")
        if provider:
            compuids = self._compuids_for_provider(provider)
            if not compuids:
                return [], 0
            placeholders = ", ".join("?" for _ in compuids)
            where.append(f"COMPUID IN ({placeholders})")
            params.extend(compuids)

        base = self.dialect.build_select(
            columns=["COMPUID", "COMPID", "OWNER", "LASTUSED"],
            from_logical="query_dir",
            where=where,
            params=params,
            order_by=["COMPID"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        compuids = [str(r[0]) for r in rows]
        texts = self._element_texts(compuids)
        providers = self._providers_for(compuids)

        summaries: list[QuerySummary] = []
        for compuid, compid, owner_val, lastused in rows:
            cu = str(compuid)
            name = _clean(compid)
            summaries.append(
                QuerySummary(
                    compuid=cu,
                    compid=name,
                    description=texts.get(cu, (None, None))[1] or texts.get(cu, (None, None))[0],
                    provider=providers.get(cu),
                    owner=_clean(owner_val),
                    last_used=_timestamp_to_date(lastused),
                    origin=classify_origin(name),
                    provenance=self.provenance("query_dir", {"COMPUID": cu, "OBJVERS": "A"}),
                )
            )
        return summaries, total

    # --- full definition -----------------------------------------------------------------

    def get_query(self, identifier: str) -> Query | UnsupportedResult:
        """Full query definition. Cached (scope ``query``).

        The recursive RSZELTXREF walk plus the RSZELTTXT/RSZSELECT/RSZRANGE/RSZCALC joins are the
        most query-heavy read in the server, and a query definition only changes on re-activation.
        """
        return self.cached_model(
            "query",
            identifier,
            model=Query,
            build=lambda: self._get_query_uncached(identifier),
            # A not-found shell carries no COMPID; never cache "does not exist".
            cache_when=lambda query: query.compid is not None,
        )

    def _get_query_uncached(self, identifier: str) -> Query | UnsupportedResult:
        unsupported = self.require("query_dir", "element_dir", "element_xref")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return Query(
                compuid=identifier,
                active=False,
                caveats=["query not found"],
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid, owner, changed_by, lastused, objstat = header

        eltuids, edge_rows, truncated = self._element_tree(compuid)
        directory = self._element_directory(eltuids)
        texts = self._element_texts(list(eltuids))
        restrictions = self._restrictions(list(eltuids))

        elements = [
            self._build_element(uid, directory.get(uid), texts.get(uid), restrictions.get(uid, []))
            for uid in sorted(eltuids)
        ]
        edges = [
            QueryElementEdge(
                parent_uid=p,
                child_uid=c,
                role=_LAYTP_TO_ROLE.get(str(laytp).strip(), "other"),
                position=_as_int(posn),
                provenance=self.provenance("element_xref", {"SELTUID": p, "TELTUID": c}),
            )
            for p, c, laytp, posn in edge_rows
        ]
        providers = self._providers_list(compuid)
        variables = self._variables(elements, restrictions)

        return Query(
            compuid=compuid,
            compid=_clean(compid),
            description=(texts.get(compuid) or (None, None))[1]
            or (texts.get(compuid) or (None, None))[0],
            active=str(objstat).strip() == "ACT",
            provider=providers[0] if providers else None,
            providers=providers,
            owner=_clean(owner),
            last_changed_by=_clean(changed_by),
            last_used=_timestamp_to_date(lastused),
            elements=elements,
            edges=edges,
            variables=variables,
            truncated=truncated,
            caveats=["element tree capped"] if truncated else [],
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    def _build_element(
        self,
        uid: str,
        directory: tuple[Any, Any, Any] | None,
        text: tuple[str | None, str | None] | None,
        restrictions: list[Restriction],
    ) -> QueryElement:
        deftp, mapname, reusable = directory if directory else (None, None, None)
        return QueryElement(
            eltuid=uid,
            element_type=_DEFTP_TO_TYPE.get(str(deftp).strip(), "unknown"),
            name=_clean(mapname),
            description=(text or (None, None))[1] or (text or (None, None))[0],
            reusable=str(reusable).strip() == "X",
            restrictions=restrictions,
            provenance=self.provenance("element_dir", {"ELTUID": uid, "OBJVERS": "A"}),
        )

    # --- usage ---------------------------------------------------------------------------

    def get_query_usage(
        self, identifier: str, *, stale_days: int = 365
    ) -> QueryUsage | UnsupportedResult:
        unsupported = self.require("query_dir")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return QueryUsage(
                compuid=identifier,
                decommission_candidate=False,
                reason="query not found",
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid, _owner, _changed, lastused, _objstat = header
        last_used = _timestamp_to_date(lastused)
        if last_used is None:
            candidate, reason = True, "never used (no LASTUSED recorded)"
        else:
            age = (datetime.now().date() - last_used).days
            candidate = age > stale_days
            reason = f"last used {age} days ago" + (" (stale)" if candidate else "")
        return QueryUsage(
            compuid=compuid,
            compid=_clean(compid),
            last_used=last_used,
            decommission_candidate=candidate,
            reason=reason,
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    # --- field-level lineage -------------------------------------------------------------

    def get_query_lineage(self, identifier: str) -> QueryLineage | UnsupportedResult:
        """Per-InfoObject lineage toward the DataSource. Cached (scope ``query_lineage``)."""
        return self.cached_model(
            "query_lineage",
            identifier,
            model=QueryLineage,
            build=lambda: self._get_query_lineage_uncached(identifier),
            cache_when=lambda lineage: lineage.compid is not None,
        )

    def _get_query_lineage_uncached(self, identifier: str) -> QueryLineage | UnsupportedResult:
        unsupported = self.require("query_dir", "element_xref", "transformation")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return QueryLineage(
                compuid=identifier,
                caveats=["query not found"],
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid = str(header[0]), header[1]
        providers = self._providers_list(compuid)
        eltuids, _edges, truncated = self._element_tree(compuid)
        restrictions = self._restrictions(list(eltuids))
        directory = self._element_directory(eltuids)
        infoobjects = self._referenced_infoobjects(list(eltuids), restrictions)

        var_names: set[str] = {
            _clean(mapname)  # type: ignore[misc]
            for deftp, mapname, _reuse in directory.values()
            if str(deftp).strip() == "VAR" and _clean(mapname)
        }
        self._add_restriction_variables(restrictions, var_names)
        customer_exit = sorted(
            {v.name for v in self._fetch_variables(var_names) if v.is_customer_exit}
        )

        master = providers[0] if providers else None
        hops, reaches, advisory = self._provider_hops(master)
        paths = [
            FieldLineagePath(
                iobjnm=iobj,
                provider=master,
                hops=hops,
                reaches_datasource=reaches,
                has_routine_hop=advisory,
                provenance=self.provenance("element_range", {"IOBJNM": iobj}),
            )
            for iobj in sorted(infoobjects)[:_MAX_LINEAGE_IOBJ]
        ]
        caveats = [
            "field lineage is object-level (InfoObject -> provider -> upstream trace to "
            "DataSource); per-field transformation-rule detail is via bw_get_transformation",
            "customer-exit variable values resolve in ABAP at runtime and are not derivable",
        ]
        if truncated:
            caveats.append("element tree capped")
        return QueryLineage(
            compuid=compuid,
            compid=_clean(compid),
            providers=providers,
            paths=paths,
            customer_exit_variables=customer_exit,
            caveats=caveats,
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    def _referenced_infoobjects(
        self, eltuids: list[str], restrictions: dict[str, list[Restriction]]
    ) -> set[str]:
        objs: set[str] = {r.iobjnm for rs in restrictions.values() for r in rs}
        if eltuids and self.capability.is_available("element_select"):
            placeholders = ", ".join("?" for _ in eltuids)
            rows = self.select(
                self.dialect.build_select(
                    columns=["IOBJNM"],
                    from_logical="element_select",
                    where=[f"ELTUID IN ({placeholders})"],
                    params=list(eltuids),
                )
            )
            objs.update(str(r[0]).strip() for r in rows if _clean(r[0]))
        return {o for o in objs if o}

    def _provider_hops(self, provider: str | None) -> tuple[list[FieldLineageHop], bool, bool]:
        """Compact provider->DataSource summary from the lineage trace (full chain via bw_trace)."""
        if provider is None:
            return [], False, False
        hops: list[FieldLineageHop] = [
            FieldLineageHop(object_name=provider, object_type="provider", via="provider")
        ]
        trace = self._lineage.trace_to_source(provider, depth=6)
        if isinstance(trace, UnsupportedResult):
            return hops, False, False
        advisory = any(e.kind == "routine_lookup" for e in trace.graph.edges)
        for datasource in trace.datasources_reached:
            hops.append(
                FieldLineageHop(
                    object_name=datasource,
                    object_type="datasource",
                    via="datasource",
                    advisory=advisory,
                )
            )
        return hops, bool(trace.datasources_reached), advisory

    # --- shared helpers ------------------------------------------------------------------

    def _header(self, identifier: str) -> tuple[str, Any, Any, Any, Any, Any] | None:
        """Resolve a COMPID (technical name) or COMPUID to the RSZCOMPDIR header row."""
        for column in ("COMPID", "COMPUID"):
            rows = self.select(
                self.dialect.build_select(
                    columns=["COMPUID", "COMPID", "OWNER", "TSTPNM", "LASTUSED", "OBJSTAT"],
                    from_logical="query_dir",
                    where=[f"{column} = ?"],
                    params=[identifier],
                )
            )
            if rows:
                r = rows[0]
                return str(r[0]), r[1], r[2], r[3], r[4], r[5]
        return None

    def _element_tree(self, compuid: str) -> tuple[set[str], list[tuple[str, str, Any, Any]], bool]:
        eltuids: set[str] = {compuid}
        edges: list[tuple[str, str, Any, Any]] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(compuid, 0)])
        truncated = False
        while queue:
            parent, level = queue.popleft()
            if parent in visited or level >= _MAX_DEPTH:
                continue
            visited.add(parent)
            rows = self.select(
                self.dialect.build_select(
                    columns=["TELTUID", "LAYTP", "POSN"],
                    from_logical="element_xref",
                    where=["SELTUID = ?"],
                    params=[parent],
                    order_by=["POSN"],
                )
            )
            for child_raw, laytp, posn in rows:
                child = str(child_raw).strip()
                if not child:
                    continue
                edges.append((parent, child, laytp, posn))
                eltuids.add(child)
                if len(eltuids) >= _MAX_ELEMENTS:
                    truncated = True
                    break
                if child not in visited:
                    queue.append((child, level + 1))
            if truncated:
                break
        return eltuids, edges, truncated

    def _element_directory(self, eltuids: set[str]) -> dict[str, tuple[Any, Any, Any]]:
        if not eltuids or not self.capability.is_available("element_dir"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "DEFTP", "MAPNAME", "REUSABLE"],
                from_logical="element_dir",
                where=[f"ELTUID IN ({placeholders})"],
                params=list(eltuids),
            )
        )
        return {str(r[0]).strip(): (r[1], r[2], r[3]) for r in rows}

    def _element_texts(self, eltuids: list[str]) -> dict[str, tuple[str | None, str | None]]:
        if not eltuids or not self.capability.is_available("element_text"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "TXTSH", "TXTLG"],
                from_logical="element_text",
                where=["LANGU = ?", f"ELTUID IN ({placeholders})"],
                params=[_LANGUAGE, *eltuids],
            )
        )
        return {str(r[0]).strip(): (_clean(r[1]), _clean(r[2])) for r in rows}

    def _restrictions(self, eltuids: list[str]) -> dict[str, list[Restriction]]:
        if not eltuids or not self.capability.is_available("element_range"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "IOBJNM", "SIGN", "OPT", "LOW", "HIGH", "LOWFLAG", "HIGHFLAG"],
                from_logical="element_range",
                where=[f"ELTUID IN ({placeholders})"],
                params=list(eltuids),
            )
        )
        result: dict[str, list[Restriction]] = defaultdict(list)
        for eltuid, iobjnm, sign, opt, low, high, lowflag, highflag in rows:
            iobj = _clean(iobjnm)
            if iobj is None:
                continue
            result[str(eltuid).strip()].append(
                Restriction(
                    iobjnm=iobj,
                    sign=_clean(sign),
                    operator=_clean(opt),
                    low=_clean(low),
                    high=_clean(high),
                    low_is_variable=str(lowflag).strip() == _VARIABLE_FLAG,
                    high_is_variable=str(highflag).strip() == _VARIABLE_FLAG,
                    provenance=self.provenance(
                        "element_range", {"ELTUID": str(eltuid).strip(), "IOBJNM": iobj}
                    ),
                )
            )
        return result

    def _variables(
        self, elements: list[QueryElement], restrictions: dict[str, list[Restriction]]
    ) -> list[QueryVariable]:
        names: set[str] = {e.name for e in elements if e.element_type == "variable" and e.name}
        self._add_restriction_variables(restrictions, names)
        return self._fetch_variables(names)

    @staticmethod
    def _add_restriction_variables(
        restrictions: dict[str, list[Restriction]], names: set[str]
    ) -> None:
        for restr_list in restrictions.values():
            for r in restr_list:
                if r.low_is_variable and r.low:
                    names.add(r.low)
                if r.high_is_variable and r.high:
                    names.add(r.high)

    def _fetch_variables(self, names: set[str]) -> list[QueryVariable]:
        if not names or not self.capability.is_available("global_variable"):
            return []
        ordered = sorted(names)
        placeholders = ", ".join("?" for _ in ordered)
        rows = self.select(
            self.dialect.build_select(
                columns=["VNAM", "VARTYP", "VPROCTP", "IOBJNM", "VARINPUT"],
                from_logical="global_variable",
                where=[f"VNAM IN ({placeholders})"],
                params=ordered,
            )
        )
        variables: list[QueryVariable] = []
        for vnam, vartyp, vproctp, iobjnm, varinput in rows:
            name = _clean(vnam)
            if name is None:
                continue
            processing = _VPROCTP_TO_TYPE.get(str(vproctp).strip(), "unknown")
            variables.append(
                QueryVariable(
                    name=name,
                    iobjnm=_clean(iobjnm),
                    kind=_VARTYP_TO_KIND.get(str(vartyp).strip(), "unknown"),
                    processing_type=processing,
                    is_customer_exit=processing == "customer_exit",
                    input_ready=str(varinput).strip() == "X",
                    provenance=self.provenance("global_variable", {"VNAM": name, "OBJVERS": "A"}),
                )
            )
        return variables

    def _query_only_filter(self) -> str | None:
        """WHERE fragment restricting RSZCOMPDIR to queries (root element DEFTP='REP')."""
        if not self.capability.is_available("element_dir"):
            return None
        status = self.capability.table("element_dir")
        physical = status.resolved_name if status and status.resolved_name else "RSZELTDIR"
        schema = self.capability.abap_schema
        ref = f"{quote_ident(schema)}.{quote_ident(physical)}" if schema else quote_ident(physical)
        return f"COMPUID IN (SELECT ELTUID FROM {ref} WHERE DEFTP = 'REP' AND OBJVERS = 'A')"

    def _compuids_for_provider(self, provider: str) -> list[str]:
        if not self.capability.is_available("query_provider"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["COMPUID"],
                from_logical="query_provider",
                where=["INFOCUBE = ?"],
                params=[provider],
            )
        )
        return sorted({str(r[0]).strip() for r in rows if str(r[0]).strip()})

    def _providers_for(self, compuids: list[str]) -> dict[str, str]:
        """Master provider per query (first/IS_MASTER)."""
        if not compuids or not self.capability.is_available("query_provider"):
            return {}
        placeholders = ", ".join("?" for _ in compuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["COMPUID", "INFOCUBE", "IS_MASTER"],
                from_logical="query_provider",
                where=[f"COMPUID IN ({placeholders})"],
                params=list(compuids),
            )
        )
        result: dict[str, str] = {}
        for compuid, infocube, is_master in rows:
            cu, provider = str(compuid).strip(), _clean(infocube)
            if provider is None:
                continue
            if cu not in result or str(is_master).strip() == "X":
                result[cu] = provider
        return result

    def _providers_list(self, compuid: str) -> list[str]:
        if not self.capability.is_available("query_provider"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["INFOCUBE", "IS_MASTER"],
                from_logical="query_provider",
                where=["COMPUID = ?"],
                params=[compuid],
            )
        )
        masters = [_clean(r[0]) for r in rows if str(r[1]).strip() == "X" and _clean(r[0])]
        others = [_clean(r[0]) for r in rows if str(r[1]).strip() != "X" and _clean(r[0])]
        ordered: list[str] = []
        for name in [*masters, *others]:
            if name is not None and name not in ordered:
                ordered.append(name)
        return ordered

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0
