"""Tests for provider health: volume from monitoring data, currency from the request ledger.

Offline against a scripted landscape. Generated-table literals are built by concatenation so the
file stays clean for the customer-metadata scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.health import (
    HealthRepository,
    _parse_timestamp,
    _parse_tsn,
)

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
    # FLEX_T / FLEX_M are InfoObject master-data loads (text and attribute). Measured: on the
    # reference system all 214 FLEX_T and all 82 FLEX_M values resolve to RSDIOBJ, none unresolved.
    "FLEX_TEXTS": "FLEX_T",
    "FLEX_ATTRS": "FLEX_M",
    # A code whose meaning genuinely is not established, so the don't-guess rule still has a test.
    # Deliberately not a Z*/Y* form: that is the customer namespace, and the leak scan flags it.
    "MYSTERY_THING": "UNMAPPED",
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


def test_flex_dta_types_are_infoobject_master_data_loads() -> None:
    """Measured, replacing a documented blank.

    These were unmapped on the evidence that "28 of 40 sampled were in no provider catalogue at
    all" - a check that looked only at *provider* catalogues. Against every catalogue, all 214
    FLEX_T and all 82 FLEX_M values on the reference system resolve to RSDIOBJ with nothing
    unresolved: FLEX_T is the text load, FLEX_M the attribute load.
    """
    assert _repo().get_health("FLEX_TEXTS").object_type == "infoobject"
    assert _repo().get_health("FLEX_ATTRS").object_type == "infoobject"


def test_an_unconfirmed_dta_type_still_yields_unknown_rather_than_a_guess() -> None:
    """The don't-guess rule survives: a code with no established meaning stays unmapped."""
    health = _repo().get_health("MYSTERY_THING")
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


# --- D1: Advanced-DSO currency from the TSN request framework ------------------------------
#
# Regression tests for a measured defect. On the reference system RSSTATMANPART holds 1.46 million
# rows and not one is an Advanced DSO (DTA_TYPE spread: CUBE / FLEX_T / FLEX_M / ODSO only), while
# RSPMREQUEST holds 2.1 million ADSO request rows. A single-ledger reader therefore reported "no
# load requests are recorded" for 248 of 248 active ADSOs - the dominant provider type - and, worse,
# could not even establish their *kind*, so volume failed too.
#
# The landscape below reproduces that shape: an ADSO recorded only in RSPMREQUEST, a classic DSO
# recorded only in RSSTATMANPART, and no ADSO row in the classic ledger at all.

_RSPM_TABLES = {
    "cs_tables": "M_CS_TABLES",
    "request_status": "RSSTATMANPART",
    "adso_request": "RSPMREQUEST",
}

# DATATARGET -> rows of (REQUEST_TSN, REQUEST_STATUS, LAST_TIME_STAMP, CREATION_END_TIME,
#                        RECORDS, SOURCE, LAST_OPERATION_TYPE), newest first.
# TSNs are NUMC(23): 14 digits of YYYYMMDDHHMMSS then 9 of sub-second precision and a counter.
_RSPM_AT: dict[str, list[tuple[Any, ...]]] = {
    # Newest active-table request is a *deleted* one - measured as the common case, not a corner.
    "FIN_ADSO": [
        (
            "20260817011127000055000",
            "D",
            "20260817011127000055000",
            "20260817011126000071000",
            0,
            "SRC_DTP_01",
            "D",
        ),
        (
            "20260816020000000010000",
            "GG",
            "20260816020100000010000",
            "20260816020000000010000",
            57_917,
            "SRC_DTP_01",
            "C",
        ),
        (
            "20260815020000000010000",
            "RG",
            "20260815020100000010000",
            "20260815020000000010000",
            12,
            "SRC_DTP_01",
            "C",
        ),
    ],
    # Only ever loaded into the activation queue and changelog, never activated: no AT rows.
    "UNACTIVATED_ADSO": [],
}
# Rows the AT filter must exclude. If the filter is dropped these become visible and the newest
# request for FIN_ADSO changes, so the test below is a real guard rather than a restatement.
_RSPM_OTHER_LAYERS: dict[str, list[tuple[Any, ...]]] = {
    "FIN_ADSO": [
        (
            "20260818090000000010000",
            "GG",
            "20260818090000000010000",
            "20260818090000000010000",
            999_999,
            "SRC_DTP_01",
            "C",
        ),
    ],
    "UNACTIVATED_ADSO": [
        (
            "20260818090000000010000",
            "GG",
            "20260818090000000010000",
            "20260818090000000010000",
            42,
            "SRC_DTP_02",
            "C",
        ),
    ],
}
_RSPM_TLOGO = {"FIN_ADSO": "ADSO", "UNACTIVATED_ADSO": "ADSO", "ODD_TARGET": "QQQQ"}


class TwoLedgerConnection(ScriptedConnection):
    """RSSTATMANPART populated but holding no ADSO row, as measured on the reference system."""

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = [str(p).strip() for p in (parameters or [])]
        if "RSPMREQUEST" in sql:
            return self._rspm(sql, params)
        if "RSSTATMANPART" in sql:
            # The classic ledger knows the classic DSO and the system's reference date, and nothing
            # whatsoever about any Advanced DSO.
            if "MAX(TIMESTAMP_ANF)" in sql:
                return [(_MAX_TS,)]
            provider = params[0] if params else ""
            if provider not in ("SALES_DSO",):
                return []
            return super()._requests(sql, params)
        return super().execute_select(sql, parameters)

    @staticmethod
    def _rspm(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        provider = params[0] if params else ""
        if "TLOGO" in sql:
            code = _RSPM_TLOGO.get(provider)
            return [(code,)] if code else []
        rows = list(_RSPM_AT.get(provider, []))
        if "STORAGE" not in sql:  # the filter was dropped: every layer becomes visible
            rows += _RSPM_OTHER_LAYERS.get(provider, [])
            rows.sort(key=lambda r: str(r[2]), reverse=True)
        if "COUNT(*)" in sql:
            return [(len(rows),)]
        return rows


def _two_ledger_repo() -> HealthRepository:
    capability = CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical,
                present=True,
                schema_name="SYS" if logical == "cs_tables" else SCHEMA,
            )
            for logical, physical in _RSPM_TABLES.items()
        },
    )
    return HealthRepository(TwoLedgerConnection(), capability)


def test_advanced_dso_currency_comes_from_the_tsn_framework() -> None:
    """The D1 defect itself: an ADSO the classic ledger never recorded still reports currency."""
    health = _two_ledger_repo().get_health("FIN_ADSO")

    assert health.last_request is not None, "ADSO currency was not established from RSPMREQUEST"
    assert health.recent_requests, "no requests returned for an ADSO with 3 active-table requests"
    assert health.request_count == 3
    # The successful load, not the newest row, is what data age must be measured from.
    assert health.last_successful_request is not None
    assert health.last_successful_request.status == "success"  # GG
    assert health.last_successful_request.records == 57_917
    # Reference date is the classic ledger's system-wide max (2026-07-28); success was 2026-08-16.
    assert health.data_age_days is not None
    assert not any("no load requests are recorded" in c for c in health.caveats), (
        "the single-ledger message survived, so the fix is not reached"
    )
    assert any("BW 7.4+ request framework" in c for c in health.caveats), (
        "the ledger actually used must be named in the response"
    )


def test_classic_providers_still_read_the_classic_ledger() -> None:
    """The fix must not reroute providers the classic ledger does record."""
    health = _two_ledger_repo().get_health("SALES_DSO")
    assert health.last_request is not None
    assert health.last_request.request_id == "REQ_B"  # an RSSTATMANPART RNR, not a TSN
    assert health.last_request.status_code == "@0A@"  # icon code, so the classic decode was used
    assert health.last_request.update_mode == "full"  # only the classic ledger carries UPDMODE
    assert not any("BW 7.4+ request framework" in c for c in health.caveats)


def test_advanced_dso_kind_is_established_when_only_the_tsn_ledger_knows_it() -> None:
    """Half of D1: a kind of ``None`` also skipped the ADSO currency branch, losing both answers.

    TLOGO is decoded from dictionary domain RSTLOGO ('ADSO' -> "DataStore Object (advanced)"), not
    from recall.
    """
    health = _two_ledger_repo().get_health("FIN_ADSO")
    assert health.object_type == "adso"
    assert not any("type could not be established" in c for c in health.caveats)


def test_an_unknown_tlogo_is_not_guessed_into_a_kind() -> None:
    health = _two_ledger_repo().get_health("ODD_TARGET")
    assert health.object_type is None
    assert health.volume_resolved is False
    assert any("type could not be established" in c for c in health.caveats)


def test_currency_is_restricted_to_the_active_table_layer() -> None:
    """A request is written once per storage layer, so an unfiltered read triple-counts.

    The excluded rows are deliberately the *newest* and largest in the fixture: if the STORAGE
    filter is dropped, the newest request becomes an activation-queue entry holding 999,999 records
    that no query can yet see, and the count inflates. Measured layer spread on the reference
    system: AQ 759,302 / AT 696,886 / CL 649,649.
    """
    health = _two_ledger_repo().get_health("FIN_ADSO")
    assert health.request_count == 3, "request count includes non-active storage layers"
    assert all(r.records != 999_999 for r in health.recent_requests), (
        "an activation-queue request leaked into the active-table currency reading"
    )
    assert any("activation-queue and changelog entries are excluded" in c for c in health.caveats)


def test_an_adso_with_no_active_table_request_is_not_reported_as_loaded() -> None:
    """Staged but never activated: falls through, and the classic ledger has nothing either."""
    health = _two_ledger_repo().get_health("UNACTIVATED_ADSO")
    assert health.last_request is None
    assert any("no load requests are recorded" in c for c in health.caveats)


def test_housekeeping_status_is_not_reported_as_a_load_failure() -> None:
    """'D' (Deleted) is not a load outcome; calling it one sends readers after a phantom failure.

    Measured: the newest active-table request for a target is 'D' far more often than 'GG', so this
    is the common path rather than an edge case.
    """
    health = _two_ledger_repo().get_health("FIN_ADSO")
    newest = health.last_request
    assert newest is not None
    assert newest.status_code == "D"
    assert newest.status == "unknown"  # honestly undecided, not forced into success or error
    assert any("housekeeping outcome rather than a load result" in c for c in health.caveats)
    assert not any("the most recent request ended 'unknown'" in c for c in health.caveats), (
        "a deleted request was described as a failed load"
    )


def test_rspm_status_codes_are_decoded_by_overall_verdict() -> None:
    """'RG' is "overall not OK but technically OK" - the overall verdict is the one that governs."""
    statuses = {
        r.status_code: r.status for r in _two_ledger_repo().get_health("FIN_ADSO").recent_requests
    }
    assert statuses["GG"] == "success"
    assert statuses["RG"] == "error"


def test_tsn_timestamps_are_parsed_from_their_first_fourteen_digits() -> None:
    parsed = _parse_tsn("20260816020000000010000")  # NUMC(23)
    assert parsed is not None
    assert parsed.isoformat() == "2026-08-16T02:00:00+00:00"
    assert _parse_tsn("2026081602") is None  # too short to carry a date
    assert _parse_tsn("2026081x02000000010000") is None  # not numeric
    assert _parse_tsn(None) is None
    # The classic parser stays strict: relaxing it would let a malformed RSSTATMANPART value
    # through, and silently accepting bad input is worse than returning None.
    assert _parse_timestamp("20260816020000000010000") is None


def test_require_health_accepts_the_tsn_ledger_alone() -> None:
    """A release recording ADSOs only in RSPMREQUEST must not report health as unsupported."""
    repo = HealthRepository(
        TwoLedgerConnection(),
        CapabilityRecord(
            system="qa",
            bw_release="7.50",
            abap_schema=SCHEMA,
            discovered_at=datetime.now(UTC),
            tables={
                logical: TableStatus(
                    logical_name=logical,
                    resolved_name="RSPMREQUEST" if logical == "adso_request" else None,
                    present=logical == "adso_request",
                    schema_name=SCHEMA,
                )
                for logical in _RSPM_TABLES
            },
        ),
    )
    assert repo.require_health() is None
