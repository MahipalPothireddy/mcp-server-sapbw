"""Tests for the logging layer.

Two properties are load-bearing rather than cosmetic:

1. **stdout stays clean.** The stdio transport frames the MCP protocol on stdout; a log handler
   there corrupts the stream and the client drops the connection.
2. **Nothing sensitive is logged.** Bound parameters carry concrete customer object names, so they
   must never appear in a log record at any level. Credentials must never appear either.
"""

from __future__ import annotations

import logging

import pytest

from mcp_server_sapbw.core import logging as sapbw_logging
from mcp_server_sapbw.core.connection import ReadOnlyConnection, SecretScrubber


class FakeCursor:
    def execute(self, operation: str, parameters: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return [("row",)]

    def close(self) -> None:
        return None


class FakeConnection:
    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def close(self) -> None:
        return None


# --- level resolution ----------------------------------------------------------------------


def test_level_prefers_the_server_specific_variable() -> None:
    level = sapbw_logging.resolve_level({"SAPBW_LOG_LEVEL": "DEBUG", "FASTMCP_LOG_LEVEL": "ERROR"})
    assert level == logging.DEBUG


def test_level_falls_back_to_the_mcp_client_variable() -> None:
    assert sapbw_logging.resolve_level({"FASTMCP_LOG_LEVEL": "ERROR"}) == logging.ERROR


def test_level_defaults_to_quiet() -> None:
    assert sapbw_logging.resolve_level({}) == logging.WARNING


def test_unparseable_level_falls_back_rather_than_crashing() -> None:
    assert sapbw_logging.resolve_level({"SAPBW_LOG_LEVEL": "chatty"}) == logging.WARNING


# --- handler placement ---------------------------------------------------------------------


def test_handler_writes_to_stderr_never_stdout() -> None:
    logger = sapbw_logging.configure({"SAPBW_LOG_LEVEL": "DEBUG"})
    streams = [
        getattr(h, "stream", None) for h in logger.handlers if isinstance(h, logging.StreamHandler)
    ]
    assert streams, "no stream handler attached"
    assert all(getattr(s, "name", "") != "<stdout>" for s in streams)
    assert logger.propagate is False  # the root logger may itself be writing to stdout


def test_configure_is_idempotent() -> None:
    first = sapbw_logging.configure({"SAPBW_LOG_LEVEL": "INFO"})
    before = len(first.handlers)
    second = sapbw_logging.configure({"SAPBW_LOG_LEVEL": "INFO"})
    assert len(second.handlers) == before, "a second configure duplicated every log line"


# --- what a query log line may contain -----------------------------------------------------


def test_table_is_extracted_without_the_statement() -> None:
    sql = 'SELECT TRANID FROM "SAPABAP1"."RSTRAN" WHERE TRANID = ? AND OBJVERS = \'A\''
    assert sapbw_logging.table_of(sql) == "SAPABAP1.RSTRAN"


def test_table_of_handles_unquoted_and_missing_from() -> None:
    assert sapbw_logging.table_of("SELECT A FROM RSPCCHAIN WHERE X = ?") == "RSPCCHAIN"
    assert sapbw_logging.table_of("SELECT 1") is None


def test_query_log_records_cost_but_never_parameters(caplog: pytest.LogCaptureFixture) -> None:
    """The regression this guards: logging the SQL with its bound values leaks object names."""
    connection = ReadOnlyConnection(FakeConnection(), scrubber=SecretScrubber([]))
    sensitive_object_name = "SOME_CONCRETE_OBJECT"
    with caplog.at_level(logging.DEBUG, logger="mcp_server_sapbw.connection"):
        connection.execute_select(
            'SELECT A FROM "SCHEMA"."RSTRAN" WHERE TRANID = ?', [sensitive_object_name]
        )
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SCHEMA.RSTRAN" in text  # the table is useful and safe
    assert "elapsed_ms" in text and "rows=1" in text  # cost is what you need for support
    assert sensitive_object_name not in text, "a bound parameter reached the log"
    assert "WHERE" not in text, "the statement body reached the log"


def test_slow_query_is_warned_even_when_debug_is_off(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("mcp_server_sapbw.test_slow")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        sapbw_logging.log_query(logger, table="S.T", elapsed_ms=9000.0, rows=3, slow_ms=5000.0)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "slow query" in caplog.text


def test_fast_query_is_not_warned(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("mcp_server_sapbw.test_fast")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        sapbw_logging.log_query(logger, table="S.T", elapsed_ms=10.0, rows=3, slow_ms=5000.0)
    assert not caplog.records


def test_credentials_never_reach_a_query_log(caplog: pytest.LogCaptureFixture) -> None:
    scrubber = SecretScrubber(["secret-host", "SAPUSER", "hunter2"])
    connection = ReadOnlyConnection(FakeConnection(), scrubber=scrubber)
    with caplog.at_level(logging.DEBUG, logger="mcp_server_sapbw.connection"):
        connection.execute_select('SELECT A FROM "S"."T"')
    for secret in ("secret-host", "SAPUSER", "hunter2"):
        assert secret not in caplog.text


def test_summarise_skips_empty_fields() -> None:
    assert sapbw_logging.summarise(a=1, b=None, c="x") == "a=1 c=x"
