"""Every bounded read must state an order (defect D8). A static invariant over the source.

**Why this is a test and not a review note.** A ``LIMIT`` over an unordered result set returns an
arbitrary subset, and the database is free to return a different one on the next call. Defect D7 was
one instance of that class - a cap over ``SYS.OBJECT_DEPENDENCIES`` that sampled roughly 0.2 usable
providers per execution against a true answer of 35, and drew a different subset each run. Fixing
that instance left the class open: an audit found **35** further bounded reads with no stated order,
across nine modules, feeding query results, security coverage findings, load closures, the routine
register and impact analysis.

Thirty-five sites cannot be held in place by review, and testing each reader's output individually
would be both enormous and incomplete - the next reader added would not be covered. So the invariant
is asserted over the source itself: if a call is bounded, its query says how the rows are ordered.
That makes the guard total rather than exemplary, and it fails on a *new* offender rather than on a
regression in an old one.

The behavioural counterpart lives in ``test_lineage_composite_consumers.py``, whose scripted
connection deliberately rotates rows when no ``ORDER BY`` is present, so the effect of the defect is
demonstrated on a real reader rather than only argued about here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mcp_server_sapbw"


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _nested_build_select(node: ast.expr) -> ast.Call | None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _called_name(sub) == "build_select":
            return sub
    return None


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parents.get(current)
    return None


def _base_query_for(name: str, function: ast.AST) -> ast.Call | None:
    """The ``build_select`` assigned to ``name`` inside ``function``, if there is one.

    Resolving the variable is what makes this check complete instead of merely suggestive: most list
    tools build their base query on one line, count it, then paginate it, so a check that only
    looked for a ``build_select`` written inline inside ``paginate(...)`` would pass nine real call
    sites without ever inspecting them.
    """
    for node in ast.walk(function):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                found = _nested_build_select(node.value)
                if found is not None:
                    return found
    return None


def _unordered_bounded_reads() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) != "paginate":
                continue
            if not node.args:
                continue
            query = _nested_build_select(node.args[0])
            if query is None and isinstance(node.args[0], ast.Name):
                function = _enclosing_function(node, parents)
                if function is not None:
                    query = _base_query_for(node.args[0].id, function)
            if query is None:
                # The base query could not be located statically. Reported rather than skipped: an
                # unresolvable site is a site nobody is checking.
                offenders.append(
                    f"{path.relative_to(_SRC.parent.parent)}:{node.lineno} "
                    "(base query could not be resolved statically)"
                )
                continue
            if _keyword(query, "order_by") is None:
                offenders.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}")
    return offenders


def test_every_bounded_read_states_an_order() -> None:
    offenders = _unordered_bounded_reads()
    assert not offenders, (
        "these reads are capped but do not state an order, so the rows they return are an "
        "arbitrary subset that may differ between identical calls (defect D8):\n  "
        + "\n  ".join(offenders)
    )


def test_the_check_can_actually_fail() -> None:
    """Guards the guard: a check that cannot fail is decoration.

    Asserts the two things the real check depends on - that a missing ``order_by`` is caught, and
    that a base query assigned to a variable is still inspected rather than passed over.
    """
    inline_offender = ast.parse(
        "rows = self.select(self.dialect.paginate("
        "self.dialect.build_select(columns=['A'], from_logical='t'), limit=5))"
    )
    call = next(
        node
        for node in ast.walk(inline_offender)
        if isinstance(node, ast.Call) and _called_name(node) == "paginate"
    )
    query = _nested_build_select(call.args[0])
    assert query is not None
    assert _keyword(query, "order_by") is None, "an inline missing order_by must be detectable"

    via_variable = ast.parse(
        "def f(self):\n"
        "    base = self.dialect.build_select(columns=['A'], from_logical='t')\n"
        "    return self.select(self.dialect.paginate(base, limit=5))\n"
    )
    function = next(node for node in ast.walk(via_variable) if isinstance(node, ast.FunctionDef))
    resolved = _base_query_for("base", function)
    assert resolved is not None, "a base query held in a variable must still be resolved"
    assert _keyword(resolved, "order_by") is None
