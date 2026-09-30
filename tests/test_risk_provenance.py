"""Every risk an analysis raises cites the record it rests on (mission Rule 3, REQ-17.2, REQ-18.1).

**Why this is a static invariant and not a behavioural test.** A ``Finding`` is the most
quotable thing this server produces: it says a named object has a named problem, at a
severity someone will act on. So it is the last place an unsourced claim belongs, and
mission Rule 3 admits no exception - provenance on every fact.

Found by auditing the written acceptance criteria rather than the code (D59). REQ-17
requires that each risk "cites the record it came from"; measured on the S05 payload,
**2 of its 3 risks carried no evidence at all** - both asserting that a named cube last
loaded a specific number of days ago, a figure read from a specific request row, citing
nothing. The failed-request risk beside them cited its provenance correctly, which is what
made the omission obvious: the mechanism was there and unused. Across the service, **24 of
30 risk sites** had the same gap.

Twenty-four sites cannot be held in place by review, and a newly added risk would not be covered -
the same reasoning that made D8's bounded-read invariant static. So the requirement is
asserted over the source: every ``run.risk(...)`` call passes ``evidence``.

**The honest exception, and why it is narrow.** Some risks are conclusions from an
*absence* - "no process chain loads this object". There is no row to cite, but there is
still something factual to report: which tables were read to establish the absence. So
those cite the tables consulted rather than being exempted, because "we looked in
RSPCCHAIN and RSBKDTP and found nothing" is a materially different claim from an
unsourced assertion that nothing exists. The exemption list below is therefore empty by
design; if a future risk genuinely cannot cite anything, adding it should require a
reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SERVICES = Path(__file__).resolve().parent.parent / "src" / "mcp_server_sapbw" / "services"

#: Deliberately empty. See the module docstring: an absence-derived risk cites the tables read to
#: establish it, so there is no category of risk that legitimately carries no evidence.
_EXEMPT: frozenset[tuple[str, int]] = frozenset()


def _risk_calls(path: Path) -> list[tuple[int, bool, str]]:
    """``(line, cites_evidence, title_expression)`` for every ``*.risk(...)`` call in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[tuple[int, bool, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "risk":
            continue
        cites = any(keyword.arg == "evidence" for keyword in node.keywords)
        # The third positional argument is the title, which identifies the site far better than a
        # line number alone when this test fails.
        title = ast.unparse(node.args[2])[:80] if len(node.args) >= 3 else "<no title>"
        calls.append((node.lineno, cites, title))
    return calls


def test_every_raised_risk_cites_its_source() -> None:
    offenders: list[str] = []
    total = 0
    for path in sorted(_SERVICES.glob("*.py")):
        for lineno, cites, title in _risk_calls(path):
            total += 1
            if cites or (path.name, lineno) in _EXEMPT:
                continue
            offenders.append(f"{path.name}:{lineno}  {title}")

    assert total, "no risk call sites found; the invariant is not actually checking anything"
    assert not offenders, (
        "these risks are raised without citing the record they rest on, so a reader"
        " cannot check "
        "them and mission Rule 3 breaks in the most quotable part of the payload (defect D59):\n  "
        + "\n  ".join(offenders)
    )


def test_the_invariant_covers_the_service_layer_it_claims_to() -> None:
    """A guard that silently stopped finding call sites would pass forever.

    The bounded-read invariant needed this lesson: an AST matcher that stops matching is
    indistinguishable from a codebase with nothing to match.
    """
    counts = {path.name: len(_risk_calls(path)) for path in sorted(_SERVICES.glob("*.py"))}
    analysing = counts.get("analysis.py", 0)
    assert analysing >= 25, (
        f"expected the analysis service to raise many risks; found {analysing}. If risks moved "
        f"elsewhere, point this invariant at them. Counts: "
        f"{ {k: v for k, v in counts.items() if v} }"
    )
