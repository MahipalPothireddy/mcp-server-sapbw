"""HANA-layer repository (B8).

Reads calc views from the HANA catalog (``SYS.VIEWS`` in the ``_SYS_BIC`` schema) and their
dependencies from ``SYS.OBJECT_DEPENDENCIES`` (DEPENDENCY_TYPE=1 = direct, validated live in B8),
resolving BW-generated ``/BIC/`` / ``/BI0/`` base tables back to BW objects (advisory, reusing the
routine parser's resolver). Builds the bidirectional BW<->HANA crossing table. The SYS catalog
views are OBJVERS-free and schema-qualified as ``SYS`` via the capability record.

BW also generates a HANA view per InfoProvider in the ABAP schema, named
``0BW:BIA:<PROVIDER>`` with internal nodes suffixed ``:<node>`` / ``.<node>`` (e.g.
``0BW:BIA:<CP>:J1.CALC.1``). Those views are what appear on the BW side of a ``bw_reads_hana``
crossing, so parsing the provider out of the name (:func:`_bw_view_provider`) and confirming its
type against the provider header tables (:meth:`HanaRepository.resolve_bw_view_providers`) is what
maps a calc view to the **CompositeProvider that consumes it** — the boundary hop BW's own
where-used lists do not show. The provider name is parsed from the naming convention; its *type* is
verified by lookup, not guessed.
"""

from __future__ import annotations

from typing import Any, Literal

from ..core.dialect import quote_ident
from ..models.completeness import BoundHit, Completeness, bounded
from ..models.evidence import evidence_for
from ..models.hana import (
    BaseTableRef,
    BwProviderView,
    CalcView,
    CalcViewCalculatedColumn,
    CalcViewColumnMapping,
    CalcViewDataSourceRef,
    CalcViewDefinition,
    CalcViewLineage,
    CalcViewNode,
    CalcViewParameter,
    CalcViewSemanticColumn,
    CalcViewType,
    CrossingDirection,
    HanaCrossing,
    HanaCrossingReport,
)
from ..models.provenance import Provenance, UnsupportedResult
from ..models.providers import classify_cube_type
from ..services.calcview_parser import CalcViewParseError, parse_calc_view
from ..services.routine_parser import _resolve_bw_table
from .base import Repository

# _SYS_BIC view types we treat as calc views (HIERARCHY views are BW hierarchy runtime, excluded).
_CALC_VIEW_TYPES = ("CALC", "JOIN", "OLAP")
_VIEW_TYPE_MAP: dict[str, CalcViewType] = {
    "CALC": "calc",
    "JOIN": "join",
    "OLAP": "olap",
    "HIERARCHY": "hierarchy",
}
_CALC_SCHEMA = "_SYS_BIC"
_DIRECT = 1  # SYS.OBJECT_DEPENDENCIES.DEPENDENCY_TYPE = direct dependency
_MAX_BASE_TABLES = 500
_MAX_CONSUMING = 5000
_MAX_PROVIDER_CONSUMERS = 200  # BW provider views resolved per calc-view lineage call

# BW-generated per-InfoProvider HANA view: '0BW:BIA:<PROVIDER>' plus optional ':node' / '.node'.
_BW_VIEW_PREFIX = "0BW:BIA:"

# Repository packages BW generates into. A view under one of these was produced by BW's own
# generation rather than modelled by a person.
_BW_REPO_PACKAGE_PREFIX = "system-local.bw"

# Largest activated definition this server will fetch and parse, in characters.
#
# Measured on the reference production system (589 activated calculation views): every *modelled*
# view fits in 630 KB, while BW-*generated* definitions average 4.7 MB and reach 323 MB. Fetching a
# 323 MB CLOB to answer a question about modelling logic would be a denial of service against the
# caller, and the answer would be BW's own generated projection rather than anything a person wrote.
# So the bound sits an order of magnitude above every modelled view and refuses the outliers by
# reporting their measured size, which is a fact rather than a failure.
_MAX_DEFINITION_CHARS = 4_000_000
# Provider header tables probed to confirm a parsed provider name, most specific first.
# (logical table, id column, kind) — 'cube_header' is refined by CUBETYPE.
_PROVIDER_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("composite_header", "HCPRNM", "compositeprovider"),
    ("adso_header", "ADSONM", "adso"),
    ("dso_header", "ODSOBJECT", "dso"),
    ("cube_header", "INFOCUBE", "infocube"),
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_bw_generated(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("/BIC/", "/BI0/"))


def _split_repo_name(view_name: str) -> tuple[str | None, str | None]:
    """A ``_SYS_BIC`` view name split into its repository package and object.

    Measured on the reference system: the runtime name is ``<PACKAGE_ID>/<OBJECT_NAME>``, and an
    internal node of the same design-time object appends further segments
    (``PKG.SUB/CV_A01/dp/Projection_1``). Package ids contain dots but never slashes, so the
    first slash is the boundary, and everything after the second segment is an internal node path
    pointing at the same activated definition.
    """
    cleaned = view_name.strip()
    if "/" not in cleaned:
        return None, None
    package, _, rest = cleaned.partition("/")
    object_name = rest.partition("/")[0]
    return (package.strip() or None), (object_name.strip() or None)


def _bw_view_provider(name: str) -> str | None:
    """Provider name inside a BW-generated view name (``0BW:BIA:<PROVIDER>[:node][.node]``).

    Returns ``None`` for anything that is not a ``0BW:BIA:`` view (including BW's internal helper
    views such as the lower-cased pruning views, which name no provider).
    """
    upper = name.strip().upper()
    if not upper.startswith(_BW_VIEW_PREFIX):
        return None
    rest = upper[len(_BW_VIEW_PREFIX) :]
    for separator in (":", "."):
        rest = rest.partition(separator)[0]
    return rest.strip() or None


class HanaRepository(Repository):
    """Calc-view catalog, calc-view -> base-table lineage, and BW<->HANA crossings."""

    # --- listing -------------------------------------------------------------------------

    def list_calc_views(
        self, *, bw_consuming_only: bool = False, limit: int = 100, offset: int = 0
    ) -> tuple[list[CalcView], int] | UnsupportedResult:
        unsupported = self.require("hana_views")
        if unsupported is not None:
            return unsupported

        types = ", ".join(f"'{t}'" for t in _CALC_VIEW_TYPES)
        where = ["SCHEMA_NAME = ?", f"VIEW_TYPE IN ({types})"]
        params: list[Any] = [_CALC_SCHEMA]
        consuming_filter = self._consuming_subquery()
        if bw_consuming_only and consuming_filter:
            where.append(f"VIEW_NAME IN ({consuming_filter})")
            params.append(self.capability.abap_schema)

        base = self.dialect.build_select(
            columns=["VIEW_NAME", "VIEW_TYPE"],
            from_logical="hana_views",
            where=where,
            params=params,
            order_by=["VIEW_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))

        consuming = set() if bw_consuming_only else self._consuming_names()
        views: list[CalcView] = []
        for name_raw, view_type in rows:
            name = _clean(name_raw)
            if name is None:
                continue
            views.append(
                CalcView(
                    name=name,
                    view_type=_VIEW_TYPE_MAP.get(str(view_type).strip(), "other"),
                    is_bw_consuming=True if bw_consuming_only else name in consuming,
                    provenance=self.provenance(
                        "hana_views", {"SCHEMA_NAME": _CALC_SCHEMA, "VIEW_NAME": name}
                    ),
                )
            )
        return views, total

    # --- calc-view lineage ---------------------------------------------------------------

    def get_calc_view_lineage(self, view_name: str) -> CalcViewLineage | UnsupportedResult:
        """Base tables and consuming providers for a calc view. Cached (scope ``calc_view``)."""
        return self.cached_model(
            "calc_view",
            view_name,
            model=CalcViewLineage,
            build=lambda: self._get_calc_view_lineage_uncached(view_name),
            # An unknown view resolves to nothing; a view activated later must not read as empty.
            cache_when=lambda lineage: bool(lineage.base_tables or lineage.consuming_bw_providers),
        )

    def _get_calc_view_lineage_uncached(
        self, view_name: str
    ) -> CalcViewLineage | UnsupportedResult:
        unsupported = self.require("object_dependencies")
        if unsupported is not None:
            return unsupported
        base_tables, truncated = self._base_tables(view_name)
        resolved: list[str] = []
        for entry in base_tables:
            if entry.resolved_object and entry.resolved_object not in resolved:
                resolved.append(entry.resolved_object)
        consumers, consumers_truncated = self._consuming_bw_providers(view_name)
        # Which cap bound, not merely that one did (D6). The two mean different things to a caller:
        # a capped base-table list understates what the view reads, a capped consumer list
        # understates who depends on it.
        hits: list[BoundHit] = []
        if truncated:
            hits.append(BoundHit(bound="row_cap", scope="base_tables", limit=_MAX_BASE_TABLES))
        if consumers_truncated:
            hits.append(
                BoundHit(
                    bound="semantic_limit",
                    scope="consuming_bw_providers",
                    limit=_MAX_PROVIDER_CONSUMERS,
                )
            )
        caveats = ["/BIC/ and /BI0/ base-table resolution to BW objects is advisory (naming-based)"]
        if consumers:
            caveats.append(
                "consuming BW providers are read from the '0BW:BIA:<PROVIDER>' generated views; "
                "the provider name is parsed from the view name and its type confirmed against the "
                "provider header tables (unverified entries are flagged)"
            )
        if truncated:
            caveats.append(f"base-table list capped at {_MAX_BASE_TABLES}")
        if consumers_truncated:
            caveats.append(f"consuming-provider list capped at {_MAX_PROVIDER_CONSUMERS}")
        return CalcViewLineage(
            view_name=view_name,
            base_tables=base_tables,
            resolved_bw_objects=resolved,
            consuming_bw_providers=consumers,
            completeness=Completeness(bounds=hits),
            caveats=caveats,
            provenance=self.provenance(
                "object_dependencies",
                {"DEPENDENT_SCHEMA_NAME": _CALC_SCHEMA, "DEPENDENT_OBJECT_NAME": view_name},
            ),
        )

    def _base_tables(self, view_name: str) -> tuple[list[BaseTableRef], bool]:
        """``(base tables, whether the row cap bound)`` for one calc view."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["BASE_SCHEMA_NAME", "BASE_OBJECT_NAME", "BASE_OBJECT_TYPE"],
                    from_logical="object_dependencies",
                    # Ordered because the read is capped: an unordered LIMIT returns an arbitrary
                    # subset, and possibly a different one per call (D8).
                    order_by=["BASE_SCHEMA_NAME", "BASE_OBJECT_NAME"],
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENT_OBJECT_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                    ],
                    params=[_CALC_SCHEMA, view_name, _DIRECT],
                ),
                limit=_MAX_BASE_TABLES + 1,
            )
        )
        truncated = len(rows) > _MAX_BASE_TABLES
        base_tables: list[BaseTableRef] = []
        seen: set[str] = set()
        for base_schema, base_object, base_type in rows[:_MAX_BASE_TABLES]:
            table = _clean(base_object)
            if table is None or table in seen:
                continue
            seen.add(table)
            is_bw = _is_bw_generated(table)
            obj, kind, _confidence = _resolve_bw_table(table) if is_bw else (None, None, None)
            base_tables.append(
                BaseTableRef(
                    table=table,
                    schema_name=_clean(base_schema),
                    object_type=_clean(base_type),
                    is_bw_generated=is_bw,
                    resolved_object=obj,
                    resolved_kind=kind,
                    provenance=self.provenance(
                        "object_dependencies",
                        {"DEPENDENT_OBJECT_NAME": view_name, "BASE_OBJECT_NAME": table},
                    ),
                )
            )
        return base_tables, truncated

    # --- calc-view logic (the activated definition) ---------------------------------------

    def get_calc_view_definition(self, view_name: str) -> CalcViewDefinition | UnsupportedResult:
        """What a calc view *does*: joins, filters, calculated columns, parameters, aggregation.

        Cached like the other structural extracts, and only when something was actually read - a
        view activated after a failed lookup must not stay cached as unreadable.
        """
        unsupported = self.require("calc_view_definition")
        if unsupported is not None:
            return unsupported
        return self.cached_model(
            "calc_view_logic",
            view_name,
            model=CalcViewDefinition,
            build=lambda: self._calc_view_definition_uncached(view_name),
            cache_when=lambda definition: definition.parsed,
        )

    def _calc_view_definition_uncached(self, view_name: str) -> CalcViewDefinition:
        package_id, object_name = _split_repo_name(view_name)
        provenance = self.provenance(
            "calc_view_definition",
            {"PACKAGE_ID": package_id or "", "OBJECT_NAME": object_name or ""},
        )
        base = CalcViewDefinition(
            view_name=view_name,
            package_id=package_id,
            object_name=object_name,
            is_bw_generated=bool(package_id and package_id.startswith(_BW_REPO_PACKAGE_PREFIX)),
            provenance=provenance,
        )
        if package_id is None or object_name is None:
            base.unparsed_reason = (
                f"{view_name!r} does not decompose into a repository package and object name "
                "(expected '<PACKAGE_ID>/<OBJECT_NAME>'), so no activated definition can be located"
            )
            return base

        header = self._definition_header(package_id, object_name)
        if header is None:
            base.unparsed_reason = (
                "no activated definition exists for this view in the repository. A BW-generated "
                "runtime view can exist in _SYS_BIC without repository content; use "
                "bw_get_calc_view_lineage for its base tables and consumers."
            )
            return base
        suffix, size, changed_at, changed_by = header
        base.definition_bytes = size
        base.changed_at = base.changed_at or changed_at
        if size > _MAX_DEFINITION_CHARS:
            base.unparsed_reason = (
                f"the activated definition is {size:,} characters, above the "
                f"{_MAX_DEFINITION_CHARS:,} bound this server fetches. It was not read, so nothing "
                "here is a statement about its logic. Definitions this large are BW-generated "
                "rather than modelled; "
                "bw_get_calc_view_lineage reports their base tables and consumers without the CLOB."
            )
            base.caveats.append(f"definition not read: {size:,} characters exceeds the bound")
            return base

        definition = self._definition_body(package_id, object_name, suffix)
        if definition is None:
            base.unparsed_reason = "the activated definition could not be fetched"
            return base
        try:
            parsed = parse_calc_view(definition)
        except CalcViewParseError as exc:
            base.unparsed_reason = exc.reason
            return base
        return self._to_definition(base, parsed, changed_by=changed_by)

    def _definition_header(
        self, package_id: str, object_name: str
    ) -> tuple[str, int, str | None, str | None] | None:
        """``(suffix, size, changed_at, changed_by)`` without fetching the CLOB.

        The size is read first, deliberately and always: it is what decides whether the definition
        can be fetched at all, and asking afterwards would mean the CLOB had already crossed the
        wire. ``attributeview`` is accepted alongside ``calculationview`` because the reference
        system carries 407 of them and they are the same kind of modelling artefact.
        """
        rows = self.select(
            self.dialect.build_select(
                columns=["OBJECT_SUFFIX", "LENGTH(CDATA)", "ACTIVATED_AT", "ACTIVATED_BY"],
                from_logical="calc_view_definition",
                where=[
                    "PACKAGE_ID = ?",
                    "OBJECT_NAME = ?",
                    "OBJECT_SUFFIX IN ('calculationview', 'attributeview')",
                ],
                params=[package_id, object_name],
                order_by=["OBJECT_SUFFIX"],
            )
        )
        if not rows:
            return None
        suffix, size, activated_at, activated_by = rows[0]
        return (
            str(suffix).strip(),
            int(size) if size is not None else 0,
            _clean(activated_at),
            _clean(activated_by),
        )

    def _definition_body(self, package_id: str, object_name: str, suffix: str) -> str | None:
        rows = self.select(
            self.dialect.build_select(
                columns=["CDATA"],
                from_logical="calc_view_definition",
                where=["PACKAGE_ID = ?", "OBJECT_NAME = ?", "OBJECT_SUFFIX = ?"],
                params=[package_id, object_name, suffix],
            )
        )
        if not rows or rows[0][0] is None:
            return None
        return str(rows[0][0])

    def _to_definition(
        self, base: CalcViewDefinition, parsed: Any, *, changed_by: str | None
    ) -> CalcViewDefinition:
        root = parsed.root
        base.description = root.get("description")
        base.changed_at = root.get("changed_at") or base.changed_at
        base.data_category = root.get("data_category")
        base.output_view_type = root.get("output_view_type")
        base.schema_version = root.get("schema_version")
        base.scenario_type = root.get("scenario_type")
        base.final_node = root.get("final_node")
        base.applies_analytic_privilege = bool(root.get("checks_privileges"))
        base.parsed = True

        for source in parsed.data_sources:
            table = source.get("column_object") or ""
            is_bw = _is_bw_generated(table)
            obj, kind, _confidence = _resolve_bw_table(table) if is_bw else (None, None, None)
            base.data_sources.append(
                CalcViewDataSourceRef(
                    id=source["id"],
                    source_type=source.get("source_type"),
                    schema_name=source.get("schema_name"),
                    column_object=source.get("column_object"),
                    resource_uri=source.get("resource_uri"),
                    resolved_object=obj,
                    resolved_kind=kind,
                    provenance=base.provenance
                    if isinstance(base.provenance, Provenance)
                    else base.provenance[0],
                )
            )

        counts: dict[str, int] = {}
        for node in parsed.nodes:
            counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
            base.nodes.append(
                CalcViewNode(
                    id=node["id"],
                    node_type=node["node_type"],
                    raw_type=node.get("raw_type"),
                    join_type=node.get("join_type"),
                    cardinality=node.get("cardinality"),
                    join_order=node.get("join_order"),
                    join_attributes=list(node.get("join_attributes") or []),
                    inputs=list(node.get("inputs") or []),
                    mappings=[
                        CalcViewColumnMapping(
                            target=m["target"],
                            source=m.get("source"),
                            value=m.get("value"),
                            kind=m.get("kind"),
                            from_node=m.get("from_node"),
                        )
                        for m in node.get("mappings") or []
                    ],
                    filter_expression=node.get("filter"),
                )
            )
        base.node_counts = dict(sorted(counts.items()))

        base.calculated_columns = [
            CalcViewCalculatedColumn(
                name=column["name"],
                formula=column.get("formula"),
                datatype=column.get("datatype"),
                length=column.get("length"),
                expression_language=column.get("expression_language"),
                node=column.get("node"),
            )
            for column in parsed.calculated_columns
        ]
        base.semantic_columns = [
            CalcViewSemanticColumn(
                name=column["name"],
                role=column["role"],
                description=column.get("description"),
                aggregation=column.get("aggregation"),
                measure_type=column.get("measure_type"),
                is_key=bool(column.get("is_key")),
                calculated=bool(column.get("calculated")),
                origin_node=column.get("origin_node"),
                origin_column=column.get("origin_column"),
                formula=column.get("formula"),
            )
            for column in parsed.attributes
        ]
        base.input_parameters = [
            CalcViewParameter(
                name=parameter["name"],
                is_input_parameter=bool(parameter.get("is_input_parameter")),
                description=parameter.get("description"),
                datatype=parameter.get("datatype"),
                length=parameter.get("length"),
                mandatory=parameter.get("mandatory"),
                selection_type=parameter.get("selection_type"),
            )
            for parameter in parsed.parameters
        ]
        base.filters = list(parsed.filters)
        base.unrecognised_elements = list(parsed.unrecognised_elements)
        if parsed.truncated:
            # One of the parser's per-section bounds stopped it; the section is named there.
            base.completeness = bounded("row_cap", scope="definition_sections")

        base.evidence = evidence_for("calc_view_definition", "activated_repository")
        base.caveats.append(
            "read from the activated definition in _SYS_REPO, which is what the modeller saved. An "
            "inactive change is not visible here, and the runtime view in _SYS_BIC is what queries "
            "actually execute."
        )
        if any(source.resolved_object for source in base.data_sources):
            base.caveats.append(
                "/BIC/ and /BI0/ data sources are resolved to BW objects by naming convention "
                "(advisory), the same reading bw_get_calc_view_lineage uses"
            )
        if base.truncated:
            base.caveats.append(
                "the definition is larger than the per-section bounds; see truncated"
            )
        if changed_by:
            base.caveats.append(f"last activated by {changed_by}")
        return base

    # --- crossings -----------------------------------------------------------------------

    def get_hana_crossings(
        self, *, calc_view: str | None = None, limit: int = 100, offset: int = 0
    ) -> HanaCrossingReport | UnsupportedResult:
        unsupported = self.require("object_dependencies")
        if unsupported is not None:
            return unsupported
        abap = self.capability.abap_schema

        hana_reads = self._hana_reads_bw(abap, calc_view, limit, offset)
        bw_reads = self._bw_reads_hana(abap, calc_view, limit, offset)
        crossings = hana_reads[0] + bw_reads[0]
        caveats = ["only direct dependencies (DEPENDENCY_TYPE=1) are included"]
        # Each direction pages independently, so they are reported independently: a caller paging
        # one of them needs to know which side still has rows behind it (D6).
        hits = [
            BoundHit(bound="page_limit", scope=scope, limit=limit)
            for scope, exhausted in (
                ("hana_reads_bw", hana_reads[2]),
                ("bw_reads_hana", bw_reads[2]),
            )
            if exhausted
        ]
        if hits:
            caveats.append("crossing list truncated per direction; totals are exact")
        return HanaCrossingReport(
            crossings=crossings,
            total_count=hana_reads[1] + bw_reads[1],
            hana_reads_bw_count=hana_reads[1],
            bw_reads_hana_count=bw_reads[1],
            completeness=Completeness(bounds=hits),
            caveats=caveats,
        )

    def _hana_reads_bw(
        self, abap: str, calc_view: str | None, limit: int, offset: int
    ) -> tuple[list[HanaCrossing], int, bool]:
        where = [
            "DEPENDENT_SCHEMA_NAME = ?",
            "BASE_SCHEMA_NAME = ?",
            "DEPENDENCY_TYPE = ?",
            "(BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')",
        ]
        params: list[Any] = [_CALC_SCHEMA, abap, _DIRECT]
        if calc_view:
            where.append("DEPENDENT_OBJECT_NAME = ?")
            params.append(calc_view)
        base = self.dialect.build_select(
            columns=["DEPENDENT_OBJECT_NAME", "BASE_OBJECT_NAME", "BASE_OBJECT_TYPE"],
            from_logical="object_dependencies",
            where=where,
            params=params,
            order_by=["DEPENDENT_OBJECT_NAME", "BASE_OBJECT_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        # This direction is filtered to /BIC/ and /BI0/ base tables in SQL, so no BW provider views.
        crossings = [
            self._crossing("hana_reads_bw", str(dep), str(base_obj), str(base_type), {})
            for dep, base_obj, base_type in rows
        ]
        return crossings, total, total > offset + limit

    def _bw_reads_hana(
        self, abap: str, calc_view: str | None, limit: int, offset: int
    ) -> tuple[list[HanaCrossing], int, bool]:
        where = ["BASE_SCHEMA_NAME = ?", "DEPENDENT_SCHEMA_NAME = ?", "DEPENDENCY_TYPE = ?"]
        params: list[Any] = [_CALC_SCHEMA, abap, _DIRECT]
        if calc_view:
            where.append("BASE_OBJECT_NAME = ?")
            params.append(calc_view)
        base = self.dialect.build_select(
            columns=["BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME", "DEPENDENT_OBJECT_TYPE"],
            from_logical="object_dependencies",
            where=where,
            params=params,
            order_by=["BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        # The BW side here is a BW-generated object; '0BW:BIA:<PROVIDER>' views resolve to the
        # consuming InfoProvider (this is the calc-view -> CompositeProvider hop).
        provider_views = self.resolve_bw_view_providers([str(dep) for _, dep, _ in rows])
        crossings = [
            self._crossing("bw_reads_hana", str(base_obj), str(dep), str(dep_type), provider_views)
            for base_obj, dep, dep_type in rows
        ]
        return crossings, total, total > offset + limit

    def _crossing(
        self,
        direction: CrossingDirection,
        hana_object: str,
        bw_object: str,
        object_type: str,
        provider_views: dict[str, BwProviderView],
    ) -> HanaCrossing:
        resolution: Literal["bic_table", "bw_provider_view", "unresolved"] = "unresolved"
        resolved: str | None = None
        kind: str | None = None
        if _is_bw_generated(bw_object):
            resolved, kind, _confidence = _resolve_bw_table(bw_object)
            if resolved is not None:
                resolution = "bic_table"
        elif (view := provider_views.get(bw_object)) is not None:
            resolved, kind = view.provider, view.resolved_kind
            resolution = "bw_provider_view"
        return HanaCrossing(
            direction=direction,
            hana_object=hana_object,
            bw_object=bw_object,
            bw_object_resolved=resolved,
            bw_object_kind=kind,
            resolution=resolution,
            object_type=_clean(object_type),
            provenance=self.provenance(
                "object_dependencies", {"HANA": hana_object, "BW": bw_object}
            ),
        )

    # --- BW provider views (0BW:BIA:) ----------------------------------------------------

    def _consuming_bw_providers(self, view_name: str) -> tuple[list[BwProviderView], bool]:
        """InfoProviders whose generated views read this calc view (the BW side of the boundary)."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DEPENDENT_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "BASE_SCHEMA_NAME = ?",
                        "BASE_OBJECT_NAME = ?",
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                        f"DEPENDENT_OBJECT_NAME LIKE '{_BW_VIEW_PREFIX}%'",
                    ],
                    params=[
                        _CALC_SCHEMA,
                        view_name,
                        self.capability.abap_schema,
                        _DIRECT,
                    ],
                    order_by=["DEPENDENT_OBJECT_NAME"],
                ),
                limit=_MAX_PROVIDER_CONSUMERS + 1,
            )
        )
        truncated = len(rows) > _MAX_PROVIDER_CONSUMERS
        names = [str(r[0]) for r in rows[:_MAX_PROVIDER_CONSUMERS]]
        resolved = self.resolve_bw_view_providers(names)
        # One entry per provider: the internal ':J1.CALC.n' nodes all name the same provider.
        by_provider: dict[str, BwProviderView] = {}
        for name in names:
            view = resolved.get(name)
            if view is not None:
                by_provider.setdefault(view.provider, view)
        return sorted(by_provider.values(), key=lambda v: v.provider), truncated

    def resolve_bw_view_providers(self, view_names: list[str]) -> dict[str, BwProviderView]:
        """Resolve ``0BW:BIA:`` view names to the InfoProviders that own them.

        The provider name is parsed from the view name; its **type** is then confirmed against the
        provider header tables present on this release, so the returned kind is verified metadata
        rather than a naming guess. Views whose provider name matches no header row are still
        returned, with ``resolved_kind=None`` and ``verified=False``.
        """
        parsed: dict[str, str] = {}
        for view in view_names:
            provider = _bw_view_provider(view)
            if provider is not None:
                parsed[view] = provider
        if not parsed:
            return {}
        kinds = self._provider_kinds(sorted(set(parsed.values())))
        resolved: dict[str, BwProviderView] = {}
        for view, provider in parsed.items():
            kind = kinds.get(provider)
            resolved[view] = BwProviderView(
                view_name=view,
                provider=provider,
                resolved_kind=kind,
                verified=kind is not None,
                provenance=self.provenance(
                    "object_dependencies", {"BW_VIEW": view, "PROVIDER": provider}
                ),
            )
        return resolved

    def _provider_kinds(self, providers: list[str]) -> dict[str, str]:
        """Confirm provider names against the header tables, returning name -> provider kind."""
        remaining = list(providers)
        kinds: dict[str, str] = {}
        for logical, id_column, kind in _PROVIDER_SOURCES:
            if not remaining or not self.capability.is_available(logical):
                continue
            is_cube = logical == "cube_header"
            columns = [id_column, "CUBETYPE"] if is_cube else [id_column]
            placeholders = ", ".join("?" for _ in remaining)
            rows = self.select(
                self.dialect.build_select(
                    columns=columns,
                    from_logical=logical,
                    where=[f"{id_column} IN ({placeholders})"],
                    params=list(remaining),
                )
            )
            for row in rows:
                name = _clean(row[0])
                if name is None or name in kinds:
                    continue
                kinds[name] = classify_cube_type(row[1]) if is_cube else kind
            remaining = [name for name in remaining if name not in kinds]
        return kinds

    # --- helpers -------------------------------------------------------------------------

    def _consuming_subquery(self) -> str | None:
        """A ``SELECT DEPENDENT_OBJECT_NAME ...`` fragment naming BW-consuming calc views."""
        status = self.capability.table("object_dependencies")
        if status is None or not status.present or status.resolved_name is None:
            return None
        ref = f"{quote_ident('SYS')}.{quote_ident(status.resolved_name)}"
        return (
            f"SELECT DEPENDENT_OBJECT_NAME FROM {ref} "
            f"WHERE DEPENDENT_SCHEMA_NAME = '{_CALC_SCHEMA}' AND BASE_SCHEMA_NAME = ? "
            f"AND DEPENDENCY_TYPE = {_DIRECT} "
            "AND (BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')"
        )

    def _consuming_names(self) -> set[str]:
        if not self.capability.is_available("object_dependencies"):
            return set()
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DEPENDENT_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    order_by=["DEPENDENT_OBJECT_NAME"],  # capped read; see D8
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "BASE_SCHEMA_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                        "(BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')",
                    ],
                    params=[_CALC_SCHEMA, self.capability.abap_schema, _DIRECT],
                ),
                limit=_MAX_CONSUMING,
            )
        )
        return {str(r[0]).strip() for r in rows if _clean(r[0])}

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0
