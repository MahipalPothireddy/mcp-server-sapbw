"""Cross-cutting result models: Provenance and UnsupportedResult.

Every fact returned by the server carries provenance so it is traceable to the metadata
row it came from (mission Non-Negotiable Rule 3). When a capability is unavailable on the
connected release, repositories return an ``UnsupportedResult`` rather than guessing
(Rule 2 / Rule 7).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["unsupported_on_release"] = "unsupported_on_release"
    missing: list[str] = Field(default_factory=list)
    release: str
    alternative: str | None = None
    detail: str
