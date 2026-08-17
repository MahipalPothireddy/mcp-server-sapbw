"""Capability resolution — the mechanism that makes the server portable.

On first connect to a profile the resolver discovers, once, what actually exists: the ABAP schema,
the BW release, which metadata tables are present (existence tier) or must be discovered by pattern
(discover tier), which object-model variants are in use, the HANA repository style, and the
process-log retention window. Repositories consult the resulting :class:`CapabilityRecord` before
building any SQL, so no tool ever queries a table that does not exist on the connected release
(mission Section 3, Rules 2 and 7).

The discovery SQL below is provisional and is validated/corrected against a live system in build
prompt B2. The resolver's *logic* — existence vs. discover, object-model detection, gating — is what
B1 establishes and tests offline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

from ..models.capability import CapabilityRecord, HanaRepoStyle, ProbeOutcome, TableStatus
from ..models.provenance import UnsupportedResult
from .connection import is_permission_denied
from .dialect import quote_ident
from .profiles import ABAP_SCHEMA_AUTO, Profile


class CapabilityError(Exception):
    """Capability discovery could not complete (e.g. the ABAP schema could not be resolved)."""


class SupportsSelect(Protocol):
    """Anything that can run a read-only query (ReadOnlyConnection satisfies this)."""

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


# EXISTENCE-tier ABAP dictionary tables: logical name -> canonical physical name.
ABAP_TABLES: dict[str, str] = {
    # process chains and scheduling
    "chain_edges": "RSPCCHAIN",
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "variant": "RSPCVARIANT",
    "variant_text": "RSPCVARIANTT",
    "log_chain": "RSPCLOGCHAIN",
    "process_log": "RSPCPROCESSLOG",
    "log_messages": "RSPCLOGS",
    "job_header": "TBTCO",
    "job_steps": "TBTCP",
    "job_schedule": "TBTCS",
    # data flow and transformations
    "transformation": "RSTRAN",
    "transformation_field": "RSTRANFIELD",
    "transformation_rule": "RSTRANRULE",
    # B2 finding (7.50): the mission's RSTRANSTEP/RSTRANSTEPRULE do not exist. The rule-to-step link
    # is RSTRANRULESTEP; fine-grained step detail is a typed family (RSTRANSTEP<TYPE>: MAP, ROUT,
    # MASTER, ODSO, ...) which B5 (transformations) will validate and read as needed.
    "transformation_rule_step": "RSTRANRULESTEP",
    "transformation_step_rout": "RSTRANSTEPROUT",  # rule/step -> routine CODEID (field routines)
    # Typed rule-step tables (all confirmed live on 7.50). These record the lookups and constants
    # BW itself knows about, so they are exact where the routine parser is only heuristic.
    "transformation_step_const": "RSTRANSTEPCNST",  # the literal a CONSTANT rule writes
    "transformation_step_master": "RSTRANSTEPMASTER",  # declared master-data (InfoObject) lookups
    "transformation_step_dso": "RSTRANSTEPODSO",  # declared classic-DSO lookups
    "transformation_step_adso": "RSTRANSTEPADSO",  # declared advanced-DSO lookups
    "transformation_seg": "RSTRANSEG",
    "routine_source": "RSAABAP",  # ABAP source lines; NOTE prefix RSA -> no auto OBJVERS injection
    "dtp": "RSBKDTP",
    "dtp_request": "RSBKREQUEST",
    "infopackage": "RSLDPIO",
    "infopackage_selection": "RSLDPSEL",
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
    # Source-system registry. NOTE: keyed by SLOGSYS (sender) and carrying OBJSTAT, not OBJVERS.
    "source_system": "RSBASIDOC",
    "request_status": "RSSTATMANPART",
    # The BW 7.4+ TSN request framework, and the ONLY place an Advanced DSO's load history lives.
    # RSSTATMANPART records classic DSOs, InfoCubes and InfoObject master-data loads; it holds no
    # ADSO rows at all. Measured on the reference system: 248 active ADSOs, 0 rows in
    # RSSTATMANPART, 216 distinct DATATARGET values in RSPMREQUEST of which every one is an ADSO
    # (TLOGO = 'ADSO' on all 2,105,832 rows). Provider currency is therefore per-object-model, not
    # universal - one ledger reader was an assumption about BW and it was wrong for the object
    # model most of a modern landscape uses.
    "adso_request": "RSPMREQUEST",
    # providers and descriptions
    "dso_header": "RSDODSO",
    "dso_text": "RSDODSOT",
    "dso_field": "RSDODSOIOBJ",
    "cube_header": "RSDCUBE",
    "cube_text": "RSDCUBET",
    "cube_field": "RSDCUBEIOBJ",
    "multiprovider_part": "RSDCUBEMULTI",
    # Advanced DSO (RSOADSO*) and CompositeProvider (RSOHCPR*): mission Appendix A flags these as
    # discover-tier (names vary by release). Confirmed live in B4 on 7.50 and kept here as
    # existence-tier (still runtime-confirmed via DD02L); the discover-tier families 'adso' /
    # 'composite_provider' remain the object-model presence check, and require()/is_available()
    # gate the repositories when a table is absent on some other release.
    "adso_header": "RSOADSO",
    "adso_text": "RSOADSOT",  # HANA-shape: DESCRIPTION/QUICK_INFO keyed by COLNAME
    "adso_keyfields": "RSOADSOKEYFIELDS",  # NOTE: no OBJVERS column (see dialect no-OBJVERS set)
    "composite_header": "RSOHCPR",  # composition/part-providers live in XML_DEF (LOB)
    "composite_text": "RSOHCPRT",  # HANA-shape: DESCRIPTION/QUICK_INFO keyed by COLNAME
    "infoobject": "RSDIOBJ",
    "infoobject_text": "RSDIOBJT",
    # InfoArea hierarchy and its texts. Every provider header already carries an INFOAREA code, and
    # without these it stays exactly that: BW's own business grouping, read and unresolved. RSDAREA
    # holds the parent link (PARENT_AREA), so an area resolves to a path rather than a flat label.
    "info_area": "RSDAREA",
    "info_area_text": "RSDAREAT",
    "characteristic": "RSDCHA",
    "keyfigure": "RSDKYF",
    "attribute": "RSDBCHATR",
    "nav_attribute": "RSDATRNAV",
    # BEx queries
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "element_select": "RSZSELECT",
    "element_range": "RSZRANGE",
    "element_calc": "RSZCALC",
    "global_variable": "RSZGLOBV",
    "element_prop": "RSZELTPROP",
    "report_dir": "RSRREPDIR",
    # BW 3.x dataflow (transfer rules / update rules). Still load-bearing on 7.50: on the reference
    # system 1,090 DataSources route through a transfer structure with no 7.x transformation at all,
    # almost all of them master data. Ignoring these tables makes that whole layer invisible.
    # RSTS keys on TRANSTRU (TSTPNM is the transport package, not a join key) and has no OLTPSOURCE;
    # RSISOSMAP is the DataSource <-> InfoSource <-> transfer-structure bridge.
    "transfer_structure": "RSTS",
    "transfer_structure_field": "RSTSFIELD",
    "transfer_rule": "RSTSRULES",
    "infosource_map": "RSISOSMAP",
    "infosource_header": "RSIS",
    "infosource_text": "RSIST",
    "comm_structure": "RSKS",
    "comm_structure_field": "RSKSFIELDNEW",
    "update_rule": "RSUPDINFO",
    "update_rule_keyfigure": "RSUPDDAT",
    "update_rule_key": "RSUPDKEY",
    "update_rule_routine": "RSUPDROUT",
    "routine_source_3x": "RSAROUT",
    "routine_text_3x": "RSAROUTT",
    # dictionary
    "dict_tables": "DD02L",
    "dict_tables_text": "DD02T",
    "dict_columns": "DD03L",
    "dict_dataelement_text": "DD04T",
    # Analysis authorisations (BW row-level security). Deliberately EXISTENCE-tier by canonical name
    # AND re-checked per query, because a locked-down reporting user frequently cannot read these at
    # all: they are the authorisation model itself. An absent or unreadable table is a documented
    # gap, never an assumption that no authorisations exist.
    "auth_values": "RSECVAL",  # the permitted value ranges, per authorisation and characteristic
    "auth_hierarchy": "RSECHIE",  # hierarchy-node authorisations
    "auth_user": "RSECUSERAUTH",  # authorisation -> user assignment
    "auth_text": "RSECTXT",  # authorisation descriptions
    # Classic ABAP authorisation objects, for the S_RS_* / S_RS_AUTH side of the picture.
    "abap_user_profile": "UST04",  # user -> profile
    "abap_profile_auth": "UST10S",  # profile -> authorisation
    "abap_user_role": "AGR_USERS",  # user -> role
    "abap_role_auth": "AGR_1251",  # role -> authorisation object + field values
}

# EXISTENCE-tier HANA catalog objects (all in SYS).
HANA_VIEWS: dict[str, str] = {
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "hana_views": "VIEWS",
    "hana_view_columns": "VIEW_COLUMNS",
    "hana_columns": "COLUMNS",
    "cs_tables": "M_CS_TABLES",
}

# DISCOVER-tier families: logical group -> DD02L LIKE pattern. Names vary by release and must be
# discovered, never assumed (mission Appendix A).
DISCOVER_PATTERNS: dict[str, str] = {
    "adso": "RSOADSO%",
    "composite_provider": "RSOHCPR%",
    "query_stats": "RSDDSTAT%",
    "transformation_text": "RSTRAN%",
    "extractor": "ROOSOURCE",  # exact, but validated at runtime (lives with extractor metadata)
    "extractor_field": "ROOSFIELD",
    # The RSEC* family varies across releases and carries generation/DAP tables whose names are not
    # stable. Discovering the family tells the security repository what is actually available before
    # it builds any SQL.
    "analysis_auth": "RSEC%",
}


# Length of a classic SAP_BW release code like "750" -> formatted as "7.50".
_SAP_BW_RELEASE_LEN = 3


def _format_release(component: str, release: str) -> str:
    rel = release.strip()
    if component in ("DW4CORE", "BW4CORE"):
        return f"BW/4HANA {rel}"
    if rel.isdigit() and len(rel) == _SAP_BW_RELEASE_LEN:
        return f"BW {rel[0]}.{rel[1:]}"
    return f"{component} {rel}".strip()


# One year of process-chain log is sufficient for analysis (owner decision). We do not track the
# full historical span; the runtime analysis window is capped here and used to bound runtime
# queries.
MAX_RUNTIME_WINDOW_DAYS = 365


def _days_since(yyyymmdd: str) -> int:
    try:
        parsed = datetime.strptime(yyyymmdd.strip(), "%Y%m%d").replace(tzinfo=UTC).date()
    except (ValueError, AttributeError):
        return 0
    return max((date.today() - parsed).days, 0)


def unsupported_result(
    record: CapabilityRecord,
    missing: Sequence[str],
    *,
    alternative: str | None = None,
    detail: str | None = None,
) -> UnsupportedResult:
    """Build a structured "cannot answer that here" result for a repository to return.

    Two causes reach this function and they are not the same answer. If any of ``missing`` was
    *refused* rather than observed absent, the result says so and carries the ``permission_denied``
    code - which makes it non-retryable and points at a grant. Reporting a missing privilege as a
    release limitation, with a release name attached, sends a customer to plan an upgrade for
    something one ``GRANT SELECT`` would fix.
    """
    listed = ", ".join(missing)
    unreadable = [name for name in missing if record.is_unreadable(name)]
    if unreadable:
        blocked = ", ".join(unreadable)
        return UnsupportedResult(
            code="permission_denied",
            missing=list(missing),
            release=record.bw_release,
            alternative=alternative,
            detail=detail
            or (
                f"{blocked} could not be read by the connected user, so whether "
                f"{record.bw_release} carries it is unknown - this is not a statement that the "
                "release lacks it. Run bw_access_report for the exact grant required."
            ),
        )
    return UnsupportedResult(
        missing=list(missing),
        release=record.bw_release,
        alternative=alternative,
        detail=detail or f"{listed} not available on {record.bw_release}",
    )


#: Outcome of a probe attempt: the rows if it ran, and why it did not if it failed. ``None`` rows
#: with a ``denied``/``failed`` outcome is the case a boolean could not express - the difference
#: between "the catalog says no" and "the catalog would not tell me".
_ProbeAttempt = tuple[list[tuple[Any, ...]] | None, ProbeOutcome]


def _attempt(
    connection: SupportsSelect, sql: str, parameters: Sequence[Any] | None = None
) -> _ProbeAttempt:
    """Run a discovery probe, classifying a failure as refused or merely broken.

    Every probe goes through here so no discovery read can silently turn a refusal into an
    observed absence. ``present``/``absent`` is the caller's decision from the rows; this function
    only reports whether it got to see any.
    """
    try:
        rows = connection.execute_select(sql, parameters)
    except Exception as exc:
        return None, ("denied" if is_permission_denied(exc) else "failed")
    return rows, "present"


class CapabilityResolver:
    """Runs discovery once per profile and produces a :class:`CapabilityRecord`."""

    def resolve(self, profile: Profile, connection: SupportsSelect) -> CapabilityRecord:
        schema = self._resolve_schema(profile, connection)
        release = self._detect_release(schema, connection)
        tables = self._probe_abap_existence(schema, connection)
        tables.update(self._probe_hana_objects(connection))
        discovered = self._discover(schema, connection)
        tables.update(discovered.table_status)
        self._populate_row_counts(schema, tables, connection)
        object_models, undetermined_models = self._detect_object_models(tables, discovered)
        hana_repo = self._detect_hana_repo_style(connection)
        retention = self._measure_retention(schema, tables, connection)

        return CapabilityRecord(
            system=profile.name,
            bw_release=release,
            abap_schema=schema,
            object_models=object_models,
            object_models_undetermined=undetermined_models,
            hana_repo_style=hana_repo,
            processlog_retention_days=retention,
            tables=tables,
            discovered_at=datetime.now(UTC),
        )

    # --- step 1: ABAP schema -------------------------------------------------------------

    def _resolve_schema(self, profile: Profile, connection: SupportsSelect) -> str:
        if profile.abap_schema != ABAP_SCHEMA_AUTO:
            return profile.abap_schema
        rows = connection.execute_select(
            "SELECT SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'RSTRAN'"
        )
        if not rows:
            raise CapabilityError(
                f"could not resolve ABAP schema for profile '{profile.name}' "
                "(RSTRAN not found in SYS.TABLES)"
            )
        return str(rows[0][0])

    # --- step 2a: BW release -------------------------------------------------------------

    def _detect_release(self, schema: str, connection: SupportsSelect) -> str:
        query = (
            f"SELECT COMPONENT, RELEASE FROM {quote_ident(schema)}.{quote_ident('CVERS')} "
            "WHERE COMPONENT IN ('SAP_BW', 'DW4CORE', 'BW4CORE')"
        )
        try:
            rows = connection.execute_select(query)
        except Exception:
            return "unknown"
        if not rows:
            return "unknown"
        # Prefer BW/4HANA component if present.
        by_component = {str(r[0]): str(r[1]) for r in rows}
        for component in ("DW4CORE", "BW4CORE", "SAP_BW"):
            if component in by_component:
                return _format_release(component, by_component[component])
        first = rows[0]
        return _format_release(str(first[0]), str(first[1]))

    # --- step 2b: existence probes -------------------------------------------------------

    def _probe_abap_existence(
        self, schema: str, connection: SupportsSelect
    ) -> dict[str, TableStatus]:
        """Existence-tier probe against DD02L.

        A refusal here is not fatal, deliberately. Every table reads as unknown, which is honest,
        and the server stays up so ``bw_access_report`` can name the grant that would fix it -
        raising instead would take away the only tool able to explain the failure.
        """
        physical_names = sorted(set(ABAP_TABLES.values()))
        placeholders = ", ".join("?" for _ in physical_names)
        query = (
            f"SELECT TABNAME FROM {quote_ident(schema)}.{quote_ident('DD02L')} "
            f"WHERE TABNAME IN ({placeholders})"
        )
        rows, outcome = _attempt(connection, query, physical_names)
        return self._existence_statuses(ABAP_TABLES, rows, outcome, schema=schema)

    def _probe_hana_objects(self, connection: SupportsSelect) -> dict[str, TableStatus]:
        physical_names = sorted(set(HANA_VIEWS.values()))
        placeholders = ", ".join("?" for _ in physical_names)
        query = (
            "SELECT VIEW_NAME FROM SYS.VIEWS "
            f"WHERE SCHEMA_NAME = 'SYS' AND VIEW_NAME IN ({placeholders})"
        )
        rows, outcome = _attempt(connection, query, physical_names)
        return self._existence_statuses(HANA_VIEWS, rows, outcome, schema="SYS")

    @staticmethod
    def _existence_statuses(
        catalog: dict[str, str],
        rows: list[tuple[Any, ...]] | None,
        outcome: ProbeOutcome,
        *,
        schema: str,
    ) -> dict[str, TableStatus]:
        """Turn one existence probe into per-table statuses.

        When the probe never ran, every table carries the probe's own outcome rather than
        ``absent``. HANA filters catalog views by privilege, so a reader without ``CATALOG READ``
        gets an empty result rather than an error - which is why an empty *successful* read is
        still recorded as ``absent`` but the report warns about it separately.
        """
        seen = {str(row[0]).upper() for row in rows} if rows is not None else set()
        result: dict[str, TableStatus] = {}
        for logical, physical in catalog.items():
            found = rows is not None and physical.upper() in seen
            result[logical] = TableStatus(
                logical_name=logical,
                resolved_name=physical if found else None,
                tier="existence",
                present=found,
                probe=("present" if found else "absent") if rows is not None else outcome,
                schema_name=schema if found else None,
            )
        return result

    # --- step 3: discover-tier -----------------------------------------------------------

    def _discover(self, schema: str, connection: SupportsSelect) -> _Discovered:
        members: dict[str, list[str]] = {}
        status: dict[str, TableStatus] = {}
        query = (
            f"SELECT TABNAME FROM {quote_ident(schema)}.{quote_ident('DD02L')} WHERE TABNAME LIKE ?"
        )
        undetermined: set[str] = set()
        for group, pattern in DISCOVER_PATTERNS.items():
            rows, outcome = _attempt(connection, query, [pattern])
            if rows is None:
                # The failure that motivated this whole distinction: swallowing it recorded
                # "this release has no Advanced DSOs / CompositeProviders", which downstream
                # became "not available on BW 7.50" - a release limitation, for a missing GRANT.
                undetermined.add(group)
                members[group] = []
                status[group] = TableStatus(
                    logical_name=group,
                    resolved_name=None,
                    tier="discover",
                    present=False,
                    probe=outcome,
                )
                continue
            found = sorted({str(r[0]).upper() for r in rows})
            members[group] = found
            resolved = self._pick_representative(group, found)
            status[group] = TableStatus(
                logical_name=group,
                resolved_name=resolved,
                tier="discover",
                present=bool(found),
                probe="present" if found else "absent",
                schema_name=schema if found else None,
            )
        return _Discovered(members=members, table_status=status, undetermined=undetermined)

    @staticmethod
    def _pick_representative(group: str, found: list[str]) -> str | None:
        """Choose the representative (header) table for a discovered family.

        Heuristic for B1: the shortest name is typically the header; text tables end in 'T'.
        Refined against real catalogs in B2.
        """
        if not found:
            return None
        if group == "transformation_text":
            texts = [name for name in found if name.endswith("T")]
            return sorted(texts, key=len)[0] if texts else None
        return sorted(found, key=len)[0]

    # --- step 4: object models, repo style, retention -----------------------------------

    def _detect_object_models(
        self, tables: dict[str, TableStatus], discovered: _Discovered
    ) -> tuple[dict[str, bool], list[str]]:
        """``(variant -> present, variants whose detection was inconclusive)``.

        The second element is what keeps the first honest. ``object_models`` has to stay a
        ``dict[str, bool]`` - it is a published field that callers branch on - so a variant whose
        probe was refused still reads ``False`` there. Naming it here is what stops that ``False``
        from being read as "this system does not use them".
        """

        def present(logical: str) -> bool:
            status = tables.get(logical)
            return status is not None and status.present

        def indeterminate(logical: str) -> bool:
            status = tables.get(logical)
            return status is not None and status.unreadable

        models = {
            "classic_dso": present("dso_header"),
            "adso": bool(discovered.members.get("adso")),
            "composite_provider": bool(discovered.members.get("composite_provider")),
            "multiprovider": present("multiprovider_part"),
            # Open ODS View has no single reliable table; detection deferred to B2.
            "open_ods_view": False,
        }
        # A variant is inconclusive when the evidence its detection rests on was never read:
        # the discover-tier pattern for adso/composite_provider, the header table for the rest.
        inconclusive: set[str] = {
            variant
            for variant in ("adso", "composite_provider")
            if variant in discovered.undetermined
        }
        for variant, logical in (
            ("classic_dso", "dso_header"),
            ("multiprovider", "multiprovider_part"),
        ):
            if indeterminate(logical):
                inconclusive.add(variant)
        return models, sorted(inconclusive)

    def _detect_hana_repo_style(self, connection: SupportsSelect) -> HanaRepoStyle:
        query = (
            "SELECT TABLE_NAME FROM SYS.TABLES "
            "WHERE SCHEMA_NAME = '_SYS_REPO' AND TABLE_NAME = 'ACTIVE_OBJECT'"
        )
        try:
            rows = connection.execute_select(query)
        except Exception:
            return "none"
        return "sys_repo" if rows else "none"

    def _measure_retention(
        self, schema: str, tables: dict[str, TableStatus], connection: SupportsSelect
    ) -> int:
        log_chain = tables.get("log_chain")
        if log_chain is None or not log_chain.present:
            return 0
        query = f"SELECT MIN(DATUM) FROM {quote_ident(schema)}.{quote_ident('RSPCLOGCHAIN')}"
        try:
            rows = connection.execute_select(query)
        except Exception:
            return 0
        if not rows or rows[0][0] is None:
            return 0
        # Cap at one year: analyze at most the last 365 days of process-chain log, regardless of
        # how far back it actually goes (owner decision); reports the actual span when under a year.
        return min(_days_since(str(rows[0][0])), MAX_RUNTIME_WINDOW_DAYS)

    def _populate_row_counts(
        self, schema: str, tables: dict[str, TableStatus], connection: SupportsSelect
    ) -> None:
        """Set row_estimate for present ABAP-schema tables via SYS.M_TABLES (cheap; no COUNT(*))."""
        names = sorted(
            {
                ts.resolved_name
                for ts in tables.values()
                if ts.present and ts.resolved_name and ts.schema_name == schema
            }
        )
        if not names:
            return
        placeholders = ", ".join("?" for _ in names)
        query = (
            "SELECT TABLE_NAME, RECORD_COUNT FROM SYS.M_TABLES "
            f"WHERE SCHEMA_NAME = ? AND TABLE_NAME IN ({placeholders})"
        )
        try:
            rows = connection.execute_select(query, [schema, *names])
        except Exception:
            return
        counts = {str(row[0]).upper(): int(row[1]) for row in rows if row[1] is not None}
        for status in tables.values():
            if status.resolved_name and status.resolved_name.upper() in counts:
                status.row_estimate = counts[status.resolved_name.upper()]


class _Discovered:
    """Internal carrier for discover-tier results.

    ``undetermined`` names the families whose pattern probe never ran, so an empty member list can
    be told apart from a family this release genuinely lacks.
    """

    def __init__(
        self,
        members: dict[str, list[str]],
        table_status: dict[str, TableStatus],
        undetermined: set[str] | None = None,
    ) -> None:
        self.members = members
        self.table_status = table_status
        self.undetermined = undetermined or set()
