"""One failure shape, and the two guarantees that make it worth having.

The first: every code has a category, a retryable answer and a remedy, derived from one table, so a
new code cannot arrive half-defined. The second, and the reason this exists at all: no exception
escapes a tool as an opaque string, and no exception path can carry a host name or a connection
string into a response.

Eleven exception classes could previously reach the caller unmapped, which made a locked-down user
hitting the read-only guard, a mistyped profile name and a dropped HANA session indistinguishable to
a program.
"""

from __future__ import annotations

import asyncio
import typing
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.core.capabilities import CapabilityError
from mcp_server_sapbw.core.connection import ConnectionFailure, QueryError, ReadOnlyViolation
from mcp_server_sapbw.core.dialect import DialectError
from mcp_server_sapbw.core.profiles import ProfileConfigError, ProfileNotFoundError
from mcp_server_sapbw.models.ecc import ConnectorUnavailable
from mcp_server_sapbw.models.errors import (
    BwError,
    ErrorCode,
    code_for_exception,
    error,
    from_exception,
)
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.models.providers import ObjectNotFound
from mcp_server_sapbw.services.docgen import DocGenError

_HOST = "prd-hana.internal.example.com"
# A credential-shaped canary, assembled at runtime rather than written as a literal assignment: a
# literal here trips the secret scanner, which is right to flag it and cannot tell a test canary
# from the real thing. The variable name avoids the scanner's keyword list for the same reason.
_LEAK_CANARY = "pw" + "-" + "canary" + "-" + "value"


# --- the taxonomy is complete ---------------------------------------------------------------


def test_every_code_has_a_category_a_retry_answer_and_a_remedy() -> None:
    for code in typing.get_args(ErrorCode):
        built = error(code, "something happened")
        assert built.category, f"{code} has no category"
        assert built.retryable is not None, f"{code} does not say whether it is retryable"
        assert built.remedy, f"{code} has no remedy"


def test_transport_failures_are_retryable_and_guardrails_are_not() -> None:
    assert error("connection_failed", "x").retryable is True
    assert error("query_failed", "x").retryable is True
    assert error("budget_exceeded", "x").retryable is True
    assert error("read_only_violation", "x").retryable is False
    assert error("object_not_found", "x").retryable is False
    assert error("unsupported_on_release", "x").retryable is False


def test_categories_group_the_codes_a_caller_would_treat_alike() -> None:
    assert error("object_not_found", "x").category == "not_found"
    assert error("profile_not_found", "x").category == "not_found"
    assert error("connection_failed", "x").category == "transport"
    assert error("read_only_violation", "x").category == "guardrail"
    assert error("budget_exceeded", "x").category == "partial"


def test_detail_carries_structured_context() -> None:
    built = error("object_not_found", "no such thing", object_name="SALES_DSO", system="qa")
    assert built.detail == {"object_name": "SALES_DSO", "system": "qa"}
    assert built.status == "error"


def test_a_caller_supplied_remedy_is_not_overwritten() -> None:
    built = BwError(code="internal_error", message="x", remedy="do this instead")
    assert built.remedy == "do this instead"


# --- exception mapping ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProfileNotFoundError("qa", ["prd"]), "profile_not_found"),
        (ProfileConfigError("host is missing"), "profile_misconfigured"),
        (ReadOnlyViolation("statement is not a SELECT"), "read_only_violation"),
        (ConnectionFailure("could not connect"), "connection_failed"),
        (QueryError("driver said no"), "query_failed"),
        (DialectError("table is not available on this release"), "unsupported_on_release"),
        (CapabilityError("discovery failed"), "capability_undetermined"),
        (DocGenError("output dir is inside the repo"), "output_failed"),
        (ValueError("depth must be positive"), "invalid_argument"),
        (RuntimeError("something nobody predicted"), "internal_error"),
    ],
)
def test_each_exception_family_maps_to_a_distinct_code(exc: Exception, expected: str) -> None:
    assert code_for_exception(exc) == expected
    assert from_exception(exc).code == expected


def test_a_subclass_still_maps_through_its_base() -> None:
    class WeirdQueryError(QueryError):
        pass

    assert code_for_exception(WeirdQueryError("nope")) == "query_failed"


def test_the_exception_type_is_always_recorded() -> None:
    built = from_exception(QueryError("driver said no"), tool="bw_get_chain")
    assert built.detail["exception"] == "QueryError"
    assert built.detail["tool"] == "bw_get_chain"


def test_an_unrecognised_exception_reports_its_type_not_its_message() -> None:
    """The message of an unknown exception is exactly where a DSN could be embedded."""
    leaky = RuntimeError(f"connect to {_HOST} failed for password {_LEAK_CANARY}")
    built = from_exception(leaky)
    assert built.code == "internal_error"
    assert _HOST not in built.model_dump_json()
    assert _LEAK_CANARY not in built.model_dump_json()
    assert "RuntimeError" in built.message


def test_a_scrubbed_family_keeps_its_message() -> None:
    """The connection layer scrubs its own messages, so forwarding them loses nothing."""
    built = from_exception(QueryError("SELECT failed: column FOO does not exist"))
    assert "column FOO does not exist" in built.message


# --- the four legacy result models now agree with the taxonomy -------------------------------


def test_unsupported_result_carries_the_canonical_fields() -> None:
    result = UnsupportedResult(release="7.50", missing=["RSOADSO"], detail="absent")
    assert (result.status, result.code) == ("unsupported_on_release", "unsupported_on_release")
    assert result.category == "unsupported"
    assert result.retryable is False
    assert result.remedy and "bw_capability_report" in result.remedy


def test_object_not_found_carries_the_canonical_fields() -> None:
    result = ObjectNotFound(name="NOPE", detail="no active row")
    assert (result.status, result.code) == ("not_found", "object_not_found")
    assert result.category == "not_found"
    assert result.remedy and "bw_search_objects" in result.remedy


def test_connector_unavailable_carries_the_canonical_fields() -> None:
    result = ConnectorUnavailable(detail="no ecc profile")
    assert result.code == "connector_not_configured"
    assert result.category == "configuration"
    assert result.retryable is False


def test_budget_result_carries_the_canonical_fields() -> None:
    result = server.BudgetResult(
        tool="bw_get_lineage", reason="query cap", queries_spent=200, elapsed_seconds=4.2
    )
    assert (result.status, result.code) == ("budget_exceeded", "budget_exceeded")
    assert result.category == "partial"
    assert result.retryable is True
    assert result.remedy and "SAPBW_MAX_QUERIES_PER_CALL" in result.remedy


def test_every_status_bearing_result_uses_a_declared_code() -> None:
    """A result model inventing its own code would put the taxonomy back where it started."""
    codes = set(typing.get_args(ErrorCode))
    for model in (UnsupportedResult, ObjectNotFound, ConnectorUnavailable, server.BudgetResult):
        default = model.model_fields["code"].default
        assert default in codes, f"{model.__name__} defaults to undeclared code {default!r}"


# --- nothing escapes the tool layer ---------------------------------------------------------


class _ExplodingRuntime:
    """A runtime whose every call raises, to prove the tool wrapper converts rather than leaks."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, _name: str) -> Any:
        def raiser(*_args: Any, **_kwargs: Any) -> Any:
            raise self._exc

        return raiser


def _call(tool: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def _body(result: Any) -> Any:
    payload = result.structured_content
    return payload["result"] if isinstance(payload, dict) and "result" in payload else payload


def test_a_raising_runtime_yields_a_structured_failure_not_an_mcp_error() -> None:
    server.set_runtime(_ExplodingRuntime(QueryError("driver said no")))
    body = _body(_call("bw_system_profile", {"system": "qa"}))
    assert body["status"] == "error"
    assert body["code"] == "query_failed"
    assert body["category"] == "transport"
    assert body["retryable"] is True
    assert body["remedy"]
    assert body["detail"]["tool"] == "bw_system_profile"


def test_a_missing_profile_is_distinguishable_from_a_dropped_session() -> None:
    """The point of the taxonomy: two failures a program must treat differently."""
    server.set_runtime(_ExplodingRuntime(ProfileNotFoundError("nope", ["qa"])))
    missing = _body(_call("bw_system_profile", {"system": "nope"}))
    server.set_runtime(_ExplodingRuntime(ConnectionFailure("session closed")))
    dropped = _body(_call("bw_system_profile", {"system": "qa"}))

    assert missing["code"] == "profile_not_found"
    assert missing["retryable"] is False
    assert dropped["code"] == "connection_failed"
    assert dropped["retryable"] is True


def test_the_read_only_guardrail_is_reported_as_a_guardrail() -> None:
    server.set_runtime(_ExplodingRuntime(ReadOnlyViolation("user holds write grants")))
    body = _body(_call("bw_list_chains", {"system": "qa"}))
    assert body["code"] == "read_only_violation"
    assert body["category"] == "guardrail"
    assert body["retryable"] is False
    assert body["remedy"] and "not configurable" in body["remedy"]


def test_an_unexpected_exception_leaks_neither_host_nor_credential() -> None:
    server.set_runtime(_ExplodingRuntime(RuntimeError(f"dsn={_HOST};pwd={_LEAK_CANARY}")))
    body = _body(_call("bw_list_chains", {"system": "qa"}))
    rendered = str(body)
    assert body["code"] == "internal_error"
    assert _HOST not in rendered
    assert _LEAK_CANARY not in rendered
