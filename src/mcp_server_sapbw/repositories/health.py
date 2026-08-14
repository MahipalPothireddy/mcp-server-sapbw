"""Provider health repository: volume from HANA monitoring, currency from BW request history.

Volume resolves a provider to the tables BW generates for it (see ``services.table_resolver``) and
reads ``SYS.M_CS_TABLES`` for each. Roles are kept apart deliberately: summing the changelog into
the active count would hide exactly the bloat you are looking for, and a provider whose tables all
exist but hold nothing is reported ``unloaded`` rather than as a legitimately empty object.

Currency comes from ``RSSTATMANPART``, BW's per-provider request ledger. ``STATUS`` holds SAP icon
codes (``@08@`` green / ``@09@`` yellow / ``@0A@`` red) decoded here from the dictionary domain
``RSSTATUS`` rather than guessed. Data age is measured against the latest request date in the
*system*, not today, so a restored copy or a frozen sandbox does not read as uniformly stale.

``M_CS_TABLES`` is live monitoring data, not metadata: it is never cached beyond the runtime tier.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from ..models.health import LoadRequest, ProviderHealth, RequestStatus, TableVolume
from ..models.provenance import UnsupportedResult
from ..services.table_resolver import PRIMARY_DATA_ROLES, candidate_tables
from .base import Repository

# RSSTATMANPART.STATUS / TSTATUS icon codes -> outcome (domain RSSTATUS, read from DD07T live).
_STATUS_MAP: dict[str, RequestStatus] = {
    "@08@": "success",  # green light (processing ended successfully)
    "@09@": "incomplete",  # yellow light (incomplete processing)
    "@0A@": "error",  # red light (incorrect processing)
}

# RSSTATMANPART.DTA_TYPE -> the provider kind whose generated table names we can derive.
#
# Only codes whose meaning is confirmed are mapped. ``FLEX_T`` / ``FLEX_M`` are deliberately absent:
# they look like advanced-DSO variants, but on the reference system none of those DTA values appear
# in RSOADSO (28 of 40 sampled were in no provider catalogue at all), so treating them as ADSOs
# would generate table names for the wrong object. They yield an unknown kind and an explicit
# caveat instead of a guess (mission Rule 2).
_DTA_TYPE_TO_KIND: dict[str, str] = {
    "ODSO": "dso",
    "CUBE": "infocube",
    "ADSO": "adso",
    "IOBJ": "infoobject",
}
_UPDMODE_LABEL: dict[str, str] = {"F": "full", "D": "delta", "I": "init", "R": "repair_full"}

_MAX_REQUESTS = 20  # most recent requests returned per provider
_TABLE_BATCH = 300
_BYTES_PER_MB = 1024 * 1024
_TS_DIGITS = 14


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an RSSTATMANPART ``YYYYMMDDHHMMSS`` timestamp into a tz-aware datetime.

    The UTC tzinfo is not a claim about the instant - it is required for the wire format.
    ``strptime`` returns a naive datetime, which serialises as ``2026-08-05T19:09:31`` with no
    offset. JSON Schema's ``format: date-time`` is RFC 3339, which *requires* an offset, so a
    strict client-side validator rejects every naive value. That rejection is total: it fails
    the whole tool response, so volume and currency became unreadable for any provider with a
    populated request ledger - which is every loaded provider.

    RSSTATMANPART stores SAP application-server wall-clock time with no offset recorded, so the
    true offset is not knowable from this table. Labelling UTC keeps the response valid and
    matches how the rest of the server emits time (see ``discovered_at``, which is genuinely
    UTC). The consequence is that these values are correct as *wall-clock readings of the BW
    server* and must not be compared against timestamps from another timezone, nor read as true
    UTC instants. ``data_age_days`` is unaffected: it subtracts two values from this same table,
    so any constant offset cancels.
    """
    text = _clean(value)
    if text is None:
        text = ""
    digits = text.split(".")[0]
    if len(digits) != _TS_DIGITS or not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class HealthRepository(Repository):
    """Volume and currency for a provider."""

    def get_health(self, provider: str, object_type: str | None = None) -> ProviderHealth:
        kind = object_type or self._infer_kind(provider)
        health = ProviderHealth(provider=provider, object_type=kind)
        caveats: list[str] = []

        if kind is None:
            health.volume_resolved = False
            caveats.append(
                "the provider's type could not be established from its request-ledger entry, so "
                "no generated table names could be derived and volume is unknown"
            )
        else:
            self._fill_volume(health, provider, kind, caveats)
        self._fill_currency(health, provider, caveats)
        health.caveats = caveats
        return health

    # --- volume ---------------------------------------------------------------------------

    def _fill_volume(
        self, health: ProviderHealth, provider: str, kind: str, caveats: list[str]
    ) -> None:
        expected = candidate_tables(provider, kind)
        if not expected:
            caveats.append(
                f"a {kind} persists no data of its own, so it has no generated tables to measure"
            )
            return
        if not self.capability.is_available("cs_tables"):
            health.volume_resolved = False
            caveats.append("SYS.M_CS_TABLES is unavailable, so volume could not be read")
            return

        volumes = self._table_volumes(list(expected))
        for table, role in sorted(expected.items()):
            measured = volumes.get(table)
            if measured is None:
                continue  # table not present: not activated, or this role does not exist here
            records, memory = measured
            health.tables.append(
                TableVolume(
                    table=table,
                    role=role,
                    record_count=records,
                    memory_mb=round(memory / _BYTES_PER_MB, 1) if memory else None,
                    provenance=self.provenance("cs_tables", {"TABLE_NAME": table}),
                )
            )
            if role in PRIMARY_DATA_ROLES:
                health.active_records += records
            elif role == "changelog":
                health.changelog_records += records
            elif role == "inbound":
                health.inbound_records += records

        health.tables_found = len(health.tables)
        total_memory = sum(t.memory_mb or 0.0 for t in health.tables)
        health.total_memory_mb = round(total_memory, 1) if total_memory else None
        if not health.tables:
            # NOT 'unloaded': we failed to locate the tables, which is not evidence the provider is
            # empty. Claiming otherwise would be a plausible-but-wrong conclusion, and the request
            # ledger frequently shows these providers loading successfully.
            health.volume_resolved = False
            caveats.append(
                f"none of the expected generated tables for a {kind} were found, so volume is "
                "unknown - this is NOT evidence the provider is empty; check the request ledger "
                "below for whether it is loading. Known limitation: a provider in its own "
                "namespace (e.g. /ABC/NAME) generates its tables in a BW generated-objects "
                "namespace (/B28/, /B299/, ...) under a generated short name, which needs a "
                "mapping-table lookup rather than the naming convention used here"
            )
        elif health.active_records == 0:
            health.unloaded = True  # tables located and genuinely empty
            caveats.append("generated tables exist but hold no active records (never loaded)")

    def _table_volumes(self, tables: list[str]) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for start in range(0, len(tables), _TABLE_BATCH):
            chunk = tables[start : start + _TABLE_BATCH]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=[
                        "TABLE_NAME",
                        "SUM(RECORD_COUNT)",
                        "SUM(MEMORY_SIZE_IN_TOTAL)",
                    ],
                    from_logical="cs_tables",
                    where=["SCHEMA_NAME = ?", f"TABLE_NAME IN ({placeholders})"],
                    params=[self.capability.abap_schema, *chunk],
                    group_by=["TABLE_NAME"],
                )
            )
            for table, records, memory in rows:
                key = _clean(table)
                if key:
                    out[key] = (_as_int(records) or 0, _as_int(memory) or 0)
        return out

    # --- currency -------------------------------------------------------------------------

    def _fill_currency(self, health: ProviderHealth, provider: str, caveats: list[str]) -> None:
        if not self.capability.is_available("request_status"):
            caveats.append(
                "RSSTATMANPART is unavailable, so load currency could not be established"
            )
            return
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[
                        "RNR",
                        "STATUS",
                        "TIMESTAMP_ANF",
                        "TIMESTAMP_VERB",
                        "ANZ_RECS",
                        "UPDMODE",
                        "OLTPSOURCE",
                        "SOURCE_DTA",
                    ],
                    from_logical="request_status",
                    where=["DTA = ?"],
                    params=[provider],
                    order_by=["TIMESTAMP_ANF DESC"],
                ),
                limit=_MAX_REQUESTS,
            )
        )
        if not rows:
            caveats.append(
                "no load requests are recorded for this provider: it may be virtual, filled by a "
                "routine, or its request history has been archived"
            )
            return

        requests = [self._request(provider, row) for row in rows]
        health.recent_requests = requests
        health.request_count = self._request_total(provider)
        health.failed_request_count = sum(1 for r in requests if r.status == "error")
        health.last_request = requests[0]
        health.last_successful_request = next((r for r in requests if r.status == "success"), None)

        reference = self._reference_date()
        last_success = health.last_successful_request
        if reference and last_success and last_success.started_at:
            health.data_age_days = (reference - last_success.started_at.date()).days
        if health.last_request and health.last_request.status != "success":
            caveats.append(
                f"the most recent load ended '{health.last_request.status}'; the provider's "
                "current contents may be partial"
            )
        if health.last_successful_request is None:
            caveats.append(
                "no successful load appears in the recent request window, so data age could not be "
                "established"
            )

    def _request(self, provider: str, row: tuple[Any, ...]) -> LoadRequest:
        rnr, status, started, ended, records, update_mode, oltp, source_dta = row
        code = _clean(status)
        return LoadRequest(
            request_id=_clean(rnr) or "",
            status=_STATUS_MAP.get(code or "", "unknown"),
            status_code=code,
            started_at=_parse_timestamp(started),
            ended_at=_parse_timestamp(ended),
            records=_as_int(records),
            update_mode=_UPDMODE_LABEL.get((_clean(update_mode) or "").upper()),
            source=_clean(oltp) or _clean(source_dta),
            provenance=self.provenance(
                "request_status", {"DTA": provider, "RNR": _clean(rnr) or ""}
            ),
        )

    def _request_total(self, provider: str) -> int:
        base = self.dialect.build_select(
            columns=["RNR"],
            from_logical="request_status",
            where=["DTA = ?"],
            params=[provider],
        )
        rows = self.select(self.dialect.count_query(base))
        return _as_int(rows[0][0]) or 0 if rows else 0

    def _reference_date(self) -> date | None:
        """Latest request date in the system - the honest 'now' for a copied or frozen system."""
        rows = self.select(
            self.dialect.build_select(columns=["MAX(TIMESTAMP_ANF)"], from_logical="request_status")
        )
        if not rows or rows[0][0] is None:
            return None
        parsed = _parse_timestamp(rows[0][0])
        return parsed.date() if parsed else None

    # --- helpers --------------------------------------------------------------------------

    def _infer_kind(self, provider: str) -> str | None:
        """Infer the provider kind from its request-ledger entry (cheap, one row)."""
        if not self.capability.is_available("request_status"):
            return None
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DTA_TYPE"],
                    from_logical="request_status",
                    where=["DTA = ?"],
                    params=[provider],
                ),
                limit=1,
            )
        )
        if not rows:
            return None
        return _DTA_TYPE_TO_KIND.get((_clean(rows[0][0]) or "").upper())

    def require_health(self) -> UnsupportedResult | None:
        """Health needs at least one of volume or currency to be available."""
        if self.capability.is_available("cs_tables") or self.capability.is_available(
            "request_status"
        ):
            return None
        return self.require("request_status", "cs_tables")
