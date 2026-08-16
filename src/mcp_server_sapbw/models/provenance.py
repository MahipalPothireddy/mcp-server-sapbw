"""Cross-cutting result models: Provenance and UnsupportedResult.

Every fact returned by the server carries provenance so it is traceable to the metadata
row it came from (mission Non-Negotiable Rule 3). When a capability is unavailable on the
connected release, repositories return an ``UnsupportedResult`` rather than guessing
(Rule 2 / Rule 7).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCategory, ErrorCode, derive_failure_fields


class Provenance(BaseModel):
    """Traceability for a single fact: the source table and its key.

    Example: ``{"source_table": "RSTRAN", "source_key": {"TRANID": "0ABC123", "OBJVERS": "A"}}``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_table: str = Field(min_length=1)
    source_key: dict[str, str] = Field(default_factory=dict)


class UnsupportedResult(BaseModel):
    """Structured "this capability does not exist on the connected release" result.

    Returned instead of raising or guessing when the capability resolver reports that a
    required table/column is absent. ``missing`` names the object(s) that were looked for;
    ``alternative`` names the correct object for this release when one is known.

    ``code``/``category``/``remedy`` are the canonical failure fields, identical in meaning to those
    on :class:`~.errors.BwError`, so a caller branches on one field whichever surface failed. The
    ``status`` literal stays for compatibility.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["unsupported_on_release"] = "unsupported_on_release"
    code: ErrorCode = "unsupported_on_release"
    category: ErrorCategory | None = None
    remedy: str | None = None
    retryable: bool | None = None
    missing: list[str] = Field(default_factory=list)
    release: str
    alternative: str | None = None
    detail: str

    @model_validator(mode="after")
    def _derive(self) -> UnsupportedResult:
        self.category, self.retryable, self.remedy = derive_failure_fields(
            self.code, self.category, self.retryable, self.remedy
        )
        return self
