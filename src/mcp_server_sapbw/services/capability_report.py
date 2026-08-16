"""Cross the capability contract with one system's discovery result.

Two facts have to meet before a question has an answer: the server must implement a reader, and the
connected release must actually have the object. Reporting either alone misleads. A customer on BW
7.4 asking why CompositeProvider lineage is thin needs to know whether the tables are missing on
their release or the reader is not written yet - those look identical from the outside and only one
of them is fixable here.

The verdict field says which case applies, per capability, in the customer's own system's terms.
"""

from __future__ import annotations

from ..core.contract import contract, contract_revision
from ..models.capability import (
    CapabilityRecord,
    CapabilityReport,
    CapabilitySupport,
    CapabilityVerdict,
    ContractEntry,
)

# How many capability names the "absent on this system" caveat lists before eliding. A caveat is a
# pointer to the list, not a second copy of it.
_CAVEAT_NAMES = 12


def _verdict(implemented: bool, present: bool | None) -> CapabilityVerdict:
    if present is None:
        # Declared, but discovery did not probe it on this release. Treat unknown presence as the
        # implementation question only, and let the caveat carry the uncertainty.
        return "usable" if implemented else "not_implemented"
    if implemented and present:
        return "usable"
    if implemented:
        return "absent_on_system"
    return "not_implemented" if present else "not_applicable"


def build_report(record: CapabilityRecord) -> CapabilityReport:
    """Join the shipped contract to a system's :class:`CapabilityRecord`."""
    entries = contract()
    caveats: list[str] = []
    if not entries:
        caveats.append(
            "The capability contract data file is missing from this installation, so only table "
            "presence is reported. Regenerate with: python scripts/capability_contract.py"
        )

    rows: list[CapabilitySupport] = []
    for name in sorted(set(entries) | set(record.tables)):
        entry = entries.get(name) or ContractEntry(
            capability=name,
            object_name=(record.tables[name].resolved_name or "") if name in record.tables else "",
            state="PLANNED",
            reason="Discovered on this system but absent from the shipped contract; "
            "the installed package and the resolver are out of step.",
        )
        status = record.tables.get(name)
        present = None if status is None else status.present
        rows.append(
            CapabilitySupport(
                capability=name,
                object_name=entry.object_name,
                state=entry.state,
                implementation=entry.implementation,
                validation=entry.validation,
                validated_on=entry.validated_on,
                reason=entry.reason,
                present=present,
                resolved_name=None if status is None else status.resolved_name,
                row_estimate=None if status is None else status.row_estimate,
                verdict=_verdict(entry.implemented, present),
            )
        )

    totals: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_validation: dict[str, int] = {}
    for row in rows:
        totals[row.verdict] = totals.get(row.verdict, 0) + 1
        by_state[row.state] = by_state.get(row.state, 0) + 1
        # Counted over the usable set only: how well-proven a capability is only matters for the
        # ones this system can actually use.
        if row.verdict == "usable":
            by_validation[row.validation] = by_validation.get(row.validation, 0) + 1

    untracked = sum(1 for row in rows if row.present is None)
    if untracked:
        caveats.append(
            f"{untracked} declared capabilities were not probed by discovery on this system, so "
            "their presence is unknown; the verdict reflects implementation only. Run "
            "bw_refresh_capabilities to re-probe."
        )
    absent = sorted(row.capability for row in rows if row.verdict == "absent_on_system")
    if absent:
        caveats.append(
            f"{len(absent)} capabilities are implemented here but absent on this system, so the "
            "features built on them return an unsupported result rather than a partial answer: "
            + ", ".join(absent[:_CAVEAT_NAMES])
            + (" ..." if len(absent) > _CAVEAT_NAMES else "")
        )
    revision = contract_revision()
    if revision:
        caveats.append(
            f"Implementation states come from contract revision {revision}, measured at build "
            "time; presence and row estimates come from this system's discovery record."
        )

    unproven = by_validation.get("not_validated", 0) + by_validation.get("unit_tested", 0)
    if unproven:
        caveats.append(
            f"{unproven} of the usable capabilities have not been verified against a real BW "
            "system - they are covered by the offline suite only, or by nothing. 'usable' means a "
            "reader exists and the object is present here; it does not mean the two have been "
            "proven to work together on a release like yours. Read the per-capability `validation` "
            "field before relying on one."
        )
    if not by_validation.get("customer_validated"):
        caveats.append(
            "No capability has been validated on a customer's own system yet. That is stated "
            "rather than omitted: it is the honest position of a pre-1.0 build, and it is the gap "
            "an onboarding exercise closes."
        )

    return CapabilityReport(
        system=record.system,
        bw_release=record.bw_release,
        totals=totals,
        by_state=by_state,
        by_validation=by_validation,
        capabilities=rows,
        caveats=caveats,
    )
