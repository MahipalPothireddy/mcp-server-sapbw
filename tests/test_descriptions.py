"""Tests for the description subsystem (B4): quality assessment + labelled generation.

Pure-logic tests (no connection). Synthetic names only.
"""

from __future__ import annotations

import pytest

from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.repositories.texts import StoredText
from mcp_server_sapbw.services.descriptions import DescriptionService


def _stored(short: str | None, long: str | None = None, language: str = "E") -> StoredText:
    return StoredText(
        short=short,
        long=long,
        language=language,
        provenance=Provenance(source_table="RSDODSOT", source_key={"ODSOBJECT": "SALES_DSO"}),
    )


@pytest.mark.parametrize(
    ("short", "technical_name", "expected"),
    [
        (None, "SALES_DSO", "missing"),
        ("   ", "SALES_DSO", "missing"),
        ("Copy of Sales orders DSO", "SALES_DSO", "copy_artifact"),
        ("ZZ_TEST", "SALES_DSO", "copy_artifact"),
        ("tmp", "SALES_DSO", "copy_artifact"),
        ("obsolete do not use", "SALES_DSO", "copy_artifact"),
        ("SALES_DSO", "SALES_DSO", "generic"),  # equals technical name
        ("Sales data", "SALES_DSO", "generic"),  # < 4 words
        ("Daily sales order line items", "SALES_DSO", "ok"),
    ],
)
def test_assess_quality(short: str | None, technical_name: str, expected: str) -> None:
    assert DescriptionService().assess(short, technical_name) == expected


def test_build_ok_returns_stored_verbatim() -> None:
    stored = _stored("Sales orders", long="Complete sales order line item detail")
    result = DescriptionService().build(
        technical_name="SALES_DSO",
        stored=stored,
        generated_summary="ignored",
        evidence=["RSDODSOT"],
    )
    assert result.origin == "stored"
    assert result.quality_flag == "ok"
    assert result.description_short == "Sales orders"
    assert result.description_long == "Complete sales order line item detail"
    assert result.language == "E"


def test_build_missing_uses_generated_summary() -> None:
    result = DescriptionService().build(
        technical_name="SALES_DSO",
        stored=None,
        generated_summary="Classic DSO in info area SALES with 12 fields; key: DOC, ITEM.",
        evidence=["RSDODSO", "RSDODSOIOBJ"],
    )
    assert result.origin == "generated"
    assert result.quality_flag == "missing"
    assert result.description_short is not None
    assert "Classic DSO" in result.description_short
    # A generated description must not claim a stored language.
    assert result.language is None


def test_build_missing_without_summary_is_honest_placeholder() -> None:
    result = DescriptionService().build(
        technical_name="SALES_DSO", stored=None, generated_summary=None, evidence=[]
    )
    assert result.origin == "stored"
    assert result.quality_flag == "missing"
    assert result.description_short is None


def test_build_copy_artifact_replaced_by_generated() -> None:
    stored = _stored("Copy of SALES_DSO")
    result = DescriptionService().build(
        technical_name="SALES_DSO",
        stored=stored,
        generated_summary="Advanced DSO in info area SALES with 8 fields.",
        evidence=["RSOADSO"],
    )
    assert result.origin == "generated"
    assert result.quality_flag == "copy_artifact"
    assert result.description_short == "Advanced DSO in info area SALES with 8 fields."


def test_build_generic_augments_stored() -> None:
    stored = _stored("Sales data")  # < 4 words -> generic
    result = DescriptionService().build(
        technical_name="SALES_DSO",
        stored=stored,
        generated_summary="Classic DSO in info area SALES with 12 fields.",
        evidence=["RSDODSO"],
    )
    assert result.origin == "stored_augmented"
    assert result.quality_flag == "generic"
    # The human-entered short text is preserved, generated context added as long.
    assert result.description_short == "Sales data"
    assert result.description_long == "Classic DSO in info area SALES with 12 fields."
