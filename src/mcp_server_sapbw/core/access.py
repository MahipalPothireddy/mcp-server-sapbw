"""Deployment modes and the grant manifest: what to provision, and what each omission costs.

**Why this module exists.** Capability discovery answers "does this release have the object". It
cannot answer "may this user read it", and until the probes were taught the difference the two
collapsed into one boolean. The consequence was a specific, silent wrong answer: a refused
dictionary read made every Advanced DSO and CompositeProvider look absent, so tools reported "not
available on BW 7.50" - a release limitation, with a release named - when the actual remedy was one
``GRANT SELECT``. The two remedies point in opposite directions, so the two causes are now
separated at the probe (:mod:`..models.capability`) and explained here.

**The manifest is grouped by function, not by table.** A security team does not want 97 table names;
it wants to know that withholding the ``chains`` group costs runtime statistics and the schedule
matrix, and that withholding ``security`` costs four tools and nothing else. Every logical
capability belongs to exactly one group, and a test asserts that, so a table added to discovery
cannot escape the manifest and quietly become an ungranted dependency.

**What is blocked is measured, not asserted.** The affected-tool list comes from the support
matrix's per-tool read attribution, recorded while the test suite exercised each tool. It carries
that measurement's caveat: attribution is a lower bound, so absence from the list is not proof a
tool is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.access import AccessMode, AccessReport, GrantGroupStatus, GrantState
from ..models.capability import CapabilityRecord
from ..models.support import SupportMatrix

#: Placeholder used in generated grant statements. Deliberately not a real user name.
GRANT_PRINCIPAL = "<BW_DISCOVERY_USER>"

#: Token replaced with the connected system's ABAP schema when a report is rendered.
SCHEMA_TOKEN = "<ABAP_SCHEMA>"


@dataclass(frozen=True)
class GrantGroup:
    """A functional bundle of metadata objects that are granted, and withheld, together."""

    group: str
    title: str
    purpose: str
    without_it: str
    #: Logical capability names from the capability record.
    capabilities: frozenset[str]
    #: Physical objects in the ABAP schema, named for the grant script. ``SCHEMA_TOKEN`` is
    #: substituted at render time.
    abap_objects: tuple[str, ...] = ()
    #: Objects outside the ABAP schema, already fully qualified.
    other_objects: tuple[str, ...] = ()
    #: System privileges (rather than object grants) this group needs. HANA filters SYS catalog
    #: views by privilege, so an object grant is not always the right instrument.
    system_privileges: tuple[str, ...] = ()
    required: bool = False
    #: True when a locked-down deployment is *expected* to withhold this group, so a denial here is
    #: a documented posture rather than a provisioning fault.
    optional_by_design: bool = False
    extra_notes: tuple[str, ...] = field(default=())


GRANT_GROUPS: tuple[GrantGroup, ...] = (
    GrantGroup(
        group="dictionary",
        title="ABAP data dictionary and release detection",
        purpose=(
            "Capability discovery itself: which metadata tables exist on this release, and which "
            "BW release this is. Every other group is gated on what this one finds."
        ),
        without_it=(
            "Nothing works. Discovery cannot establish what exists, so no tool can build SQL it "
            "is allowed to trust. This is the one group with no degraded mode."
        ),
        capabilities=frozenset(
            {"dict_tables", "dict_tables_text", "dict_columns", "dict_dataelement_text"}
        ),
        abap_objects=("DD02L", "DD02T", "DD03L", "DD04T", "CVERS"),
        other_objects=("SYS.TABLES",),
        required=True,
    ),
    GrantGroup(
        group="catalog",
        title="HANA catalog and the read-only assertion",
        purpose=(
            "Resolving the ABAP schema name, estimating table row counts without a COUNT(*), and "
            "the connect-time assertion that this user holds no write privileges."
        ),
        without_it=(
            "The ABAP schema must be named explicitly on the profile instead of resolved, row "
            "estimates are absent from the system profile, and the fail-closed read-only check "
            "cannot complete - which refuses the connection when read_only_user is true, by "
            "design."
        ),
        capabilities=frozenset(),
        other_objects=("SYS.EFFECTIVE_PRIVILEGES", "SYS.M_TABLES"),
        system_privileges=("CATALOG READ",),
        required=True,
        extra_notes=(
            "HANA filters SYS catalog views by the reader's privileges, so withholding CATALOG "
            "READ makes objects look absent rather than producing an error. That is the failure "
            "mode this whole module exists to make visible.",
        ),
    ),
    GrantGroup(
        group="providers",
        title="InfoProviders and InfoObjects",
        purpose=(
            "DSOs, Advanced DSOs, InfoCubes, MultiProviders, CompositeProviders, InfoObjects, "
            "their fields, descriptions and attributes. The object inventory everything else "
            "refers to."
        ),
        without_it=(
            "No object descriptions, no field lists, no search, no inventory. Lineage still walks "
            "transformations but cannot say what kind of object each node is."
        ),
        capabilities=frozenset(
            {
                "dso_header",
                "dso_text",
                "dso_field",
                "cube_header",
                "cube_text",
                "cube_field",
                "multiprovider_part",
                "adso_header",
                "adso_text",
                "adso_keyfields",
                "composite_header",
                "composite_text",
                "infoobject",
                "infoobject_text",
                "characteristic",
                "keyfigure",
                "attribute",
                "nav_attribute",
                "adso",
                "composite_provider",
            }
        ),
        abap_objects=(
            "RSDODSO",
            "RSDODSOT",
            "RSDODSOIOBJ",
            "RSDCUBE",
            "RSDCUBET",
            "RSDCUBEIOBJ",
            "RSDCUBEMULTI",
            "RSOADSO",
            "RSOADSOT",
            "RSOADSOKEYFIELDS",
            "RSOHCPR",
            "RSOHCPRT",
            "RSDIOBJ",
            "RSDIOBJT",
            "RSDCHA",
            "RSDKYF",
            "RSDBCHATR",
            "RSDATRNAV",
        ),
    ),
    GrantGroup(
        group="dataflow",
        title="Transformations, routines, DTPs and DataSources",
        purpose=(
            "The 7.x dataflow: transformation headers and field rules, full ABAP routine source, "
            "DTP update modes, DataSources, source systems and the per-provider request ledger."
        ),
        without_it=(
            "No lineage, no impact analysis, no routine code or anti-pattern analysis, no load "
            "currency. This is the group most of the server's value rests on."
        ),
        capabilities=frozenset(
            {
                "transformation",
                "transformation_field",
                "transformation_rule",
                "transformation_rule_step",
                "transformation_step_rout",
                "transformation_step_const",
                "transformation_step_master",
                "transformation_step_dso",
                "transformation_step_adso",
                "transformation_seg",
                "transformation_text",
                "routine_source",
                "dtp",
                "dtp_request",
                "infopackage",
                "infopackage_selection",
                "datasource",
                "datasource_field",
                "source_system",
                "request_status",
            }
        ),
        abap_objects=(
            "RSTRAN",
            "RSTRANFIELD",
            "RSTRANRULE",
            "RSTRANRULESTEP",
            "RSTRANSTEPROUT",
            "RSTRANSTEPCNST",
            "RSTRANSTEPMASTER",
            "RSTRANSTEPODSO",
            "RSTRANSTEPADSO",
            "RSTRANSEG",
            "RSTRANT",
            "RSAABAP",
            "RSBKDTP",
            "RSBKREQUEST",
            "RSLDPIO",
            "RSLDPSEL",
            "RSDS",
            "RSDSSEGFD",
            "RSBASIDOC",
            "RSSTATMANPART",
        ),
        extra_notes=(
            "RSAABAP carries ABAP routine source, which is customer intellectual property and "
            "often business logic. Grant it knowingly.",
        ),
    ),
    GrantGroup(
        group="chains",
        title="Process chains and job scheduling",
        purpose=(
            "Chain structure and nesting, per-step run logs, and the background-job tables that "
            "are the only reliable source of periodicity."
        ),
        without_it=(
            "No chain structure, no runtime statistics or p95 completion, no schedule matrix, and "
            "no load cadence - so 'when is this data current' becomes unanswerable."
        ),
        capabilities=frozenset(
            {
                "chain_edges",
                "chain_attr",
                "chain_text",
                "variant",
                "variant_text",
                "log_chain",
                "process_log",
                "log_messages",
                "job_header",
                "job_steps",
                "job_schedule",
            }
        ),
        abap_objects=(
            "RSPCCHAIN",
            "RSPCCHAINATTR",
            "RSPCCHAINT",
            "RSPCVARIANT",
            "RSPCVARIANTT",
            "RSPCLOGCHAIN",
            "RSPCPROCESSLOG",
            "RSPCLOGS",
            "TBTCO",
            "TBTCP",
            "TBTCS",
        ),
    ),
    GrantGroup(
        group="queries",
        title="BEx queries",
        purpose=(
            "Query directory and element tree, restrictions, calculated key figures, variables, "
            "and the text join that supplies a query's own description."
        ),
        without_it=(
            "No query definitions, no field-level query lineage, no shared-element detection, and "
            "no report-side impact analysis."
        ),
        capabilities=frozenset(
            {
                "query_dir",
                "query_provider",
                "element_dir",
                "element_xref",
                "element_text",
                "element_select",
                "element_range",
                "element_calc",
                "global_variable",
                "element_prop",
                "report_dir",
            }
        ),
        abap_objects=(
            "RSZCOMPDIR",
            "RSZCOMPIC",
            "RSZELTDIR",
            "RSZELTXREF",
            "RSZELTTXT",
            "RSZSELECT",
            "RSZRANGE",
            "RSZCALC",
            "RSZGLOBV",
            "RSZELTPROP",
            "RSRREPDIR",
        ),
    ),
    GrantGroup(
        group="flows_3x",
        title="BW 3.x transfer and update rules",
        purpose=(
            "The pre-7.x load path: transfer structures and rules, InfoSources, communication "
            "structures, update rules and their routines."
        ),
        without_it=(
            "Lineage stops at any DataSource that has no 7.x transformation. On the reference "
            "system that is over a thousand DataSources, mostly master data, so this is not "
            "legacy trivia - the loads are live."
        ),
        capabilities=frozenset(
            {
                "transfer_structure",
                "transfer_structure_field",
                "transfer_rule",
                "infosource_map",
                "infosource_header",
                "infosource_text",
                "comm_structure",
                "comm_structure_field",
                "update_rule",
                "update_rule_keyfigure",
                "update_rule_key",
                "update_rule_routine",
                "routine_source_3x",
                "routine_text_3x",
            }
        ),
        abap_objects=(
            "RSTS",
            "RSTSFIELD",
            "RSTSRULES",
            "RSISOSMAP",
            "RSIS",
            "RSIST",
            "RSKS",
            "RSKSFIELDNEW",
            "RSUPDINFO",
            "RSUPDDAT",
            "RSUPDKEY",
            "RSUPDROUT",
            "RSAROUT",
            "RSAROUTT",
        ),
    ),
    GrantGroup(
        group="hana",
        title="HANA calculation views and volume",
        purpose=(
            "Calc view dependencies, the BW<->HANA boundary crossings BW's own where-used lists "
            "omit, and per-table volume."
        ),
        without_it=(
            "No calc view lineage, no crossing inventory, and no provider volume. A "
            "CompositeProvider's parts still resolve, but consumption from outside BW becomes "
            "invisible - which is exactly the dependency a decommissioning decision needs."
        ),
        capabilities=frozenset(
            {
                "object_dependencies",
                "hana_views",
                "hana_view_columns",
                "hana_columns",
                "cs_tables",
            }
        ),
        other_objects=(
            "SYS.OBJECT_DEPENDENCIES",
            "SYS.VIEWS",
            "SYS.VIEW_COLUMNS",
            "SYS.COLUMNS",
            "SYS.M_CS_TABLES",
            "_SYS_REPO.ACTIVE_OBJECT",
        ),
        system_privileges=("CATALOG READ",),
    ),
    GrantGroup(
        group="statistics",
        title="Query usage statistics",
        purpose="Ranking queries by real execution history, which is what makes a decommissioning "
        "candidate credible rather than merely unused-looking.",
        without_it=(
            "Usage falls back to RSZCOMPDIR.LASTUSED alone. Decommissioning candidates are still "
            "reported, on weaker evidence."
        ),
        capabilities=frozenset({"query_stats"}),
        abap_objects=("RSDDSTAT%",),
        extra_notes=(
            "The RSDDSTAT* family varies by release, so this is granted as a pattern rather than "
            "a fixed list. Discovery reports which members exist here.",
        ),
    ),
    GrantGroup(
        group="extractor",
        title="Extractor metadata",
        purpose="DataSource extract structures and appended customer fields, which is the "
        "metadata-confirmed evidence that an extractor was enhanced.",
        without_it="The extractor-enhancement inventory cannot be built, so a source-side "
        "enhancement is invisible from BW.",
        capabilities=frozenset({"extractor", "extractor_field"}),
        abap_objects=("ROOSOURCE", "ROOSFIELD"),
    ),
    GrantGroup(
        group="security",
        title="Analysis authorisations (row-level security)",
        purpose=(
            "Authorisation shape, coverage gaps, and whether a query returns different data per "
            "user. Four tools read nothing else."
        ),
        without_it=(
            "The four security tools report a documented gap. Withholding this is a reasonable "
            "posture and the server is built for it: nothing here is ever cached, and an "
            "unreadable RSEC* table is reported as unreadable, never as 'no authorisations "
            "exist'."
        ),
        capabilities=frozenset(
            {
                "auth_values",
                "auth_hierarchy",
                "auth_user",
                "auth_text",
                "abap_user_profile",
                "abap_profile_auth",
                "abap_user_role",
                "abap_role_auth",
                "analysis_auth",
            }
        ),
        abap_objects=(
            "RSECVAL",
            "RSECHIE",
            "RSECUSERAUTH",
            "RSECTXT",
            "UST04",
            "UST10S",
            "AGR_USERS",
            "AGR_1251",
        ),
        optional_by_design=True,
        extra_notes=(
            "RSECVAL holds permission values, and joined to a user id that is personal data. This "
            "is the group to withhold first if any is withheld.",
        ),
    ),
)

#: Group id -> group, for lookup.
GROUPS_BY_ID: dict[str, GrantGroup] = {g.group: g for g in GRANT_GROUPS}


def group_for_capability(capability: str) -> GrantGroup | None:
    """The grant group owning a logical capability name, or ``None`` if unmapped."""
    for group in GRANT_GROUPS:
        if capability in group.capabilities:
            return group
    return None


def mapped_capabilities() -> frozenset[str]:
    """Every logical capability name the manifest accounts for."""
    names: set[str] = set()
    for group in GRANT_GROUPS:
        names |= group.capabilities
    return frozenset(names)


def grant_statements(group: GrantGroup, schema: str) -> list[str]:
    """HANA grant statements for one group, against a concrete ABAP schema."""
    statements = [f"GRANT {priv} TO {GRANT_PRINCIPAL};" for priv in group.system_privileges]
    for name in group.abap_objects:
        if name.endswith("%"):
            statements.append(
                f"-- {name}: grant each member discovery reports, or the whole schema:\n"
                f'--   GRANT SELECT ON SCHEMA "{schema}" TO {GRANT_PRINCIPAL};'
            )
            continue
        statements.append(f'GRANT SELECT ON "{schema}"."{name}" TO {GRANT_PRINCIPAL};')
    for qualified in group.other_objects:
        if "." in qualified:
            owner, obj = qualified.split(".", 1)
            statements.append(f'GRANT SELECT ON "{owner}"."{obj}" TO {GRANT_PRINCIPAL};')
        else:  # pragma: no cover - every entry is qualified today
            statements.append(f"GRANT SELECT ON {qualified} TO {GRANT_PRINCIPAL};")
    return statements


def _group_state(
    group: GrantGroup, record: CapabilityRecord
) -> tuple[GrantState, list[str], list[str]]:
    """Classify one group against the probe evidence.

    Returns ``(state, denied, undetermined)``. A group with no tracked capabilities (``catalog``)
    cannot be classified from probe results, because its objects are what the probes were run
    *with* - a failure there prevents discovery rather than being recorded by it.
    """
    tracked = sorted(c for c in group.capabilities if record.table(c) is not None)
    if not tracked:
        return "undetermined", [], []

    denied: list[str] = []
    undetermined: list[str] = []
    present = 0
    absent = 0
    for capability in tracked:
        status = record.table(capability)
        if status is None:  # pragma: no cover - filtered above
            continue
        if status.probe == "denied":
            denied.append(capability)
        elif status.probe in ("failed", "not_probed"):
            undetermined.append(capability)
        elif status.present:
            present += 1
        else:
            absent += 1

    if denied and (present or absent):
        return "partial", denied, undetermined
    if denied:
        return "denied", denied, undetermined
    if undetermined and not (present or absent):
        return "undetermined", denied, undetermined
    if present:
        return "granted", denied, undetermined
    return "absent", denied, undetermined


def _observed_mode(states: dict[str, GrantState], classifiable: set[str]) -> AccessMode:
    """Derive the posture actually in force from the group states.

    Anything refused means a least-privilege deployment, whatever the profile claims. An absent
    group does not, because a release that lacks an object is not a provisioning choice.

    Only ``classifiable`` groups vote. A group with no capability discovery tracks - ``catalog``,
    whose objects are what the probes are run *with* - is structurally undetermined, and letting it
    vote would peg every deployment at ``unknown`` including a complete technical read.
    """
    voting = {group: state for group, state in states.items() if group in classifiable}
    if any(state in ("denied", "partial") for state in voting.values()):
        return "least_privilege"
    if not voting or any(state == "undetermined" for state in voting.values()):
        return "unknown"
    return "technical_read"


def build_access_report(
    record: CapabilityRecord,
    *,
    declared_mode: AccessMode = "unknown",
    read_only_asserted: bool = True,
    matrix: SupportMatrix | None = None,
) -> AccessReport:
    """Cross the probe evidence with the grant manifest into a provisioning answer."""
    schema = record.abap_schema
    groups: list[GrantGroupStatus] = []
    states: dict[str, GrantState] = {}
    grants: list[str] = []
    denied_capabilities: set[str] = set()

    classifiable: set[str] = set()

    for group in GRANT_GROUPS:
        state, denied, undetermined = _group_state(group, record)
        states[group.group] = state
        if any(record.table(c) is not None for c in group.capabilities):
            classifiable.add(group.group)
        denied_capabilities.update(denied)
        needed = grant_statements(group, schema) if state in ("denied", "partial") else []
        for statement in needed:
            if statement not in grants:
                grants.append(statement)
        groups.append(
            GrantGroupStatus(
                group=group.group,
                title=group.title,
                required=group.required,
                state=state,
                purpose=group.purpose,
                without_it=group.without_it,
                objects=[
                    *(f'"{schema}"."{name}"' for name in group.abap_objects),
                    *group.other_objects,
                    *(f"{priv} (system privilege)" for priv in group.system_privileges),
                ],
                denied_capabilities=denied,
                undetermined_capabilities=undetermined,
                grant_statements=needed,
            )
        )

    observed = _observed_mode(states, classifiable)
    totals: dict[str, int] = {}
    for state in states.values():
        totals[state] = totals.get(state, 0) + 1

    blocked = _blocked_tools(denied_capabilities, matrix)
    caveats = _caveats(record, states, denied_capabilities, blocked, matrix)

    return AccessReport(
        system=record.system,
        bw_release=record.bw_release,
        declared_mode=declared_mode,
        observed_mode=observed,
        mode_mismatch=declared_mode not in ("unknown", observed),
        read_only_asserted=read_only_asserted,
        groups=groups,
        totals=totals,
        grants_required=grants,
        blocked_tools=blocked,
        undetermined_object_models=list(record.object_models_undetermined),
        caveats=caveats,
    )


def _blocked_tools(denied: set[str], matrix: SupportMatrix | None) -> list[str]:
    """Tools reading at least one refused capability, from measured attribution."""
    if not denied or matrix is None:
        return []
    return sorted(tool.tool for tool in matrix.tools if denied & set(tool.requires))


def _caveats(
    record: CapabilityRecord,
    states: dict[str, GrantState],
    denied: set[str],
    blocked: list[str],
    matrix: SupportMatrix | None,
) -> list[str]:
    caveats: list[str] = []
    unreadable = record.unreadable_tables()
    if unreadable:
        caveats.append(
            f"{len(unreadable)} metadata object(s) could not be read, so their presence is "
            "unknown rather than absent. A tool depending on one of them reports a gap; it does "
            "not report that this release lacks the object."
        )
    if record.object_models_undetermined:
        listed = ", ".join(record.object_models_undetermined)
        caveats.append(
            f"object-model detection was inconclusive for: {listed}. These read as absent in "
            "object_models for lack of evidence, not because this system lacks them."
        )
    required_broken = [
        group
        for group, state in states.items()
        if state in ("denied", "partial") and GROUPS_BY_ID[group].required
    ]
    if required_broken:
        caveats.append(
            f"required group(s) {', '.join(sorted(required_broken))} are not fully readable. "
            "These gate discovery itself, so results elsewhere in this report may understate "
            "what the system actually has."
        )
    if denied and matrix is None:
        caveats.append(
            "the support matrix was unavailable, so the affected-tool list could not be computed "
            "from measured attribution and is empty rather than complete."
        )
    if blocked:
        caveats.append(
            f"{len(blocked)} tool(s) read at least one refused object. Attribution is a measured "
            "lower bound, so a tool absent from that list is not proven unaffected."
        )
    if not denied:
        caveats.append(
            "no read was refused on this connection, so nothing here is evidence about grants "
            "that were never exercised - a group reported granted was probed, not audited."
        )
    return caveats
