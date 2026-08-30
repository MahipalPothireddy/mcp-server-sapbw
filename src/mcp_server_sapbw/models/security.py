"""Domain models for BW security: analysis authorisations and their assignment.

**This subsystem reads a different class of data from the rest of the server, and says so.**

Everywhere else, the server reads structural metadata: object definitions, mappings,
schedules. An analysis authorisation is not structure - ``RSECVAL`` holds *data values*.
"Cost centres 1000-1999, company code DE01" states what a named person may see, which is
more sensitive than the ABAP source the cache already holds, and becomes personal data
once joined to a user id.

Three consequences are built into the design rather than left to operator discipline:

1. **Authorisation values are never cached.** The repository does not route these reads
   through the SQLite cache at any tier. Structural metadata ages slowly and benefits from
   caching; a permission set changes when someone joins, moves or leaves, and a stale
   answer to "who can see this" is worse than a slow one.
2. **Value ranges are opt-in.** Listing and coverage tools return the *shape* of an
   authorisation (which characteristics it restricts, how many ranges, whether it grants
   everything). Concrete values come only from the single-object tool, so a broad question
   cannot incidentally dump a landscape's permission data into a transcript.
3. **``0BI_ALL`` is called out, not counted.** A catch-all holder is unrestricted, and
   averaging them in with genuinely scoped users makes a landscape look better governed
   than it is.

Special values BW uses inside ranges, decoded rather than passed through raw:

* ``:`` - aggregation authorisation. Permits *aggregated* access only; the user may see a
  total but not the individual rows behind it. Frequently misread as "no access".
* ``#`` - the unassigned/blank member. Records with no value for the characteristic.
* ``*`` - everything for that characteristic.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .completeness import BoundedResult
from .provenance import Provenance

#: How a range's bounds are interpreted (RSECVAL SIGN / OPTION, decoded).
RangeSign = Literal["include", "exclude", "unknown"]
RangeOperator = Literal["equal", "between", "greater_equal", "less_equal", "pattern", "unknown"]

#: What a value literally means, where BW gives it a special reading.
SpecialValue = Literal["all", "aggregation_only", "unassigned", "literal"]

#: How an authorisation came to exist. A generated authorisation is maintained by a program or DAP,
#: so changing it by hand is overwritten on the next run — worth knowing before advising an edit.
AuthOrigin = Literal["maintained", "generated", "unknown"]


class AuthValueRange(BaseModel):
    """One permitted (or excluded) range for one characteristic within an authorisation."""

    model_config = ConfigDict(extra="forbid")

    characteristic: str
    sign: RangeSign = "include"
    operator: RangeOperator = "equal"
    low: str | None = None
    high: str | None = None
    special: SpecialValue = "literal"
    #: True when the bound is a variable reference resolved per user at runtime, not a fixed value.
    is_variable: bool = False
    provenance: Provenance


class AuthHierarchyNode(BaseModel):
    """A hierarchy-node authorisation: access to a subtree rather than a flat value range."""

    model_config = ConfigDict(extra="forbid")

    characteristic: str
    hierarchy: str | None = None
    node: str | None = None
    node_type: str | None = None
    #: How far below the node access extends, as BW records it (level/depth semantics vary).
    level: str | None = None
    validity_from: date | None = None
    validity_to: date | None = None
    provenance: Provenance


class AnalysisAuthSummary(BaseModel):
    """The *shape* of one analysis authorisation, without its concrete values.

    Returned by listing and coverage tools. ``grants_everything`` flags a catch-all
    (``0BI_ALL``, or an authorisation whose every characteristic is ``*``): the single most
    important fact about an authorisation, and the easiest to lose in a value dump.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    origin: AuthOrigin = "unknown"
    characteristics: list[str] = Field(default_factory=list)
    range_count: int = 0
    hierarchy_node_count: int = 0
    grants_everything: bool = False
    #: Characteristics restricted by a variable rather than a fixed value: the effective scope is
    #: per-user and cannot be read from metadata.
    variable_driven_characteristics: list[str] = Field(default_factory=list)
    user_count: int | None = None  # None when the assignment table could not be read
    provenance: Provenance | list[Provenance]


class AnalysisAuth(BoundedResult):
    """One analysis authorisation in full, including its value ranges.

    Only ``bw_get_analysis_auth`` returns this. ``contains_data_values`` is always true and
    stated explicitly, so a caller (or an audit of a transcript) can see that this payload
    carried permission data rather than structure.
    """

    name: str
    description: str | None = None
    origin: AuthOrigin = "unknown"
    ranges: list[AuthValueRange] = Field(default_factory=list)
    hierarchy_nodes: list[AuthHierarchyNode] = Field(default_factory=list)
    grants_everything: bool = False
    assigned_users: list[str] = Field(default_factory=list)
    contains_data_values: Literal[True] = True
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class AuthRelevantCharacteristic(BaseModel):
    """A characteristic flagged authorisation-relevant, and whether anything authorises it.

    The gap this exposes: a characteristic marked authorisation-relevant that **no**
    authorisation covers blocks every query touching it. That is a live configuration fault,
    not a style issue, and it is invisible unless the two sides are compared.
    """

    model_config = ConfigDict(extra="forbid")

    characteristic: str
    description: str | None = None
    authorisation_count: int = 0
    covered: bool = False
    provenance: Provenance | list[Provenance]


class SecurityOverview(BoundedResult):
    """The landscape's row-level security posture, without any concrete permission values."""

    authorisation_count: int = 0
    generated_count: int = 0
    maintained_count: int = 0
    catch_all_authorisations: list[str] = Field(default_factory=list)
    unrestricted_users: list[str] = Field(default_factory=list)  # holders of a catch-all
    auth_relevant_characteristics: list[AuthRelevantCharacteristic] = Field(default_factory=list)
    uncovered_characteristics: list[str] = Field(default_factory=list)
    users_with_any_authorisation: int | None = None
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class QueryAuthExposure(BaseModel):
    """Which authorisation-relevant characteristics a query is subject to.

    Answers "does this report show different data to different people, and on which
    fields?" - the question behind most BW audit findings. It reports the *characteristics
    in play*, never who sees what: that needs a value join per user, which this tool
    deliberately does not do.

    ``authorization_variables`` are the query's own variables filled from the user's
    authorisations at runtime (``RSZGLOBV.VPROCTP = 6``). They are a metadata dead end in
    the same way customer-exit variables are: BW names them, and their value exists only
    per session.
    """

    model_config = ConfigDict(extra="forbid")

    compuid: str
    compid: str | None = None
    providers: list[str] = Field(default_factory=list)
    auth_relevant_characteristics: list[str] = Field(default_factory=list)
    uncovered_characteristics: list[str] = Field(default_factory=list)
    authorization_variables: list[str] = Field(default_factory=list)
    user_specific_result: bool = False  # any auth-relevant characteristic or auth variable in play
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]
