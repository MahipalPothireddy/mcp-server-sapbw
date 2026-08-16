"""One shape for every way a call can fail to give the answer asked for.

**The problem this solves.** Failure arrived in two incompatible forms. Four structured results -
``UnsupportedResult``, ``ObjectNotFound``, ``BudgetResult``, ``ConnectorUnavailable`` - each had a
``status`` string and its own field names, with no shared taxonomy and nothing saying what to do
next. Everything else escaped as one of eleven exception classes, reaching the caller as an opaque
error string with no code to branch on: a locked-down user hitting the read-only guard, a
mistyped profile name, and a dropped HANA session were indistinguishable to a program.

:class:`BwError` is the single envelope. ``code`` is a stable machine string, ``category`` groups
the codes so a caller can branch coarsely without enumerating them, ``retryable`` answers the only
question a retry loop has, and ``remedy`` says what to do. The latter three are *derived* from
``code`` by one table, so a new code cannot arrive without a category and an instruction.

The four existing result models keep their own ``status`` literals - they are published schemas -
and gain the canonical ``code``/``category``/``remedy`` alongside, so a caller can branch on one
field regardless of which surface produced the failure.

**Secrets never reach here.** :func:`from_exception` uses an exception's own message only for the
families whose messages are scrubbed at the raise site (the connection layer runs every message
through its secret scrubber). For anything else it reports the exception *type* and a fixed message,
because an unrecognised exception is exactly the case where a host name or a DSN might be embedded.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Coarse grouping, so a caller can branch without enumerating every code.
ErrorCategory = Literal[
    "not_found",  # the thing asked for does not exist
    "unsupported",  # this release or system cannot answer
    "invalid_request",  # the request itself was wrong
    "partial",  # a bound was hit; what came back is incomplete but usable
    "configuration",  # something has to be configured before this can work
    "transport",  # the connection or the query failed
    "guardrail",  # a safety rule refused the operation
    "internal",  # a defect here
]

#: Stable machine codes. Additive: a code is never renamed, because callers branch on it.
ErrorCode = Literal[
    "object_not_found",
    "profile_not_found",
    "unsupported_on_release",
    "capability_undetermined",
    "invalid_argument",
    "budget_exceeded",
    "result_truncated",
    "connector_not_configured",
    "profile_misconfigured",
    "connection_failed",
    "query_failed",
    "permission_denied",
    "read_only_violation",
    "output_failed",
    "internal_error",
]

# code -> (category, retryable, remedy). The single place a code's meaning is decided; a code with
# no entry cannot exist, because the validator refuses to fill the derived fields without one.
_CODE_SPEC: dict[str, tuple[ErrorCategory, bool, str]] = {
    "object_not_found": (
        "not_found",
        False,
        "Check the technical name, or use bw_search_objects - BW names are upper-case and a "
        "DataSource endpoint is stored space-padded with its logical system.",
    ),
    "profile_not_found": (
        "not_found",
        False,
        "Use bw_list_systems to see the configured profile names; add the profile to profiles.yaml "
        "if it is missing.",
    ),
    "unsupported_on_release": (
        "unsupported",
        False,
        "This release does not carry the metadata object the answer needs. bw_capability_report "
        "shows which questions resolve on this system and which do not.",
    ),
    "capability_undetermined": (
        "unsupported",
        False,
        "Capability discovery has not established whether this object exists here. Run "
        "bw_refresh_capabilities.",
    ),
    "invalid_argument": (
        "invalid_request",
        False,
        "Correct the argument and call again; the message names the value that was rejected.",
    ),
    "budget_exceeded": (
        "partial",
        True,
        "The per-call query or time budget was spent. Narrow the request (a smaller depth, a "
        "tighter filter, a smaller page) or raise SAPBW_MAX_QUERIES_PER_CALL / "
        "SAPBW_MAX_SECONDS_PER_CALL.",
    ),
    "result_truncated": (
        "partial",
        True,
        "A row or node cap stopped the walk. Page through with limit/offset, or reduce depth.",
    ),
    "connector_not_configured": (
        "configuration",
        False,
        "The metadata lives in a system this server has not been pointed at. Add the connector "
        "profile (see profiles.example.yaml) and call again.",
    ),
    "profile_misconfigured": (
        "configuration",
        False,
        "The profile is present but not usable as written. The message names the field; fix "
        "profiles.yaml or the environment variable it interpolates.",
    ),
    "connection_failed": (
        "transport",
        True,
        "The database connection could not be established or was lost. Verify the host is "
        "reachable and retry; a dropped session usually succeeds on the next attempt.",
    ),
    "query_failed": (
        "transport",
        True,
        "The statement failed at the driver. Retry once; if it persists the object may have been "
        "changed or the user may lack SELECT on it.",
    ),
    "permission_denied": (
        "configuration",
        False,
        "The connected user may not read the object this answer needs. This is a grant, not a "
        "release limitation: bw_access_report names the exact SELECT privileges to request and "
        "what stays unanswerable until they are granted. Retrying will not help.",
    ),
    "read_only_violation": (
        "guardrail",
        False,
        "This server issues SELECT only, and refuses a connection whose user holds write grants. "
        "Point it at a read-only user. This is not configurable.",
    ),
    "output_failed": (
        "configuration",
        False,
        "The output directory could not be written, or it is inside the tracked repository (which "
        "is refused, because generated content is customer metadata). Choose a git-ignored path.",
    ),
    "internal_error": (
        "internal",
        False,
        "A defect in this server. The message names the failure class; please report it with the "
        "tool and arguments used.",
    ),
}

#: Exception class name -> code. Keyed by name rather than by type so this module imports nothing
#: from core/ or connectors/ and stays a leaf.
_EXCEPTION_CODES: dict[str, ErrorCode] = {
    "ProfileNotFoundError": "profile_not_found",
    "ProfileConfigError": "profile_misconfigured",
    "ReadOnlyViolation": "read_only_violation",
    "ConnectionFailure": "connection_failed",
    "QueryError": "query_failed",
    "AdtError": "connection_failed",
    "DialectError": "unsupported_on_release",
    "CapabilityError": "capability_undetermined",
    "DocGenError": "output_failed",
    "ValueError": "invalid_argument",
}

#: Exceptions whose messages are scrubbed of secrets at the raise site, so the text is safe to
#: forward. Everything else is reported by type only - an unrecognised exception is exactly where a
#: host name or a DSN could be embedded.
_SAFE_MESSAGE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "ProfileNotFoundError",
        "ReadOnlyViolation",
        "ConnectionFailure",
        "QueryError",
        "DialectError",
        "CapabilityError",
        "DocGenError",
        "ValueError",
    }
)


class BwError(BaseModel):
    """A failure, in the one shape every surface uses.

    ``category``, ``retryable`` and ``remedy`` are derived from ``code`` and must not be passed in;
    that is what makes them impossible to omit and impossible to disagree with the code.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["error"] = "error"
    code: ErrorCode
    category: ErrorCategory | None = None
    message: str
    retryable: bool | None = None
    remedy: str | None = None
    #: Structured context - the object, system, table or argument the failure is about. Strings
    #: only, so nothing unserialisable or unexpectedly large ends up in a tool response.
    detail: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive(self) -> BwError:
        category, retryable, remedy = _CODE_SPEC[self.code]
        if self.category is None:
            self.category = category
        if self.retryable is None:
            self.retryable = retryable
        if self.remedy is None:
            self.remedy = remedy
        return self


def derive_failure_fields(
    code: str,
    category: ErrorCategory | None,
    retryable: bool | None,
    remedy: str | None,
) -> tuple[ErrorCategory, bool, str]:
    """Fill the derived failure fields from a code, for the models that carry their own ``status``.

    Shared with :class:`BwError` so ``UnsupportedResult``, ``ObjectNotFound``, ``BudgetResult`` and
    ``ConnectorUnavailable`` cannot disagree with it about what a code means.
    """
    spec_category, spec_retryable, spec_remedy = _CODE_SPEC[code]
    return (
        spec_category if category is None else category,
        spec_retryable if retryable is None else retryable,
        spec_remedy if remedy is None else remedy,
    )


def error(code: ErrorCode, message: str, **detail: str) -> BwError:
    """Build a :class:`BwError`, with any keyword arguments becoming structured detail."""
    return BwError(code=code, message=message, detail={k: str(v) for k, v in detail.items()})


def code_for_exception(exc: BaseException) -> ErrorCode:
    """The canonical code for an exception, walking its MRO so a subclass still maps.

    A refused read is separated from a broken one before the class mapping is consulted: both
    arrive as ``QueryError``, but only one of them is retryable, and telling a caller to retry a
    missing grant would loop forever against an answer that cannot change.
    """
    if getattr(exc, "permission_denied", False) is True:
        return "permission_denied"
    for klass in type(exc).__mro__:
        mapped = _EXCEPTION_CODES.get(klass.__name__)
        if mapped is not None:
            return mapped
    return "internal_error"


def from_exception(exc: BaseException, **detail: str) -> BwError:
    """Turn any escaping exception into a structured failure a program can branch on.

    The message is forwarded only for exception families whose text is scrubbed at the raise site.
    For anything else the failure class is named and the message is fixed, so an unrecognised
    exception can never carry a host name or a connection string into a tool response.
    """
    name = type(exc).__name__
    code = code_for_exception(exc)
    safe = any(k.__name__ in _SAFE_MESSAGE_EXCEPTIONS for k in type(exc).__mro__)
    message = str(exc) if safe and str(exc) else f"{name} raised while answering this call"
    return BwError(
        code=code,
        message=message,
        detail={"exception": name, **{k: str(v) for k, v in detail.items()}},
    )
