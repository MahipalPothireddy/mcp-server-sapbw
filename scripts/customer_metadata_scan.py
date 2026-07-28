"""Customer-metadata leak check (mission Section 10) - reusable, cross-platform.

Fails if a customer object-naming pattern appears outside ``tests/fixtures/`` in the WORKING TREE
or ANYWHERE IN GIT HISTORY (a clean tip commit is meaningless if an earlier commit leaked a name).
Two patterns:

1. generated tables:  ``/BIC/<name>`` or ``/BI0/<name>`` (3+ name chars);
2. customer namespace: whole tokens starting ``Z`` or ``Y`` with 4+ upper/digit/underscore chars.

Documentation and source use the bare ``/BIC/`` or ``/BIC/<...>`` placeholder form, which pattern 1
does not match. The allow-list holds generic non-customer tokens that happen to match pattern 2.

This is the single source of truth for the check: CI runs it, and it is the same code used locally
(no bash/Python drift). Runs on Linux and Windows (pure Python + ``git``; uses ``git grep -P``).

Usage (from the repo root):

    python scripts/customer_metadata_scan.py

Exit code 0 = clean, 1 = potential leak (details printed), 2 = usage error.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Pattern 1 (/BIC//BI0/ generated tables) | pattern 2 (Z*/Y* customer namespace tokens).
PATTERN = r"(/(?:BIC|BI0)/[A-Za-z0-9_]{3,})|(\b[ZY][A-Z0-9_]{4,}\b)"
_COMBINED = re.compile(PATTERN)

# Generic non-customer tokens that match pattern 2 but are not object names:
#   ZZ_TEST                    - throwaway/copy-artifact marker the description service detects
#   YYYYMMDD / YYYYMMDDHHMMSS   - SAP date/timestamp FORMAT placeholders (process-log parser)
#   ZY_FIELDS                  - a former internal SQL column alias (since renamed) mentioned once
#                                in an earlier commit's PROGRESS.md prose
#   ZXRSAU01..04               - SAP-DEFINED customer includes of enhancement RSAP0001 (the four BW
#                                extractor exits). Named by SAP's own ZXnnnU01 convention, identical
#                                on every ABAP system, and carrying no customer information. They
#                                sit in the customer namespace only because SAP put them there.
ALLOW = {
    "ZZ_TEST",
    "YYYYMMDD",
    "YYYYMMDDHHMMSS",
    "ZY_FIELDS",
    "ZXRSAU01",
    "ZXRSAU02",
    "ZXRSAU03",
    "ZXRSAU04",
}

# Working-tree directories never scanned (git-ignored artefacts, caches, and the fixtures dir where
# synthetic sample names deliberately live).
SKIP_DIRS = {
    ".git",
    ".venv",
    "fixtures",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "output",
    "extracts",
    "cache",
}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
    ".example",
    "",
    ".lock",
}
_ALWAYS_SCAN_NAMES = {".gitignore", ".gitattributes"}


def _tokens(text: str) -> set[str]:
    # Strip backslashes first: they are SQL-LIKE / regex escape artefacts, never part of a BW
    # object name, and they would otherwise split a real name mid-token and hide it from the
    # pattern (e.g. an escaped "ABC\_O3" reads as the too-short "ABC"). Found by a live miss.
    normalized = text.replace("\\", "")
    return {m.group(0) for m in _COMBINED.finditer(normalized)} - ALLOW


def scan_working_tree() -> set[str]:
    found: set[str] = set()
    for path in _ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in _ALWAYS_SCAN_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = _tokens(text)
        if hits:
            print(f"  working tree {rel}: {sorted(hits)}")
            found |= hits
    return found


def scan_history() -> set[str]:
    revs = subprocess.run(
        ["git", "rev-list", "--all"], cwd=_ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    if not revs:
        return set()
    proc = subprocess.run(
        ["git", "grep", "-hIoP", PATTERN, *revs, "--", ".", ":(exclude)tests/fixtures/*"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        token = line.strip()
        if token and token not in ALLOW and _COMBINED.fullmatch(token):
            found.add(token)
    return found


def main() -> int:
    working_tree = scan_working_tree()
    history = scan_history()
    if working_tree or history:
        print("\nPotential customer object tokens detected:")
        if working_tree:
            print(f"  working tree: {sorted(working_tree)}")
        if history:
            print(f"  git history:  {sorted(history)}")
        print(
            "\nCustomer object names must never be committed (working tree or history). "
            "Move synthetic examples into tests/fixtures/; use placeholder forms elsewhere. "
            "If a name reached history, purge it with git filter-repo, not just a delete."
        )
        return 1
    print("Customer-metadata leak check clean (working tree + full history).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
