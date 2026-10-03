"""Every table an analysis section cites is a real capability key (D60).

``AnalysisStep.source_tables`` is the section-level half of the provenance contract: it says which
physical tables an answer rests on, so a reader can check a section without reverse-engineering the
compound tool. It is produced by resolving the logical names passed to ``run.step(..., tables=...)``
through the capability record.

**The failure this prevents is silent and looks authoritative.** ``Repository.physical()``
falls back to returning the logical name when it does not recognise it, which is a
reasonable thing for a resolver to do and a terrible thing for a citation: one section
passed ``"auth_value"`` where the capability key is ``"auth_values"``, so the payload
cited a table named ``auth_value``, which exists on no BW system anywhere. The number
beside it was correct and the source under it was fiction.

Nothing caught it for the life of the build, and the reason is worth recording: the offline
fixture's own table map carried the same singular spelling, so the resolution "succeeded"
in every test. A fixture that agrees with the bug cannot detect the bug. Hence this guard
reads the **real** capability map rather than a test one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mcp_server_sapbw.core.capabilities import ABAP_TABLES, HANA_VIEWS, REPO_TABLES

_SERVICES = Path(__file__).resolve().parent.parent / "src" / "mcp_server_sapbw" / "services"


def _cited_logical_names(path: Path) -> list[tuple[int, str]]:
    """``(line, logical name)`` for every literal passed as ``tables=`` to a section recorder."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {
            "step",
            "anchor",
            "note_section",
        }:
            continue
        for keyword in node.keywords:
            if keyword.arg != "tables":
                continue
            for element in ast.walk(keyword.value):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    found.append((node.lineno, element.value))
    return found


def test_every_section_cites_a_real_capability_key() -> None:
    # Every existence tier, because a section may legitimately cite an ABAP table, a HANA catalog
    # view or a repository object.
    known = {*ABAP_TABLES, *HANA_VIEWS, *REPO_TABLES}
    offenders: list[str] = []
    checked = 0
    for path in sorted(_SERVICES.glob("*.py")):
        for lineno, logical in _cited_logical_names(path):
            checked += 1
            if logical not in known:
                near = sorted(k for k in known if k.startswith(logical[:6]))
                offenders.append(
                    f"{path.name}:{lineno}  {logical!r} is not a capability key"
                    + (f"; did you mean {near}?" if near else "")
                )

    assert checked, "no section table citations found; this guard is not checking anything"
    assert not offenders, (
        "these sections cite a logical name the capability map does not have, so"
        " `physical()` falls back to the logical name and the payload names a table that"
        " does not exist (defect D60):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_have_caught_the_defect_that_motivated_it(tmp_path: Path) -> None:
    """Proof the matcher works, because the source was fixed before this guard existed.

    A guard nobody has watched fail is an assumption, not a test. The exact typo is replayed here -
    the singular ``auth_value`` against a capability map that has only the plural - so the detection
    is demonstrated rather than asserted.
    """
    module = tmp_path / "fake_service.py"
    module.write_text(
        "def build(run):\n"
        "    run.step('security', 'bw_get_query_auth_exposure', lambda: None,\n"
        "             tables=('auth_value',))\n"
        "    run.step('currency', 'bw_get_provider_health', lambda: None,\n"
        "             tables=('request_status',))\n",
        encoding="utf-8",
    )
    known = {*ABAP_TABLES, *HANA_VIEWS, *REPO_TABLES}
    cited = _cited_logical_names(module)
    assert {name for _line, name in cited} == {"auth_value", "request_status"}

    bad = [name for _line, name in cited if name not in known]
    assert bad == ["auth_value"], "the singular form must be rejected"
    assert "auth_values" in known, "and the plural must be the one that exists"


def test_the_guard_sees_the_citations_it_claims_to() -> None:
    """A matcher that stops matching passes forever; the count is asserted so it cannot."""
    total = sum(len(_cited_logical_names(path)) for path in sorted(_SERVICES.glob("*.py")))
    assert total >= 20, (
        f"expected many section table citations across the service layer; found {total}. If they "
        "moved, point this guard at them rather than deleting it."
    )
