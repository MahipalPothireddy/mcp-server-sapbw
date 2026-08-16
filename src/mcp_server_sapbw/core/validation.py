"""How far the capabilities behind one tool have been proven, by the weakest link.

**The question this answers.** A compound answer draws on six or seven readers. If five are
integration-tested and one has never been touched by a test, the answer as a whole is only as proven
as that one - and a caller reading "integration_tested" would be trusting a section nothing has
checked. So the status reported for a tool is the *minimum* across everything it reads, never an
average and never the best case.

**Both inputs are measured, not asserted.** The capabilities a tool reads come from the support
matrix, recorded by attributing each metadata read to the tool that caused it while the suite runs.
How far each capability is proven comes from the capability contract, which records what the suite
measurably touched. This module only crosses them; it invents nothing.

**It cannot report ``real_bw_validated``.** That rung is emitted by the validation matrix - a
recorded scenario with an expected answer and a correctness verdict - not by anything the offline
build can observe. A tool whose readers all work against a live system still reports
``integration_tested`` here until a scenario has checked that the answer was *right*.
"""

from __future__ import annotations

from ..models.capability import VALIDATION_RANK, ValidationStatus
from .contract import contract
from .support import support_matrix

#: The floor. Used when the inputs are unavailable or a capability is unknown to the contract, so an
#: unmeasurable case degrades downward rather than inheriting a claim it has not earned.
UNPROVEN: ValidationStatus = "not_validated"


def validation_for_tool(tool: str) -> tuple[ValidationStatus, str]:
    """``(weakest validation across the tool's capabilities, how that was reached)``.

    The second element names the weakest capability, because a bare status is not checkable: a
    customer disputing it has to be able to see which reader is holding the answer back.
    """
    matrix = support_matrix()
    entries = contract()
    if matrix is None or not entries:
        return UNPROVEN, (
            "the shipped support matrix or capability contract is unavailable in this "
            "installation, so how far these readers have been proven could not be established - "
            "reported as unproven rather than assumed."
        )

    support = matrix.tool(tool)
    if support is None:
        return UNPROVEN, (
            f"{tool} is not in the shipped support matrix, so the capabilities it reads are not "
            "known here and no validation level can be claimed for it."
        )

    if not support.requires:
        if support.reads_nothing:
            return "integration_tested", (
                f"{tool} was measured to read no BW metadata - it answers from data shipped in the "
                "package - so there is no reader whose validation could limit it."
            )
        return UNPROVEN, (
            f"what {tool} reads was never measured, so its validation level is unknown rather than "
            "high. An unmeasured requirement set is not an empty one."
        )

    known = {name: entries[name] for name in support.requires if name in entries}
    unknown = sorted(set(support.requires) - set(known))
    if not known:
        return UNPROVEN, (
            f"none of the {len(support.requires)} capabilities {tool} reads appear in the "
            "capability contract, so nothing here establishes how far they are proven."
        )

    weakest_name = min(known, key=lambda n: (VALIDATION_RANK.get(known[n].validation, 0), n))
    weakest = known[weakest_name].validation
    basis = (
        f"the weakest of the {len(known)} capabilities {tool} was measured to read: "
        f"`{weakest_name}` is {weakest}"
    )
    release = known[weakest_name].validated_on
    if release:
        basis += f", verified on {release}"
    if unknown:
        basis += (
            f". {len(unknown)} capability name(s) it reads are absent from the contract "
            f"({', '.join(unknown[:3])}) and were excluded, so this may be optimistic"
        )
    return weakest, basis + "."
