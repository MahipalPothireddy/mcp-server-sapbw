"""Capability contract: what this server declares, what it actually implements, and the gap.

**The problem this solves.** The capability resolver declares a logical name for every metadata
table the server knows about. Declaring one is cheap; reading it is not. Left unchecked the two
drift, and the schema ends up claiming an understanding the implementation does not have - a
customer discovers it by asking a question the server answers thinly. This script makes the gap
explicit and machine-checkable.

**How implementation is established.** Two signals, unioned:

*static*
    ``from_logical="<name>"`` found in the source. Definite, but an undercount: eight call sites
    pass the name as a variable fed from a module-level spec table (the text tables, the search
    sources, the provider catalogue, the declared-lookup stores), and a grep reports those as dead.

*observed*
    the logical names the SQL dialect was actually asked for while the test suite ran, collected
    through ``dialect.record_reads``. Measures the answer instead of inferring it. Bounded by test
    coverage, so it is a floor rather than a census - which is why it is unioned with the static
    scan rather than replacing it.

Anything in neither signal must carry an explicit state in ``DECLARED_STATE`` with a reason. That is
the contract: a table is either read, or its non-implementation is a stated decision. Nothing sits
in between silently.

Usage::

    python scripts/capability_contract.py            # report
    python scripts/capability_contract.py --check    # non-zero exit on undeclared drift

Exit codes: 0 clean, 1 drift, 2 usage error.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from mcp_server_sapbw.core.capabilities import (  # noqa: E402
    ABAP_TABLES,
    DISCOVER_PATTERNS,
    HANA_VIEWS,
)

# Implementation states. A capability is never simply "declared"; it is one of these.
#
#   SUPPORTED       read by the server, covered by tests
#   PARTIAL         read, but the surface built on it is incomplete - the gap is named
#   DISCOVERY_ONLY  not read as a table; used to detect which object-model variant a release has
#   PLANNED         declared ahead of implementation, deliberately, with the intended use named
#   NOT_SUPPORTED   declared for validation plumbing only; no feature will ever read it
#   DEPRECATED      was read; superseded, kept only so an older release still resolves
STATES = (
    "SUPPORTED",
    "PARTIAL",
    "DISCOVERY_ONLY",
    "PLANNED",
    "NOT_SUPPORTED",
    "DEPRECATED",
)
# States that are a positive statement about what the server does, as opposed to work outstanding.
_IMPLEMENTED_STATES = ("SUPPORTED", "PARTIAL", "DISCOVERY_ONLY")

# --- two axes, because one word was doing two jobs -------------------------------------------
#
# "SUPPORTED" reads to a customer as "validated against supported BW versions". It only ever meant
# "a reader exists and a test covers it". Those are different claims and a buying decision rests on
# the second one, so they are now separate columns.
#
# Implementation: does the code exist, and how completely.
IMPLEMENTATION_STATUSES = (
    "implemented",
    "partial",
    "discovery_only",
    "planned",
    "unsupported",
    "deprecated",
)
IMPLEMENTATION_OF_STATE: dict[str, str] = {
    "SUPPORTED": "implemented",
    "PARTIAL": "partial",
    "DISCOVERY_ONLY": "discovery_only",
    "PLANNED": "planned",
    "NOT_SUPPORTED": "unsupported",
    "DEPRECATED": "deprecated",
}

# Validation: how far the code has been proven, weakest first. A ladder, so a caller can filter on
# "at least unit_tested" without enumerating.
VALIDATION_STATUSES = ("not_validated", "unit_tested", "integration_tested", "customer_validated")

#: The BW release the integration verification below was performed against. A validation claim
#: without a release is not a claim, because these tables differ across releases.
LIVE_VERIFIED_RELEASE = "BW 7.50 (SAP_BW 750, HANA 2.0)"

#: Capabilities exercised against a live BW system, with the result inspected.
#:
#: This is a **maintained claim of record**, not an inference, and the bar is deliberately high:
#: existence-probing a table during capability discovery does not qualify, because probing that a
#: table exists is not the same as reading it through a feature and checking what came back. Every
#: entry here corresponds to a tool or repository method run against the reference system with its
#: output examined. Anything not listed reports ``unit_tested`` at best.
#:
#: One stated exception, at the top of the list: a *discovery pattern* is never read as a table, so
#: "read through a feature" cannot apply to it. Its feature **is** resolution - deciding which
#: object-model variant a release carries - so it qualifies when the resolved name was returned by a
#: live capability record and checked. The read recorder cannot see these at all, because the
#: resolver issues its own SQL rather than going through the dialect; leaving them unvalidated would
#: report a measurement limitation as an untested feature.
LIVE_VERIFIED: frozenset[str] = frozenset(
    {
        # discovery patterns, verified through live capability resolution (see the note above):
        # RSOADSO% -> RSOADSO, RSOHCPR% -> RSOHCPR, RSDDSTAT% -> RSDDSTAT, RSEC% -> RSECHIE
        "adso",
        "composite_provider",
        "query_stats",
        "analysis_auth",
        # chains and scheduling
        "chain_edges",
        "chain_attr",
        "chain_text",
        "log_chain",
        "process_log",
        # providers, texts and master data
        "dso_header",
        "dso_field",
        "dso_text",
        "adso_header",
        "adso_text",
        "adso_keyfields",
        "cube_header",
        "cube_field",
        "cube_text",
        "multiprovider_part",
        "composite_header",
        "composite_text",
        "infoobject",
        "infoobject_text",
        "characteristic",
        "keyfigure",
        "attribute",
        "nav_attribute",
        # transformations and routines
        "transformation",
        "transformation_field",
        "transformation_rule",
        "routine_source",
        # BEx queries
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
        # HANA boundary
        "object_dependencies",
        "hana_views",
        # BW 3.x dataflow
        "transfer_structure",
        "transfer_rule",
        "update_rule",
        "update_rule_routine",
        "routine_source_3x",
        "routine_text_3x",
        "infosource_map",
        # sources and the dictionary
        "datasource",
        "source_system",
        "dict_columns",
    }
)

#: Verified on a customer's own system, by that customer. Empty, and saying so is the point: a
#: customer reading this contract can see exactly how much of it has been proven outside this
#: project. Populating it is an onboarding output, not a development one.
CUSTOMER_VALIDATED: frozenset[str] = frozenset()

# Intent for every table no reader touches. Written once, deliberately, and asserted by --check.
# The reason is the useful part: it tells the next reader whether this is work or a decision.
DECLARED_STATE: dict[str, tuple[str, str]] = {
    # --- discovery patterns: matched against the dictionary to learn which variant a release has,
    # --- never read as a table. The concrete tables they find are declared separately. ---------
    "adso": (
        "DISCOVERY_ONLY",
        "Pattern RSOADSO% establishes whether this release has Advanced DSOs at all. The tables it "
        "finds are declared as adso_header/adso_text/adso_keyfields, which are read.",
    ),
    "composite_provider": (
        "DISCOVERY_ONLY",
        "Pattern RSOHCPR% establishes whether CompositeProviders exist. The tables it finds are "
        "declared as composite_header/composite_text, which are read.",
    ),
    "query_stats": (
        "DISCOVERY_ONLY",
        "Pattern RSDDSTAT% establishes which BW statistics variant a release carries; the names "
        "differ across 7.4/7.5/BW4. Query usage is read from RSZCOMPDIR.LASTUSED instead, which is "
        "present on every release, so no statistics table is read directly.",
    ),
    "analysis_auth": (
        "DISCOVERY_ONLY",
        "Pattern RSEC% establishes whether analysis authorisations are present. The tables it "
        "finds are declared as auth_values/auth_user/auth_hierarchy/auth_text, which are read.",
    ),
    # --- read only to establish the connected system's shape, never as a feature -------------
    "dict_tables": (
        "NOT_SUPPORTED",
        "DD02L is used by the resolver's own existence probe, which issues its SQL directly rather "
        "than through the dialect. No feature reads it.",
    ),
    "dict_tables_text": (
        "NOT_SUPPORTED",
        "DD02T carries table descriptions. Object descriptions come from the BW text tables, which "
        "are the ones users recognise, so the dictionary texts are not surfaced.",
    ),
    "dict_dataelement_text": (
        "NOT_SUPPORTED",
        "DD04T holds data-element texts. Superseded for every current purpose by the domain-value "
        "decoding in services/aggregation.py, which reads DD07L/DD07T directly.",
    ),
    "source_table": (
        "NOT_SUPPORTED",
        "A resolver-internal alias, not a BW metadata table.",
    ),
    "update_mode": (
        "NOT_SUPPORTED",
        "A resolver-internal alias, not a BW metadata table.",
    ),
    "dso": (
        "DEPRECATED",
        "Superseded by dso_header. Retained so a release naming the table differently still "
        "resolves during discovery.",
    ),
    # --- genuinely planned, with the feature named ------------------------------------------
    "dtp_request": (
        "PLANNED",
        "RSBKREQUEST holds per-DTP request history. Provider currency currently comes from "
        "RSSTATMANPART; this would add per-DTP durations and record counts.",
    ),
    "report_dir": (
        "PLANNED",
        "RSRREPDIR holds query generation status, which distinguishes a query that exists from one "
        "that is executable.",
    ),
    "job_header": (
        "PLANNED",
        "TBTCO/TBTCP/TBTCS carry job periodicity. Chain cadence is currently measured from "
        "observed run history, which is the more honest signal; the declared schedule would let "
        "the two be compared, and a divergence is itself a finding.",
    ),
    "job_steps": ("PLANNED", "See job_header."),
    "job_schedule": ("PLANNED", "See job_header."),
    "infopackage": (
        "PLANNED",
        "RSLDPIO/RSLDPSEL describe InfoPackages, the 3.x-era load step ahead of the transfer "
        "structure. Needed to complete a 3.x flow's upstream hop.",
    ),
    "infopackage_selection": ("PLANNED", "See infopackage."),
    "infosource_header": (
        "PLANNED",
        "RSIS/RSIST/RSISOSMAP describe InfoSources. Partly covered: the 3.x flow tools resolve the "
        "transfer-structure path, but the InfoSource object itself is not describable.",
    ),
    "infosource_text": ("PLANNED", "See infosource_header."),
    "comm_structure": (
        "PLANNED",
        "RSKS/RSKSFIELDNEW hold the 3.x communication structure between transfer rules and update "
        "rules. bw_list_update_rules reports the update rules; the structure between them is not "
        "yet resolved.",
    ),
    "comm_structure_field": ("PLANNED", "See comm_structure."),
    "transfer_structure_field": (
        "PARTIAL",
        "RSTSFIELD is reached through the transfer-rule read rather than directly; the field list "
        "is therefore only as complete as the rules that reference it.",
    ),
    "update_rule_key": (
        "PARTIAL",
        "RSUPDKEY/RSUPDDAT hold 3.x update-rule key and key-figure detail. bw_list_update_rules "
        "reports the rules; this field-level detail is not surfaced.",
    ),
    "update_rule_keyfigure": ("PARTIAL", "See update_rule_key."),
    "extractor_field": (
        "PARTIAL",
        "ROOSFIELD holds extractor field definitions. Enhancement detection uses RSDSSEGFD, which "
        "is the BW-side evidence; ROOSFIELD would confirm it from the replicated extractor.",
    ),
    "transformation_seg": (
        "PARTIAL",
        "RSTRANSEG describes transformation segments. Field mappings are read per rule, so the "
        "segment grouping is not needed for the current output but would matter for a "
        "multi-segment transformation.",
    ),
    "transformation_rule_step": (
        "PARTIAL",
        "RSTRANRULESTEP sequences rule steps. The steps are read through the typed step tables; "
        "the ordering is not yet surfaced.",
    ),
    "variant": (
        "PARTIAL",
        "RSPCVARIANT/RSPCVARIANTT hold process-variant parameters. Chain steps resolve their "
        "targets through RSPCCHAIN, which is the reliable path; variant parameters would add the "
        "per-process detail.",
    ),
    "variant_text": ("PARTIAL", "See variant."),
    "log_messages": (
        "NOT_SUPPORTED",
        "RSPCLOGS was declared as holding per-step chain messages. Verified against the "
        "dictionary: it has four columns (TYPE, VARIANTE, INSTANCE, LOGHANDLE) and no message at "
        "all - it is a pointer into the Application Log. Reading the messages needs BALHDR/BALMSG "
        "or BAL_LOG_MSG_READ, neither of which this server declares, so the honest state is that "
        "this table alone cannot answer the question it was declared for.",
    ),
    "hana_columns": (
        "PARTIAL",
        "SYS.COLUMNS is used for calc-view column resolution through the view-column read; direct "
        "column-level lineage inside a calc view is not implemented.",
    ),
    "hana_view_columns": ("PARTIAL", "See hana_columns."),
    "abap_user_profile": (
        "PLANNED",
        "UST04/UST10S/AGR_USERS/AGR_1251 tie a BW user to their ABAP roles and profiles. The "
        "security tools report analysis-authorisation shape and assignment; the ABAP role side "
        "would show how a user comes to hold one.",
    ),
    "abap_profile_auth": ("PLANNED", "See abap_user_profile."),
    "abap_user_role": ("PLANNED", "See abap_user_profile."),
    "abap_role_auth": ("PLANNED", "See abap_user_profile."),
}


def _declared() -> dict[str, str]:
    """Every logical name the resolver knows, mapped to its physical table or pattern."""
    declared: dict[str, str] = {}
    declared.update(ABAP_TABLES)
    declared.update(HANA_VIEWS)
    declared.update(DISCOVER_PATTERNS)
    return declared


def _static_readers() -> set[str]:
    src = _ROOT / "src" / "mcp_server_sapbw"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    return set(re.findall(r'from_logical="([a-z0-9_]+)"', blob))


def _observed_readers(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def _measure(observed_path: Path) -> set[str]:
    """Run the suite with recording on, so variable-fed reads are counted."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), "BW_RECORD_CAPABILITY_READS": str(observed_path)},
        check=False,
    )
    if proc.returncode != 0:
        print("tests failed; the observed-read measurement is unreliable", file=sys.stderr)
        print(proc.stdout[-2000:], file=sys.stderr)
    return _observed_readers(observed_path)


@dataclass(frozen=True)
class Entry:
    """One capability's full contract row."""

    state: str  # the six-value ContractState, kept because it is a published schema
    reason: str
    implementation: (
        str  # implemented / partial / discovery_only / planned / unsupported / deprecated
    )
    validation: str  # not_validated / unit_tested / integration_tested / customer_validated
    validated_on: str | None = None  # the release integration testing was done against


def build() -> tuple[dict[str, Entry], list[str]]:
    """Return the contract keyed by logical name, plus the list of undeclared drift.

    The two read signals are kept apart here rather than unioned, because the difference between
    them *is* the unit-test evidence: a capability the static scan finds has a reader, and a
    capability the recorder saw during the suite has a reader **that a test exercised**. Unioning
    them first, as an earlier version did, threw that distinction away.
    """
    declared = _declared()
    observed_path = _ROOT / "output" / "capability-reads.json"
    exercised = _measure(observed_path)  # touched while the tests ran
    static = _static_readers()  # has a reader in the source
    readers = static | exercised

    contract: dict[str, Entry] = {}
    drift: list[str] = []
    for logical in sorted(declared):
        if logical in readers:
            state, reason = DECLARED_STATE.get(logical, ("SUPPORTED", "read by the server"))
            # A table that IS read cannot be PLANNED, NOT_SUPPORTED or discovery-only; that is
            # stale intent left behind by an implementation that has since landed.
            if state in ("PLANNED", "NOT_SUPPORTED", "DISCOVERY_ONLY"):
                drift.append(
                    f"{logical}: declared {state} but the server reads it - update DECLARED_STATE"
                )
                state = "SUPPORTED"
            contract[logical] = Entry(
                state=state,
                reason=reason,
                implementation=IMPLEMENTATION_OF_STATE[state],
                validation=_validation_of(logical, exercised),
                validated_on=LIVE_VERIFIED_RELEASE if logical in LIVE_VERIFIED else None,
            )
            continue
        if logical in DECLARED_STATE:
            state, reason = DECLARED_STATE[logical]
            contract[logical] = Entry(
                state=state,
                reason=reason,
                implementation=IMPLEMENTATION_OF_STATE[state],
                # No reader, so the recorder cannot have seen it: an empty exercised set makes
                # `unit_tested` structurally unreachable here rather than merely unlikely. A live
                # claim can still apply - a discovery pattern is verified by resolution, not a read.
                validation=_validation_of(logical, set()),
                validated_on=LIVE_VERIFIED_RELEASE if logical in LIVE_VERIFIED else None,
            )
            continue
        drift.append(f"{logical}: declared as a capability, never read, and no state declared")
        contract[logical] = Entry(
            state="PLANNED",
            reason="UNDECLARED - no reader and no stated intent",
            implementation="planned",
            validation="not_validated",
        )
    return contract, drift


def _validation_of(logical: str, exercised: set[str]) -> str:
    """The highest validation level this capability has actually reached.

    Deliberately never inferred upward. A capability with a reader that no test touched is
    ``not_validated``, not ``unit_tested`` - the recorder measures which tables the suite actually
    asked for, so this is observed rather than assumed. ``customer_validated`` is reserved for
    verification on a customer's own system by that customer, which nothing has yet reached; that
    empty column is the most useful thing in it.
    """
    if logical in CUSTOMER_VALIDATED:
        return "customer_validated"
    if logical in LIVE_VERIFIED:
        return "integration_tested"
    if logical in exercised:
        return "unit_tested"
    return "not_validated"


CONTRACT_PATH = _ROOT / "docs" / "capability-contract.md"
# Shipped inside the package so an installed wheel carries the same contract as the checkout.
# core/contract.py reads it; bw_capability_report crosses it with a system's discovery record.
DATA_PATH = _ROOT / "src" / "mcp_server_sapbw" / "data" / "capability_contract.json"


def _render_data(contract: dict[str, Entry], declared: dict[str, str]) -> str:
    """The machine-readable twin of the markdown artifact.

    Revision is the package version plus a digest of the contract content, not a wall-clock
    timestamp: it identifies *which* contract this is, and keeps ``--check`` deterministic.
    """
    rows = [
        {
            "capability": name,
            "object_name": declared.get(name, ""),
            "state": contract[name].state,
            "implementation": contract[name].implementation,
            "validation": contract[name].validation,
            "validated_on": contract[name].validated_on,
            "reason": contract[name].reason,
        }
        for name in sorted(contract)
    ]
    body = json.dumps(rows, indent=2, sort_keys=True)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    payload = {
        "_comment": "GENERATED by scripts/capability_contract.py - do not edit by hand.",
        "revision": f"{version('mcp-server-sapbw')}+{digest}",
        "capabilities": rows,
    }
    return json.dumps(payload, indent=2) + "\n"


def _render(contract: dict[str, Entry], declared: dict[str, str]) -> str:
    """The committed artifact. Reviewable in a diff, and the test asserts it stays complete."""
    implemented = sum(1 for e in contract.values() if e.state in _IMPLEMENTED_STATES)
    by_validation = {
        level: sum(1 for e in contract.values() if e.validation == level)
        for level in VALIDATION_STATUSES
    }
    lines = [
        "# Capability contract",
        "",
        "<!-- GENERATED by scripts/capability_contract.py - do not edit by hand. -->",
        "",
        "Every metadata capability this server declares, and what it actually does with it.",
        "Declaring a capability is cheap; reading it is not, so the two are recorded",
        "separately and a drift check fails the build when a capability is declared with",
        "neither a reader nor a stated reason.",
        "",
        "## Two questions, two columns",
        "",
        "**Does the code exist** and **has it been proven** are different questions, and a buying",
        'decision rests on the second. They used to share one word: `SUPPORTED` meant "a reader',
        'exists and a test covers it", but reads as "validated against supported BW versions".',
        "",
        f"- **Implementation** - {implemented} of {len(contract)} capabilities are implemented.",
        f"- **Validation** - {by_validation['integration_tested']} have been read through a real",
        f"  feature against a live BW system, {by_validation['unit_tested']} are covered by the",
        f"  offline suite only, {by_validation['not_validated']} are unproven, and",
        f"  **{by_validation['customer_validated']} have been validated on a customer's own",
        "  system**.",
        "",
        "| Validation | Meaning |",
        "|---|---|",
        "| `customer_validated` | Verified on a customer's system, by that customer |",
        f"| `integration_tested` | Read through a feature against {LIVE_VERIFIED_RELEASE}, output "
        "inspected |",
        "| `unit_tested` | Exercised by the offline suite against synthetic fixtures |",
        "| `not_validated` | No test has touched it. A reader may still exist |",
        "",
        "Validation is measured, not asserted: the SQL dialect records which logical tables the",
        "suite actually asks for, so `unit_tested` is observed. It is never inferred upward - a",
        "capability with a reader that no test touched reports `not_validated`.",
        "",
        "| State | Implementation | Meaning |",
        "|---|---|---|",
        "| `SUPPORTED` | `implemented` | Read by the server |",
        "| `PARTIAL` | `partial` | Read, but the surface built on it is incomplete - gap named |",
        "| `DISCOVERY_ONLY` | `discovery_only` | Not read as a table; detects an object-model "
        "variant |",
        "| `PLANNED` | `planned` | Declared ahead of implementation, intended feature named |",
        "| `NOT_SUPPORTED` | `unsupported` | Validation plumbing only; no feature will read it |",
        "| `DEPRECATED` | `deprecated` | Superseded; kept so an older release still resolves |",
        "",
        "Presence of a table on *your* system is a third question again, answered per connection",
        "by `bw_system_profile` and crossed with this contract by `bw_capability_report`.",
        "",
    ]
    for state in STATES:
        names = sorted(n for n, e in contract.items() if e.state == state)
        if not names:
            continue
        lines += [
            f"## {state} ({len(names)})",
            "",
            "| Capability | Object | Validation | Notes |",
            "|---|---|---|---|",
        ]
        for name in names:
            entry = contract[name]
            reason = entry.reason.replace("|", "\\|")
            lines.append(
                f"| `{name}` | `{declared.get(name, '?')}` | `{entry.validation}` | {reason} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = sys.argv[1:]
    check = "--check" in args
    contract, drift = build()
    declared = _declared()

    if not drift:
        artifacts = {
            CONTRACT_PATH: _render(contract, declared),
            DATA_PATH: _render_data(contract, declared),
        }
        for path, rendered in artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not check:
                path.write_text(rendered, encoding="utf-8")
                print(f"wrote {path.relative_to(_ROOT).as_posix()}")
            elif not path.is_file() or path.read_text(encoding="utf-8") != rendered:
                print(
                    f"{path.relative_to(_ROOT).as_posix()} is stale or missing. "
                    "Run: python scripts/capability_contract.py"
                )
                return 1

    by_state: dict[str, list[str]] = {}
    for logical, entry in contract.items():
        by_state.setdefault(entry.state, []).append(logical)

    print(f"Capability contract - {len(contract)} declared capabilities\n")
    for state in STATES:
        names = sorted(by_state.get(state, []))
        if not names:
            continue
        print(f"{state} ({len(names)})")
        for name in names:
            print(f"    {name}  [{contract[name].validation}]")
        print()

    print("Validation (measured, never inferred upward):")
    for level in reversed(VALIDATION_STATUSES):
        count = sum(1 for e in contract.values() if e.validation == level)
        print(f"    {level:20s} {count}")
    print()

    if drift:
        print(f"DRIFT ({len(drift)}):")
        for item in drift:
            print(f"  {item}")
        print(
            "\nEvery declared capability must either be read by the server or carry an explicit "
            "state in DECLARED_STATE. Add the table to a reader, or state why it is not read."
        )
        return 1 if check else 0
    print("No drift: every declared capability is read or has a stated reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
