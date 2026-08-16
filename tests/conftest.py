"""Shared pytest configuration.

Two build-time measurements are switched on by environment variables, and both are inert without
them - a normal test run adds two session fixtures that do nothing.

``BW_RECORD_CAPABILITY_READS`` names a file to write the logical tables the code actually read.
``scripts/capability_contract.py`` uses it to measure implementation rather than inferring it from a
grep that cannot see the call sites passing the name as a variable.

``BW_RECORD_TOOL_READS`` names a file to write the same thing **attributed per tool**.
``scripts/support_matrix.py`` uses it, because the support matrix answers a different question:
which capabilities does *this tool* need. Hand-maintaining that across 55 tools would drift the
first time a tool gained a reader, and drift silently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_server_sapbw.core.dialect import (
    enable_tool_attribution,
    record_reads,
    reset_tool_attribution,
    tool_attribution,
)

_ENV_VAR = "BW_RECORD_CAPABILITY_READS"
_TOOL_ENV_VAR = "BW_RECORD_TOOL_READS"


def _write(target: str, payload: object) -> None:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _capability_read_recorder() -> Iterator[None]:
    target = os.environ.get(_ENV_VAR)
    if not target:
        yield
        return
    with record_reads() as observed:
        yield
    _write(target, sorted(observed))


@pytest.fixture(scope="session", autouse=True)
def _tool_read_recorder() -> Iterator[None]:
    target = os.environ.get(_TOOL_ENV_VAR)
    if not target:
        yield
        return
    reset_tool_attribution()
    enable_tool_attribution()
    yield
    _write(target, {tool: sorted(reads) for tool, reads in tool_attribution().items()})
