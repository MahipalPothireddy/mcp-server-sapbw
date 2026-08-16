"""Business-area models: BW's own semantic grouping, resolved rather than left as a code.

**What was missing.** Every provider header carries an ``INFOAREA``, and the repositories already
read it - so ``info_area: "AREA_04"`` reached callers as an opaque code. The tables giving it a name
and a position in the hierarchy (``RSDAREA``, ``RSDAREAT``) were not in the capability map at all.
BW's own answer to "what is this object *for*" was being read and thrown away.

**Declared, never inferred.** An assignment comes from BW's ``INFOAREA`` field and nothing else. No
business area is guessed from a naming convention, because a convention is a site habit rather than
a fact and a wrong functional attribution is worse than none - it sends a change review to the wrong
team. Objects with no InfoArea are reported as ``unassigned``, which is a finding in its own right,
not a gap to be filled by pattern-matching.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance

#: How an object's business area was established. Only one value exists today, stated explicitly so
#: that adding an inferred route later cannot silently blend into the declared one.
AreaBasis = Literal["declared_infoarea"]


class BusinessArea(BaseModel):
    """One InfoArea: its name, where it sits, and what it contains."""

    model_config = ConfigDict(extra="forbid")

    area: str = Field(min_length=1)
    #: From RSDAREAT. ``None`` when BW holds no text - reported rather than back-filled with the
    #: code, so an undocumented area stays visible as one.
    name: str | None = None
    #: Parent InfoArea (RSDAREA.PARENT_AREA). ``None`` for a root.
    parent: str | None = None
    #: Root-to-leaf path of area codes, so a functional area can be read without walking the tree.
    path: list[str] = Field(default_factory=list)
    depth: int = 0
    #: Providers assigned to this area, counted by object type.
    provider_counts: dict[str, int] = Field(default_factory=dict)
    provider_total: int = 0
    basis: AreaBasis = "declared_infoarea"
    provenance: list[Provenance] = Field(default_factory=list)


class SemanticMap(BaseModel):
    """The landscape's business areas, plus what BW does not place in one."""

    model_config = ConfigDict(extra="forbid")

    system: str
    areas: list[BusinessArea] = Field(default_factory=list)
    total_count: int = 0
    limit: int = 0
    offset: int = 0
    #: Providers carrying no InfoArea. A count rather than a list at this level; the number is the
    #: signal, and naming thousands of objects would bury it.
    unassigned_providers: int = 0
    #: Areas BW names but nothing is assigned to - usually an organising node, sometimes a leftover.
    empty_areas: list[str] = Field(default_factory=list)
    #: Areas referenced by a provider but absent from RSDAREA, which means the hierarchy read could
    #: not place them. Reported so a missing name is never mistaken for a missing assignment.
    unresolved_areas: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
