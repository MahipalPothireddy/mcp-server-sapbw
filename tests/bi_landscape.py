"""A scripted BW landscape for the BI-connector scenarios (9.7 / 9.8).

Kept separate from ``test_analyzers``'s landscape because these two scenarios need things that one
does not have: chain run history with real clock times (so a p95 completion exists to compare a
report start against) and calc-view consumers (so a dashboard's view can be classified as shared
with BW or not).

Shape:

    SALES_DSO  <- loaded by CH_SALES, which typically starts 05:00 and p95-completes 06:40
    PKG/SHARED_CV   <- consumed by BW provider SALES_CP  -> a shared view
    PKG/PRIVATE_CV  <- consumed by nothing in BW         -> a separate view

Synthetic names only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.services.analyzers import Analyzers  # re-exported for the tests

__all__ = ["Analyzers", "ScriptedConnection", "capability"]

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "chain_edges": "RSPCCHAIN",
    "log_chain": "RSPCLOGCHAIN",
    "process_log": "RSPCPROCESSLOG",
    "dtp": "RSBKDTP",
    "transformation": "RSTRAN",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "hana_views": "VIEWS",
    "composite_header": "RSOHCPR",
}

CHAIN = "CH_SALES"
PROVIDER = "SALES_DSO"
DTP = "DTP_SALES"

# Anchored to today rather than a fixed date: the schedule matrix filters run history to a recent
# window, so hard-coded dates would silently fall outside it as time passes and the test would rot.
_RUN_DAYS = 20
_LAST_DAY = date.today()
_DATES = [_LAST_DAY - timedelta(days=offset) for offset in range(_RUN_DAYS - 1, -1, -1)]

# Runs start 05:00 and take ~100 minutes, so the p95 completion lands about 06:40.
_START_HHMMSS = "050000"
_DURATION_SECONDS = 6000

# calc view -> the 0BW:BIA: provider views that read it (empty means no BW consumer).
_VIEW_CONSUMERS = {
    "PKG/SHARED_CV": ["0BW:BIA:SALES_CP"],
    "PKG/PRIVATE_CV": [],
}


def _datum(value: date) -> str:
    return value.strftime("%Y%m%d")


def _ts(day: date, seconds_offset: int = 0) -> Decimal:
    """An RSPCPROCESSLOG-style decimal timestamp YYYYMMDDHHMMSS."""
    if not seconds_offset:
        return Decimal(f"{_datum(day)}{_START_HHMMSS}")
    # Add whole seconds through a datetime so the minute/hour carry is correct.
    start = datetime.strptime(f"{_datum(day)}{_START_HHMMSS}", "%Y%m%d%H%M%S")
    return Decimal((start + timedelta(seconds=seconds_offset)).strftime("%Y%m%d%H%M%S"))


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        first = str(params[0]).strip() if params else ""

        if "OBJECT_DEPENDENCIES" in sql:
            return self._objdep(sql, params)
        if '"VIEWS"' in sql:
            return [(1,)] if "TOTAL_COUNT" in sql else [("PKG/SHARED_CV", "CALC")]
        if "RSOHCPR" in sql:
            wanted = {str(p).strip() for p in params}
            return [("SALES_CP",)] if "SALES_CP" in wanted else []
        if "RSPCCHAINATTR" in sql:
            if "TOTAL_COUNT" in sql:
                return [(1,)]
            return [(CHAIN, "ZAPP", "ACT")]
        if "RSPCCHAINT" in sql:
            return [("E", "Sales load")]
        if "RSPCCHAIN" in sql:
            if "TYPE = ?" in sql:  # parent chains: none
                return []
            wanted = {str(p).strip() for p in params}
            return [(CHAIN,)] if DTP in wanted else []
        if "RSPCPROCESSLOG" in sql:
            return self._process_log(sql)
        if "RSPCLOGCHAIN" in sql:
            return self._log_chain(sql)
        if "RSBKDTP" in sql:
            if "DISTINCT TGT" in sql:
                return []
            if "UPDMODE = ?" in sql:
                return []
            return [(DTP,)] if first == PROVIDER else []
        if "RSTRAN" in sql:
            return []
        return []

    @staticmethod
    def _objdep(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "LIKE" in sql and "BASE_OBJECT_NAME = ?" in sql:  # provider views on one calc view
            view = str(params[1]).strip()
            return [(name,) for name in _VIEW_CONSUMERS.get(view, [])]
        if "DEPENDENT_OBJECT_NAME = ?" in sql:  # calc-view base tables
            return []
        if "LIKE" in sql:
            return [("PKG/SHARED_CV",)]
        return []

    @staticmethod
    def _log_chain(sql: str) -> list[tuple[Any, ...]]:
        if "MAX(DATUM)" in sql and "CHAIN_ID" not in sql:  # reference date
            return [(_datum(_LAST_DAY),)]
        if "COUNT(DISTINCT LOG_ID)" in sql:  # cadence run-day summary (5 cols)
            return [(CHAIN, _RUN_DAYS, _RUN_DAYS, _datum(_DATES[0]), _datum(_LAST_DAY))]
        if "COUNT(*)" in sql:  # schedule-matrix run summary (4 cols)
            return [(CHAIN, _RUN_DAYS, _datum(_DATES[0]), _datum(_LAST_DAY))]
        if "ANALYZED_STATUS" in sql:  # runtime stats run list: LOG_ID, DATUM, ZEIT, STATUS
            return [
                (f"LOG{index}", _datum(day), _START_HHMMSS, "G") for index, day in enumerate(_DATES)
            ]
        if "ZEIT" in sql:  # median start time (CHAIN_ID, ZEIT)
            return [(CHAIN, _START_HHMMSS)]
        if "DATUM" in sql:  # distinct run days
            return [(CHAIN, _datum(day)) for day in _DATES]
        return []

    @staticmethod
    def _process_log(sql: str) -> list[tuple[Any, ...]]:
        if "TOTAL_COUNT" in sql:
            return [(_RUN_DAYS,)]
        # Column order as the repository selects it:
        # LOG_ID, TYPE, VARIANTE, INSTANCE, STATE, STARTTIMESTAMP, ENDTIMESTAMP
        return [
            (
                f"LOG{index}",
                "LOADING",
                DTP,
                f"REQU{index}",
                "G",
                _ts(day),
                _ts(day, _DURATION_SECONDS),
            )
            for index, day in enumerate(_DATES)
        ]


def capability(present: set[str] | None = None) -> CapabilityRecord:
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
                schema_name=("SYS" if logical in {"object_dependencies", "hana_views"} else SCHEMA)
                if logical in present
                else None,
            )
            for logical, physical in _TABLES.items()
        },
    )
