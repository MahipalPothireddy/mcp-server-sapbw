"""Minimal SQL ``LIKE`` evaluator for the offline fixtures.

Repositories build ``LIKE`` conditions via :func:`mcp_server_sapbw.core.dialect.like_term`, so a
scripted fixture connection has to honour those patterns the way HANA would. A fixture that just
does a substring check would happily pass a pattern the database matches differently — which is
exactly how the underscore-escaping defect survived the original suite.

Supports ``%`` (any run), ``_`` (one character), and an optional escape character. Matching is
case-sensitive, as in HANA: callers mirror a production ``UPPER(col)`` by upper-casing the value.
"""

from __future__ import annotations

import re

from mcp_server_sapbw.core.dialect import LIKE_ESCAPE


def like_to_regex(pattern: str, escape: str | None = LIKE_ESCAPE) -> re.Pattern[str]:
    """Compile a SQL ``LIKE`` pattern into an equivalent anchored regex."""
    parts = [r"\A"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if escape and char == escape and index + 1 < len(pattern):
            parts.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
        index += 1
    parts.append(r"\Z")
    return re.compile("".join(parts), re.DOTALL)


def matches_like(pattern: str, value: str, escape: str | None = LIKE_ESCAPE) -> bool:
    """True when ``value`` satisfies the SQL ``LIKE`` ``pattern``."""
    return like_to_regex(pattern, escape).match(value) is not None


def escape_for_sql(sql: str) -> str | None:
    """The escape character in force for a built condition, mirroring its ``ESCAPE`` clause.

    Without an ``ESCAPE`` clause HANA has no escape character, so a backslash is a literal.
    """
    return LIKE_ESCAPE if "ESCAPE" in sql else None
