"""Generate the support matrix: which tool works on which BW release.

**The question this answers**, which nothing else in the project does:

    I run BW 7.4 (or BW/4HANA). Which of your tools will work on my landscape?

``bw_capability_report`` answers a neighbouring question well, but only once you have connected, and
only per metadata table. This one is answerable from shipped data, keyed by tool, and explicit about
the difference between "verified here" and "nobody has checked".

**How a tool's requirements are established.** By measurement, not by hand. Each tool call opens a
nested read-recording scope (``dialect.record_tool_reads``), so the logical tables it asks for are
attributed to it. The suite runs with ``BW_RECORD_TOOL_READS`` set and this script reads the result.

A hand-written mapping was rejected: 55 tools, each composing several readers, and nothing would
contradict the mapping when a tool gained a reader. It would drift on the first change and drift
silently, which is worse than not having it.

**Two kinds of not-knowing, kept apart.** They look the same in a table and mean different things:

*not_measured*
    the offline suite never invoked this tool at the tool boundary, so what it needs is unknown.
    Reported as ``unknown``, never as an empty requirement set - that would turn a measurement gap
    into a claim of universal compatibility, the most misleading answer available.

*unverified*
    the requirements are known, but nobody has run the tool against that release. A prediction is
    not offered, because which metadata objects a release carries is precisely what this server
    discovers at runtime instead of assuming.

Usage::

    python scripts/support_matrix.py            # regenerate the data file and the doc
    python scripts/support_matrix.py --check    # non-zero exit when either is stale

Exit codes: 0 clean, 1 stale, 2 usage error.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from mcp_server_sapbw.core.capabilities import DISCOVER_PATTERNS  # noqa: E402
from mcp_server_sapbw.core.contract import contract, contract_revision  # noqa: E402
from mcp_server_sapbw.models.capability import (  # noqa: E402
    VALIDATION_RANK,
    ImplementationStatus,
    ValidationStatus,
)
from mcp_server_sapbw.models.support import (  # noqa: E402
    ReleaseSupport,
    ReleaseVerdict,
    SupportMatrix,
    ToolSupport,
)

DATA_PATH = _ROOT / "src" / "mcp_server_sapbw" / "data" / "support_matrix.json"
DOC_PATH = _ROOT / "docs" / "support-matrix.md"
_OBSERVED_PATH = _ROOT / "output" / "tool-reads.json"

#: The release the reference system runs, and the only one with verification evidence. Kept in step
#: with `capability_contract.LIVE_VERIFIED_RELEASE` by a test, because two files claiming different
#: verification releases is the kind of drift a customer would find before we did.
VERIFIED_RELEASE = "BW 7.50"
VERIFIED_ON = "SAP_BW 750, HANA 2.0"

#: Releases the matrix has an opinion about. Anything beyond the verified one is listed **so that
#: its absence of evidence is visible**. Omitting them would let silence read as coverage, which
#: is the failure mode this whole file exists to avoid.
RELEASES: dict[str, str] = {
    "BW 7.40": (
        "Object model predates the advanced DSO. The provider tables this server resolves by "
        "pattern are the ones that differ; nothing here has been run against a 7.4 system."
    ),
    VERIFIED_RELEASE: (
        "The reference system. Every 'verified' verdict in this matrix was measured here, by "
        "reading through a feature and inspecting the output."
    ),
    "BW/4HANA 2.0": (
        "The classic InfoCube and 3.x dataflow objects are removed and the advanced DSO is the "
        "primary provider, so the tools built on the classic tables are the ones to check first. "
        "Nothing here has been run against a BW/4HANA system."
    ),
}

#: Tools whose answer is completed by a system outside BW. They work on every release as far as BW
#: is concerned; without the connector they return a template naming what is missing, which is a
#: different thing from failing and is reported as such.
CONNECTOR_TOOLS: dict[str, str] = {
    "bw_get_extractor_exit_code": "ABAP source system (ADT)",
    "bw_check_schedule_risk": "BI platform inventory",
}

#: Tools that legitimately read no BW metadata, with the reason. Distinguished from `not_measured`
#: by
#: being a *stated* empty set: these answer from shipped data, configuration, or the local cache.
#: Listing them means a measured-empty result is a confirmation rather than a surprise.
READS_NOTHING: dict[str, str] = {
    "bw_list_systems": "Reads the profiles file, not BW.",
    "bw_cache_status": "Reports the local cache and snapshot store.",
    "bw_refresh_cache": "Clears the local cache.",
    "bw_capability_report": "Crosses the shipped contract with an already-discovered record.",
    "bw_support_matrix": "Reads the shipped matrix; this is the tool that needs no system at all.",
    "bw_list_snapshots": "Lists the local snapshot store.",
}


def _tool_names() -> list[str]:
    """Every registered tool name, from the server itself rather than a maintained list."""
    from fastmcp import Client  # noqa: PLC0415 - import cost only when generating

    from mcp_server_sapbw import server  # noqa: PLC0415

    async def names() -> list[str]:
        async with Client(server.mcp) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return asyncio.run(names())


def _measure() -> dict[str, set[str]]:
    """Run the suite with per-tool attribution on, and read what each tool asked for."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "BW_RECORD_TOOL_READS": str(_OBSERVED_PATH)},
        check=False,
    )
    if proc.returncode != 0:
        print("tests failed; the per-tool measurement is unreliable", file=sys.stderr)
        print(proc.stdout[-2000:], file=sys.stderr)
    if not _OBSERVED_PATH.is_file():
        return {}
    raw: dict[str, list[str]] = json.loads(_OBSERVED_PATH.read_text(encoding="utf-8"))
    return {tool: set(reads) for tool, reads in raw.items()}


def _weakest(names: list[str]) -> tuple[ImplementationStatus | None, ValidationStatus]:
    """The weakest implementation and validation status across a tool's requirements.

    A minimum rather than a summary: a tool is only as proven as the least-proven thing it reads,
    and averaging would let one verified capability hide four unverified ones.
    """
    entries = contract()
    implementations = [entries[n].implementation for n in names if n in entries]
    validations: list[ValidationStatus] = [entries[n].validation for n in names if n in entries]
    rank = {"implemented": 0, "discovery_only": 1, "partial": 2, "deprecated": 3, "planned": 4}
    worst_impl = max((i for i in implementations if i), key=lambda i: rank.get(i, 9), default=None)
    # A tool with no measured requirements has nothing to be weaker than, so it starts unproven
    # rather than inheriting a level from an empty set.
    unproven: ValidationStatus = "not_validated"
    worst_valid = min(validations, key=lambda v: VALIDATION_RANK.get(v, 0), default=unproven)
    return worst_impl, worst_valid


def _verdict(
    release: str, *, measured: bool, requires: list[str], connector: str | None
) -> ReleaseVerdict:
    """One tool's verdict on one release. Every value names where the claim comes from."""
    if connector:
        return "needs_connector"
    if not measured:
        return "unknown"
    if release != VERIFIED_RELEASE:
        # No prediction. Which metadata objects a release carries is what the capability resolver
        # discovers at runtime; asserting it here from a version number would be the exact thing
        # this server refuses to do.
        return "unverified"
    entries = contract()
    if requires and all(
        entries.get(name) is not None and entries[name].validation == "integration_tested"
        for name in requires
    ):
        return "verified"
    return "verified" if not requires else "expected"


def build() -> SupportMatrix:
    """Assemble the matrix. Pure function of the measurement plus the shipped contract.

    Returns the pydantic model rather than a dict so the generator and the server validate the same
    schema. The alternative - assembling a dict and validating it only on the way back in - is how a
    generated file ends up shaped almost right.
    """
    observed = _measure()
    tools = _tool_names()
    conditional = sorted(DISCOVER_PATTERNS)

    rows: list[ToolSupport] = []
    for tool in tools:
        connector = CONNECTOR_TOOLS.get(tool)
        measured = tool in observed
        requires = sorted(observed.get(tool, ()))
        impl, valid = _weakest(requires)
        note = READS_NOTHING.get(tool, "")
        if measured and not requires and not note:
            note = "Measured to read no BW metadata."
        if not measured:
            note = (
                "The offline suite does not invoke this tool at the tool boundary, so what it "
                "needs is unmeasured. Reported as unknown rather than as needing nothing."
            )
        rows.append(
            ToolSupport(
                tool=tool,
                requires=requires,
                measurement="measured" if measured else "not_measured",
                implementation=impl,
                validation=valid,
                releases={
                    release: _verdict(
                        release, measured=measured, requires=requires, connector=connector
                    )
                    for release in RELEASES
                },
                needs_connector=connector,
                note=note,
            )
        )

    totals: dict[str, dict[str, int]] = {}
    for release in RELEASES:
        counts: dict[str, int] = {}
        for row in rows:
            verdict = row.releases[release]
            counts[verdict] = counts.get(verdict, 0) + 1
        totals[release] = dict(sorted(counts.items()))

    releases = [
        ReleaseSupport(
            release=release,
            evidence="verified" if release == VERIFIED_RELEASE else "not_verified",
            verified_on=VERIFIED_ON if release == VERIFIED_RELEASE else None,
            release_conditional=conditional,
            note=note,
        )
        for release, note in RELEASES.items()
    ]

    unmeasured = [r.tool for r in rows if r.measurement == "not_measured"]
    caveats = [
        "A tool's requirement list is a measured LOWER BOUND. It is collected by attributing each "
        "read to the tool that caused it while the offline suite runs, so a code path no test "
        "reaches contributes nothing. Everything listed really is read; the list may be short.",
        f"Only {VERIFIED_RELEASE} has been verified ({VERIFIED_ON}). Every other release reports "
        "'unverified' for every tool. That is an absence of evidence, stated rather than filled "
        "in: which metadata objects a release carries is what the capability resolver discovers "
        "at connect time, and predicting it from a version number here would be guesswork "
        "wearing the clothes of a support statement.",
        "On an unverified release, the capabilities resolved by pattern rather than by a known "
        f"name are the ones that decide the answer: {', '.join(conditional)}. Connect and run "
        "bw_capability_report to settle them in one call.",
    ]
    if unmeasured:
        caveats.append(
            f"{len(unmeasured)} tool(s) were not invoked at the tool boundary by the offline "
            "suite, so their requirements are unknown rather than empty: " + ", ".join(unmeasured)
        )
    return SupportMatrix(
        server_version=version("mcp-server-sapbw"),
        contract_revision=contract_revision(),
        releases=releases,
        tools=rows,
        totals=totals,
        caveats=caveats,
    )


_VERDICT_MEANING = {
    "verified": "Every capability it needs was read through a feature on this release, output "
    "inspected",
    "expected": "Implemented and read, but not every capability was verified on this release",
    "unverified": "Nobody has run this tool against this release. Not a prediction",
    "needs_connector": "The BW half works; the answer is completed by a system outside BW",
    "unknown": "This build could not measure what the tool needs",
}


def render_doc(matrix: SupportMatrix) -> str:
    """The committed artifact, reviewable in a diff."""
    releases = [r.release for r in matrix.releases]
    lines = [
        "# Support matrix",
        "",
        "<!-- GENERATED by scripts/support_matrix.py - do not edit by hand. -->",
        "",
        "Which tool works on which BW release, answerable without connecting to anything.",
        "`bw_support_matrix` returns the same content over the protocol.",
        "",
        "## What the verdicts mean",
        "",
        "There is deliberately no `supported`. Every value says where the claim comes from.",
        "",
        "| Verdict | Meaning |",
        "|---|---|",
        *(f"| `{k}` | {v} |" for k, v in _VERDICT_MEANING.items()),
        "",
        "## Releases",
        "",
        "| Release | Evidence | Verified on | Note |",
        "|---|---|---|---|",
    ]
    for entry in matrix.releases:
        lines.append(
            f"| `{entry.release}` | `{entry.evidence}` | "
            f"{entry.verified_on or '-'} | {entry.note} |"
        )
    lines += ["", "## Totals", "", "| Release | " + " | ".join(_VERDICT_MEANING) + " |"]
    lines.append("|---" * (len(_VERDICT_MEANING) + 1) + "|")
    for release in releases:
        counts = matrix.totals[release]
        lines.append(
            f"| `{release}` | "
            + " | ".join(str(counts.get(verdict, 0)) for verdict in _VERDICT_MEANING)
            + " |"
        )
    lines += [
        "",
        "## Per tool",
        "",
        "`requires` is a measured lower bound - see the caveats below.",
        "",
        "| Tool | " + " | ".join(f"`{r}`" for r in releases) + " | Validation | Requires |",
        "|---" * (len(releases) + 3) + "|",
    ]
    for row in matrix.tools:
        requires = ", ".join(f"`{c}`" for c in row.requires) or "-"
        verdicts = " | ".join(f"`{row.releases[r]}`" for r in releases)
        lines.append(f"| `{row.tool}` | {verdicts} | `{row.validation}` | {requires} |")
    lines += ["", "## Caveats", ""]
    lines += [f"- {caveat}" for caveat in matrix.caveats]
    lines.append("")
    return "\n".join(lines)


def _payload(matrix: SupportMatrix) -> str:
    """Serialise, with the generator's banner first so a human opening the file sees it."""
    payload: dict[str, object] = {
        "_comment": "GENERATED by scripts/support_matrix.py - do not edit by hand.",
        **matrix.model_dump(mode="json"),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    unknown = [a for a in argv[1:] if a != "--check"]
    if unknown:
        print(f"unknown argument(s): {unknown}", file=sys.stderr)
        return 2

    matrix = build()
    data, doc = _payload(matrix), render_doc(matrix)

    if check:
        stale: list[str] = []
        for path, expected in ((DATA_PATH, data), (DOC_PATH, doc)):
            actual = path.read_text(encoding="utf-8") if path.is_file() else ""
            if actual.replace("\r\n", "\n") != expected:
                stale.append(str(path.relative_to(_ROOT)))
        if stale:
            print(
                "support matrix is stale: " + ", ".join(stale),
                "\nregenerate with: python scripts/support_matrix.py",
                file=sys.stderr,
            )
            return 1
        print("support matrix is current.")
        return 0

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(data, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")

    print(f"wrote {DATA_PATH.relative_to(_ROOT)} and {DOC_PATH.relative_to(_ROOT)}")
    for release, counts in matrix.totals.items():
        print(f"  {release:<14} {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
