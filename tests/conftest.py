"""Shared pytest configuration.

The capability-read recorder is switched on for the whole session when
``BW_RECORD_CAPABILITY_READS`` names an output file. ``scripts/capability_contract.py`` uses that to
measure which declared logical tables the code actually reads, rather than inferring it from a grep
that cannot see the eight call sites passing the name as a variable.

Recording is inert during a normal test run: without the environment variable this file adds one
session fixture that does nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_server_sapbw.core.dialect import record_reads

_ENV_VAR = "BW_RECORD_CAPABILITY_READS"


@pytest.fixture(scope="session", autouse=True)
def _capability_read_recorder() -> Iterator[None]:
    target = os.environ.get(_ENV_VAR)
    if not target:
        yield
        return
    with record_reads() as observed:
        yield
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(observed), indent=2), encoding="utf-8")
