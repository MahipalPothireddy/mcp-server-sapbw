"""Capability shapes for release-portability testing.

**What these are.** Behavioural fixtures describing *which logical tables are present*, used to
prove the server degrades correctly when a table is absent. Everything has only ever been validated
live against one BW 7.50 system, so the portability claim in the README rests on the capability
resolver rather than on testing — these shapes turn that claim into something the suite checks.

**What these are not.** They are not a verified inventory of what SAP ships in each release. The
server's rule is never to assert metadata it has not read (mission Rule 2), and that applies to its
own test fixtures: a shape here says "suppose these tables are missing", not "release X lacks them".
The value is in the behaviour proven — no tool builds SQL against an absent table, and every
affected tool returns a structured ``UnsupportedResult`` rather than raising — which holds whatever
the real inventory turns out to be.

Each shape is deliberately *hostile* in a different way, because absence is the interesting case.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_server_sapbw.core.capabilities import ABAP_TABLES, DISCOVER_PATTERNS, HANA_VIEWS
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus

SCHEMA = "TESTSCHEMA"

#: Every logical table the server knows about.
ALL_LOGICAL: tuple[str, ...] = (
    *sorted(ABAP_TABLES),
    *sorted(HANA_VIEWS),
    *sorted(DISCOVER_PATTERNS),
)

# --- shapes ---------------------------------------------------------------------------------

#: The reference system: everything the build was validated against.
FULL_75: frozenset[str] = frozenset(ALL_LOGICAL)

#: An older release shape: no advanced DSO, no CompositeProvider, no HANA calc-view catalogue.
#: Classic cubes and DSOs carry the model instead.
NO_MODERN_PROVIDERS: frozenset[str] = FULL_75 - {
    "adso",
    "adso_header",
    "adso_text",
    "adso_keyfields",
    "composite_provider",
    "composite_header",
    "composite_text",
    "transformation_step_adso",
}

#: A BW/4-style shape: ADSO and CompositeProvider only, with the classic cube and the whole BW 3.x
#: dataflow stack gone. This is the shape most likely to break code that assumes RSDCUBE exists.
NO_CLASSIC_STACK: frozenset[str] = FULL_75 - {
    "cube_header",
    "cube_text",
    "cube_field",
    "multiprovider_part",
    "dso_header",
    "dso_text",
    "dso_field",
    "transfer_structure",
    "transfer_structure_field",
    "transfer_rule",
    "comm_structure",
    "comm_structure_field",
    "update_rule",
    "update_rule_key",
    "update_rule_keyfigure",
    "update_rule_routine",
    "routine_source_3x",
    "routine_text_3x",
    "infosource_header",
    "infosource_text",
    "infosource_map",
}

#: No HANA catalogue access at all (a locked-down user with no SYS grants).
NO_HANA_CATALOG: frozenset[str] = FULL_75 - set(HANA_VIEWS)

#: No BEx query tables (a system whose reporting moved off BEx entirely).
NO_BEX: frozenset[str] = FULL_75 - {
    "query_dir",
    "query_provider",
    "query_stats",
    "element_dir",
    "element_xref",
    "element_text",
    "element_select",
    "element_range",
    "element_calc",
    "element_prop",
    "global_variable",
    "report_dir",
}

#: No run history: a freshly copied system, or one whose logs rotated out.
NO_RUN_HISTORY: frozenset[str] = FULL_75 - {
    "log_chain",
    "process_log",
    "log_messages",
    "job_header",
    "job_steps",
    "job_schedule",
}

#: The pathological case: nothing is available. Every tool must say so, and none may raise.
NOTHING: frozenset[str] = frozenset()

SHAPES: dict[str, frozenset[str]] = {
    "full_7_50_reference": FULL_75,
    "no_modern_providers": NO_MODERN_PROVIDERS,
    "no_classic_stack": NO_CLASSIC_STACK,
    "no_hana_catalog": NO_HANA_CATALOG,
    "no_bex": NO_BEX,
    "no_run_history": NO_RUN_HISTORY,
    "nothing_available": NOTHING,
}


def _physical(logical: str) -> str:
    if logical in ABAP_TABLES:
        return str(ABAP_TABLES[logical])
    if logical in HANA_VIEWS:
        return str(HANA_VIEWS[logical])
    return logical.upper()


def capability(present: frozenset[str], *, release: str = "7.50") -> CapabilityRecord:
    """A capability record in which exactly ``present`` logical tables are available."""
    return CapabilityRecord(
        system="qa",
        bw_release=release,
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=_physical(logical) if logical in present else None,
                present=logical in present,
                schema_name=("SYS" if logical in HANA_VIEWS else SCHEMA)
                if logical in present
                else None,
            )
            for logical in ALL_LOGICAL
        },
    )
