"""The capability contract, as data the server can read at runtime.

``scripts/capability_contract.py`` establishes what the server does with each declared capability
by measurement - a static scan of the readers, unioned with the logical names the SQL dialect was
actually asked for while the test suite ran. That measurement needs a test run, so it happens at
build time and lands here as a shipped data file. The server consumes it; it never recomputes it.

Two consumers:

* ``bw_capability_report`` crosses these states with what discovery found on a connected system, so
  a customer can see which questions have an answer on *their* release rather than in principle.
* ``tests/test_capability_contract.py`` asserts the data file still matches what the resolver
  declares, which is what stops the schema from claiming an understanding the code does not have.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from ..models.capability import ContractEntry

# Shipped inside the package (not read from the repo tree) so an installed wheel behaves the same
# as a source checkout. Regenerate with: python scripts/capability_contract.py
_DATA_PACKAGE = "mcp_server_sapbw.data"
_DATA_FILE = "capability_contract.json"


@lru_cache(maxsize=1)
def _payload() -> dict[str, object]:
    """The shipped data file, or an empty payload when it is absent.

    Absence is not fatal: the contract describes the server, so a missing file degrades
    ``bw_capability_report`` to a stated caveat rather than breaking metadata extraction.
    """
    try:
        raw = resources.files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, TypeError):  # pragma: no cover - packaging
        return {}
    loaded: dict[str, object] = json.loads(raw)
    return loaded


@lru_cache(maxsize=1)
def contract() -> dict[str, ContractEntry]:
    """Every declared capability mapped to its implementation state."""
    rows = _payload().get("capabilities")
    if not isinstance(rows, list):
        return {}
    entries = [ContractEntry.model_validate(row) for row in rows]
    return {entry.capability: entry for entry in entries}


def contract_revision() -> str | None:
    """Identifies the shipped contract exactly: package version plus a digest of its content.

    A wall-clock generation timestamp was rejected: it records when somebody ran a script, not
    which contract this is, and it would make the ``--check`` comparison non-deterministic.
    """
    revision = _payload().get("revision")
    return revision if isinstance(revision, str) else None


def state_of(capability: str) -> str | None:
    """The contract state for one capability, or ``None`` when it is not declared."""
    entry = contract().get(capability)
    return None if entry is None else entry.state
