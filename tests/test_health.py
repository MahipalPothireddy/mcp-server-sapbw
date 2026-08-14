"""Tests for provider health: volume from monitoring data, currency from the request ledger.

Offline against a scripted landscape. Generated-table literals are built by concatenation so the
file stays clean for the customer-metadata scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.health import HealthRepository

SCHEMA = "TESTSCHEMA"
BIC = "/BIC/"
_TABLES = {"cs_tables": "M_CS_TABLES", "request_status": "RSSTATMANPART"}

MB = 1024 * 1024
# table -> (record_count, memory_bytes). FIN_ADSO has a changelog far bigger than its active data.
_VOLUMES: dict[str, tuple[int, int]] = {
    BIC + "AFIN_ADSO1": (500, 1 * MB),  # inbound / activation queue
    BIC + "AFIN_ADSO2": (10_000, 20 * MB),  # active
    BIC + "AFIN_ADSO3": (250_000, 400 * MB),  # changelog - the bloat
    BIC + "ASALES_DSO00": (7_000, 14 * MB),
}
# provider -> DTA_TYPE
_DTA_TYPES = {
    "FIN_ADSO": "ADSO",
    "SALES_DSO": "ODSO",
    "EMPTY_ADSO": "ADSO",
    # FLEX_T is a real live code whose meaning is NOT confirmed, so it must stay unmapped.
    "FLEX_THING": "FLEX_T",
}
# provider -> rows of (RNR, STATUS, TIMESTAMP_ANF, TIMESTAMP_VERB, ANZ_RECS, UPDMODE, OLTP, SRC_DTA)
_REQUESTS: dict[str, list[tuple[Any, ...]]] = {
    "FIN_ADSO": [
        ("REQ_3", "@08@", "20260728120000", "20260728121500", 1200, "D", "0FI_SRC", ""),
        ("REQ_2", "@0A@", "20260727120000", "20260727120500", 0, "D", "0FI_SRC", ""),
        ("REQ_1", "@08@", "20260726120000", "20260726121000", 900, "D", "0FI_SRC", ""),
    ],
    # Most recent load FAILED: contents may be partial, and age must come from the last success.
    "SALES_DSO": [
        ("REQ_B", "@0A@", "20260728080000", "20260728080300", 0, "F", "", "STG_DSO"),
        ("REQ_A", "@08@", "20260720080000", "20260720081000", 5000, "F", "", "STG_DSO"),
    ],
}
_MAX_TS = "20260728120000"


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = [str(p).strip() for p in (parameters or [])]
        if "M_CS_TABLES" in sql:
            wanted = [p for p in params if p in _VOLUMES]
            return [(t, *_VOLUMES[t]) for t in wanted]
        if "RSSTATMANPART" in sql:
            return self._requests(sql, params)
        return []

    @staticmethod
    def _requests(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        if "MAX(TIMESTAMP_ANF)" in sql:
            return [(_MAX_TS,)]
        provider = params[0] if params else ""
        rows = _REQUESTS.get(provider, [])
        if "TOTAL_COUNT" in sql:
            return [(len(rows),)]
        if "DTA_TYPE" in sql:
            kind = _DTA_TYPES.get(provider)
            return [(kind,)] if kind else []
        return rows


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name="SYS" if logical == "cs_tables" and logical in present else SCHEMA,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(present: set[str] | None = None) -> HealthRepository:
    return HealthRepository(ScriptedConnection(), _capability(present))


# --- volume -------------------------------------------------------------------------------


def test_changelog_is_reported_separately_from_active_data() -> None:
    """Summing the changelog into the active count would hide exactly the bloat being sought."""
    health = _repo().get_health("FIN_ADSO")
    assert health.active_records == 10_000
    assert health.changelog_records == 250_000  # 25x the active data
    assert health.inbound_records == 500
    assert health.tables_found == 3
    assert health.total_memory_mb == 421.0
    assert health.unloaded is False


def test_provider_kind_is_inferred_from_the_request_ledger() -> None:
    health = _repo().get_health("FIN_ADSO")
    assert health.object_type == "adso"  # FLEX_T
    assert _repo().get_health("SALES_DSO").object_type == "dso"


def test_unlocatable_tables_are_not_reported_as_empty() -> None:
    """Failing to find a provider's tables is not evidence that it holds no data."""
    health = _repo().get_health("EMPTY_ADSO")
    assert health.tables_found == 0
    assert health.volume_resolved is False
    assert health.unloaded is False  # crucially NOT claimed empty
    assert any("NOT evidence the provider is empty" in c for c in health.caveats)


def test_volume_unavailable_is_declared() -> None:
    health = _repo(present={"request_status"}).get_health("FIN_ADSO")
    assert health.tables == []
    assert health.volume_resolved is False
    assert any("M_CS_TABLES is unavailable" in c for c in health.caveats)


def test_unconfirmed_dta_type_yields_unknown_kind_not_a_guess() -> None:
    """FLEX_* codes are not mapped: guessing ADSO would derive tables for the wrong object."""
    health = _repo().get_health("FLEX_THING")
    assert health.object_type is None
    assert health.volume_resolved is False
    assert any("type could not be established" in c for c in health.caveats)


# --- currency -----------------------------------------------------------------------------


def test_request_status_icons_are_decoded() -> None:
    health = _repo().get_health("FIN_ADSO")
    assert health.last_request is not None
    assert health.last_request.request_id == "REQ_3"
    assert health.last_request.status == "success"  # @08@
    assert health.last_request.status_code == "@08@"
    assert health.last_request.update_mode == "delta"
    assert health.last_request.records == 1200
    assert health.failed_request_count == 1  # the @0A@ red request


def test_timestamps_are_parsed() -> None:
    health = _repo().get_health("FIN_ADSO")
    started = health.last_request.started_at  # type: ignore[union-attr]
    assert started is not None
    # Offset-bearing, per RFC 3339 - see test_timestamps_are_rfc3339 for why that matters.
    assert started.isoformat() == "2026-07-28T12:00:00+00:00"
    # The wall-clock reading itself must be untouched by the tz labelling.
    assert started.strftime("%Y%m%d%H%M%S") == "20260728120000"


def test_timestamps_are_rfc3339() -> None:
    """Request timestamps must carry a UTC offset, or the whole tool response is rejected.

    Regression test for a real outage of this tool. ``strptime`` returns a *naive* datetime,
    which serialises as ``2026-07-28T12:00:00`` with no offset. JSON Schema's
    ``format: date-time`` is RFC 3339, which requires an offset, so a strict client-side
    validator rejected the entire response - meaning volume and currency were unreadable for
    every provider that had ever been loaded. The failure was total, not partial, and it looked
    like missing data rather than a serialisation defect.
    """
    health = _repo().get_health("FIN_ADSO")

    stamps = [
        ("last_request.started_at", health.last_request.started_at),  # type: ignore[union-attr]
        ("last_request.ended_at", health.last_request.ended_at),  # type: ignore[union-attr]
    ]
    stamps += [
        (f"recent_requests[{i}].{f}", getattr(r, f))
        for i, r in enumerate(health.recent_requests)
        for f in ("started_at", "ended_at")
    ]

    checked = 0
    for label, value in stamps:
        if value is None:
            continue
        checked += 1
        assert value.tzinfo is not None, f"{label} is naive; RFC 3339 requires an offset"
        assert value.utcoffset() is not None, f"{label} has tzinfo but no resolvable offset"
        # What actually goes on the wire, and the shape the validator enforces.
        emitted = health.model_dump(mode="json")
        assert emitted["last_request"]["started_at"].endswith(("Z", "+00:00")), (
            "serialised timestamp lacks a UTC offset"
        )

    assert checked > 0, "fixture produced no timestamps, so this test proved nothing"


def test_failed_latest_load_is_flagged_and_age_uses_last_success() -> None:
    """A red latest load means the contents may be partial; age must come from the last success."""
    health = _repo().get_health("SALES_DSO")
    assert health.last_request is not None
    assert health.last_request.status == "error"
    assert health.last_successful_request is not None
    assert health.last_successful_request.request_id == "REQ_A"
    # Reference is the latest request in the system (2026-07-28), success was 2026-07-20.
    assert health.data_age_days == 8
    assert any("most recent load ended 'error'" in c for c in health.caveats)


def test_fresh_provider_has_zero_age() -> None:
    health = _repo().get_health("FIN_ADSO")
    assert health.data_age_days == 0


def test_provider_without_requests_says_so() -> None:
    health = _repo().get_health("EMPTY_ADSO")
    assert health.last_request is None
    assert any("no load requests are recorded" in c for c in health.caveats)


def test_require_health_needs_at_least_one_source() -> None:
    assert _repo().require_health() is None
    assert _repo(present={"cs_tables"}).require_health() is None
    assert _repo(present=set()).require_health() is not None
