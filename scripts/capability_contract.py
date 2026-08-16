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
    "element_prop": (
        "PLANNED",
        "RSZELTPROP carries element display properties and axis placement. Needed to report where "
        "a query element sits (rows, columns, free characteristics, filter).",
    ),
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
    "routine_source_3x": (
        "PLANNED",
        "RSAROUT/RSAROUTT hold 3.x routine source. bw_get_routine_code covers 7.x routines from "
        "RSAABAP only, so a 3.x flow's conversion routines are currently named but not read.",
    ),
    "routine_text_3x": ("PLANNED", "See routine_source_3x."),
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
        "PLANNED",
        "RSPCLOGS holds per-step chain messages. Runtime statistics come from RSPCPROCESSLOG; the "
        "messages are what a failure diagnosis needs.",
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


def build() -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Return ``{logical: (state, reason)}`` and the list of undeclared drift."""
    declared = _declared()
    observed_path = _ROOT / "output" / "capability-reads.json"
    readers = _static_readers() | _measure(observed_path)

    contract: dict[str, tuple[str, str]] = {}
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
            contract[logical] = (state, reason)
            continue
        if logical in DECLARED_STATE:
            contract[logical] = DECLARED_STATE[logical]
            continue
        drift.append(f"{logical}: declared as a capability, never read, and no state declared")
        contract[logical] = ("PLANNED", "UNDECLARED - no reader and no stated intent")
    return contract, drift


CONTRACT_PATH = _ROOT / "docs" / "capability-contract.md"
# Shipped inside the package so an installed wheel carries the same contract as the checkout.
# core/contract.py reads it; bw_capability_report crosses it with a system's discovery record.
DATA_PATH = _ROOT / "src" / "mcp_server_sapbw" / "data" / "capability_contract.json"


def _render_data(contract: dict[str, tuple[str, str]], declared: dict[str, str]) -> str:
    """The machine-readable twin of the markdown artifact.

    Revision is the package version plus a digest of the contract content, not a wall-clock
    timestamp: it identifies *which* contract this is, and keeps ``--check`` deterministic.
    """
    rows = [
        {
            "capability": name,
            "object_name": declared.get(name, ""),
            "state": contract[name][0],
            "reason": contract[name][1],
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


def _render(contract: dict[str, tuple[str, str]], declared: dict[str, str]) -> str:
    """The committed artifact. Reviewable in a diff, and the test asserts it stays complete."""
    implemented = sum(1 for s, _ in contract.values() if s in _IMPLEMENTED_STATES)
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
        f"**{implemented} of {len(contract)} declared capabilities are implemented** "
        f"(`SUPPORTED`, `PARTIAL` or `DISCOVERY_ONLY`).",
        "",
        "| State | Meaning |",
        "|---|---|",
        "| `SUPPORTED` | Read by the server and covered by tests |",
        "| `PARTIAL` | Read, but the surface built on it is incomplete - the gap is named |",
        "| `DISCOVERY_ONLY` | Not read as a table; used to detect an object-model variant |",
        "| `PLANNED` | Declared ahead of implementation, with the intended feature named |",
        "| `NOT_SUPPORTED` | Validation plumbing only; no feature will read it |",
        "| `DEPRECATED` | Superseded; kept so an older release still resolves |",
        "",
        "Presence of a table on *your* system is a separate question, answered per connection by",
        "`bw_system_profile`. This file records what the server would do with it if present.",
        "",
    ]
    for state in STATES:
        names = sorted(n for n, (s, _r) in contract.items() if s == state)
        if not names:
            continue
        lines += [
            f"## {state} ({len(names)})",
            "",
            "| Capability | Object | Notes |",
            "|---|---|---|",
        ]
        for name in names:
            reason = contract[name][1].replace("|", "\\|")
            lines.append(f"| `{name}` | `{declared.get(name, '?')}` | {reason} |")
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
    for logical, (state, _reason) in contract.items():
        by_state.setdefault(state, []).append(logical)

    print(f"Capability contract - {len(contract)} declared capabilities\n")
    for state in STATES:
        names = sorted(by_state.get(state, []))
        if not names:
            continue
        print(f"{state} ({len(names)})")
        for name in names:
            print(f"    {name}")
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
