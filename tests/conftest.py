"""Shared pytest fixtures.

The entire test suite runs offline. No test may open a network connection or touch a
live BW system. Metadata fixtures live under tests/fixtures/ and use synthetic names
only (never /BIC/<concrete>, /BI0/<concrete>, or real chain/DSO prefixes).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Absolute path to the synthetic metadata fixtures directory."""
    return Path(__file__).parent / "fixtures"
