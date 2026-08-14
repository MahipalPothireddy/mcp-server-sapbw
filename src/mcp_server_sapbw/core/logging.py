"""Structured logging — diagnosable without leaking anything.

Supporting an organisation you cannot log into means you need to know *which* statement was slow,
what the cache did, and whether the transport dropped. Two constraints shape this module:

1. **stderr only.** The stdio transport uses stdout for the MCP protocol itself. A stray print or a
   stdout log handler corrupts the framing and the client disconnects, so the handler is pinned to
   stderr.
2. **No secrets and no customer object names at default level.** Host, user and password never
   reach a log record (the connection layer scrubs them from exceptions). Bound query *parameters*
   carry concrete object names, so they are never logged at all — not even at DEBUG. What is logged
   is shape and cost: the table, elapsed milliseconds, row count. That is enough to find a slow
   query without recording the customer's data model in an operator's log file.

Level comes from ``SAPBW_LOG_LEVEL``, falling back to ``FASTMCP_LOG_LEVEL`` (which MCP clients
already set) and defaulting to WARNING, so a default install is quiet.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_ROOT_NAME = "mcp_server_sapbw"
_DEFAULT_LEVEL = "WARNING"
# Module-level marker so configure() is idempotent: a second call must not add a second handler
# (which would duplicate every line). Held in a mutable container to keep the function global-free.
_STATE: dict[str, bool] = {"configured": False}


def resolve_level(env: dict[str, str] | None = None) -> int:
    """The configured log level, from SAPBW_LOG_LEVEL / FASTMCP_LOG_LEVEL."""
    source = env if env is not None else dict(os.environ)
    raw = source.get("SAPBW_LOG_LEVEL") or source.get("FASTMCP_LOG_LEVEL") or _DEFAULT_LEVEL
    resolved = logging.getLevelName(raw.strip().upper())
    return resolved if isinstance(resolved, int) else logging.WARNING


def configure(env: dict[str, str] | None = None) -> logging.Logger:
    """Attach a stderr handler to the package logger. Idempotent; safe to call from ``main()``."""
    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(resolve_level(env))
    if not _STATE["configured"]:
        handler = logging.StreamHandler(stream=sys.stderr)  # never stdout: that is the MCP channel
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.propagate = False  # the root logger may be writing to stdout
        _STATE["configured"] = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """A child logger. Quiet by default: no handler until :func:`configure` runs."""
    logger = logging.getLogger(name if name.startswith(_ROOT_NAME) else f"{_ROOT_NAME}.{name}")
    if not logging.getLogger(_ROOT_NAME).handlers:
        logging.getLogger(_ROOT_NAME).addHandler(logging.NullHandler())
    return logger


def log_query(
    logger: logging.Logger,
    *,
    table: str | None,
    elapsed_ms: float,
    rows: int,
    slow_ms: float,
) -> None:
    """Record one statement's cost. Parameters are deliberately absent — they carry object names."""
    if elapsed_ms >= slow_ms:
        logger.warning("slow query table=%s elapsed_ms=%.0f rows=%d", table, elapsed_ms, rows)
    elif logger.isEnabledFor(logging.DEBUG):
        logger.debug("query table=%s elapsed_ms=%.0f rows=%d", table, elapsed_ms, rows)


def table_of(sql: str) -> str | None:
    """The first quoted identifier after FROM — the table, without logging the whole statement.

    Statements are built by the dialect and are schema-qualified with double quotes, so this is a
    cheap way to label a log line with *what* was read without echoing predicates.
    """
    marker = " FROM "
    index = sql.upper().find(marker)
    if index == -1:
        return None
    tail = sql[index + len(marker) :].lstrip()
    if not tail.startswith('"'):
        return tail.split()[0] if tail.split() else None
    parts: list[str] = []
    rest = tail
    while rest.startswith('"'):
        end = rest.find('"', 1)
        if end == -1:
            break
        parts.append(rest[1:end])
        rest = rest[end + 1 :]
        if rest.startswith("."):
            rest = rest[1:]
            continue
        break
    return ".".join(parts) if parts else None


def summarise(**fields: Any) -> str:
    """``key=value`` pairs for a single log line, skipping empties."""
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
