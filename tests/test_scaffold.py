"""Scaffold smoke tests (build prompt B0).

These assert only that the package imports and its structure is wired correctly. Real
behavior is tested from build prompt B1 onward against synthetic fixtures.
"""

from __future__ import annotations

import importlib

import pytest

import mcp_server_sapbw
from mcp_server_sapbw import server

SUBPACKAGES = [
    "mcp_server_sapbw.core",
    "mcp_server_sapbw.repositories",
    "mcp_server_sapbw.services",
    "mcp_server_sapbw.connectors",
    "mcp_server_sapbw.models",
    "mcp_server_sapbw.prompts",
]


def test_package_version() -> None:
    assert mcp_server_sapbw.__version__ == "0.1.0"


@pytest.mark.parametrize("module_name", SUBPACKAGES)
def test_subpackages_import(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_server_entry_point_exists() -> None:
    """The console entry point and FastMCP instance exist (do not call main(); it blocks)."""
    assert callable(server.main)
    assert server.mcp is not None
