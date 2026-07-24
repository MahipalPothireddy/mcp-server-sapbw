"""Tests for the cross-cutting core models (B1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import Provenance, UnsupportedResult


class TestProvenance:
    def test_holds_table_and_key(self) -> None:
        prov = Provenance(source_table="RSTRAN", source_key={"TRANID": "0ABC123", "OBJVERS": "A"})
        assert prov.source_table == "RSTRAN"
        assert prov.source_key["TRANID"] == "0ABC123"

    def test_is_frozen(self) -> None:
        prov = Provenance(source_table="RSTRAN")
        with pytest.raises(ValidationError):
            prov.source_table = "RSDS"

    def test_rejects_empty_table(self) -> None:
        with pytest.raises(ValidationError):
            Provenance(source_table="")

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            Provenance(source_table="RSTRAN", bogus="x")  # type: ignore[call-arg]


class TestUnsupportedResult:
    def test_default_status(self) -> None:
        result = UnsupportedResult(release="7.50", detail="ADSO tables absent", missing=["RSOADSO"])
        assert result.status == "unsupported_on_release"
        assert result.alternative is None
        assert "RSOADSO" in result.missing

    def test_carries_alternative(self) -> None:
        result = UnsupportedResult(
            release="7.40",
            detail="no ADSO on this release",
            missing=["RSOADSO"],
            alternative="RSDODSO",
        )
        assert result.alternative == "RSDODSO"


class TestTableStatus:
    def test_defaults(self) -> None:
        status = TableStatus(logical_name="rstran")
        assert status.present is False
        assert status.resolved_name is None
        assert status.tier == "existence"

    def test_discover_tier(self) -> None:
        status = TableStatus(
            logical_name="composite_provider_header",
            resolved_name="RSOHCPR",
            tier="discover",
            present=True,
            schema_name="SAPHANADB",
            row_estimate=42,
        )
        assert status.tier == "discover"
        assert status.schema_name == "SAPHANADB"


def _record(**overrides: object) -> CapabilityRecord:
    base: dict[str, object] = {
        "system": "qa",
        "bw_release": "7.50",
        "abap_schema": "SAPHANADB",
        "object_models": {"classic_dso": True, "adso": True, "composite_provider": False},
        "hana_repo_style": "sys_repo",
        "processlog_retention_days": 30,
        "tables": {
            "rstran": TableStatus(
                logical_name="rstran", resolved_name="RSTRAN", present=True, schema_name="SAPHANADB"
            ),
            "adso_header": TableStatus(logical_name="adso_header", present=False),
        },
        "discovered_at": datetime.now(UTC),
    }
    base.update(overrides)
    return CapabilityRecord.model_validate(base)


class TestCapabilityRecord:
    def test_is_available_true_for_present_resolved(self) -> None:
        assert _record().is_available("rstran") is True

    def test_is_available_false_for_absent(self) -> None:
        assert _record().is_available("adso_header") is False

    def test_is_available_false_for_untracked(self) -> None:
        assert _record().is_available("does_not_exist") is False

    def test_has_object_model(self) -> None:
        record = _record()
        assert record.has_object_model("adso") is True
        assert record.has_object_model("composite_provider") is False
        assert record.has_object_model("unknown_variant") is False

    def test_not_expired_when_fresh(self) -> None:
        assert _record().is_expired() is False

    def test_expired_when_past_ttl(self) -> None:
        old = datetime.now(UTC) - timedelta(seconds=100)
        record = _record(discovered_at=old, ttl_seconds=10)
        assert record.is_expired() is True

    def test_naive_discovered_at_treated_as_utc(self) -> None:
        naive_old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
        record = _record(discovered_at=naive_old, ttl_seconds=86400)
        assert record.is_expired() is True
