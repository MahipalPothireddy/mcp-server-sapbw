"""Snapshots of a system's metadata, and the diff between two of them.

**The two questions this answers.** "What changed since last week?" and "What is different between
QA and production?" BW answers neither. A transport log says what moved, not what the result was,
and it says nothing about an object changed outside transport.

A snapshot is a set of **fingerprints**, not a copy of the metadata. Each object contributes a
handful of structural facts, hashed; the facts are kept alongside so a diff can say *which* fact
changed rather than just that something did. Fingerprints keep a snapshot small (a real system's
24,000 objects fit in a few hundred kilobytes) and make the comparison a set operation.

Three things decide whether a diff is useful or noise, and all three are handled explicitly:

* **Volatile facts are excluded from the fingerprint.** Last-changed timestamps, last-used dates,
  record counts and request counters change constantly without anything structural changing.
  Including them would report every object as changed on every run, which is the same as reporting
  nothing. :data:`VOLATILE_FIELDS` names what is deliberately left out.
* **Capability parity is checked before comparing.** A table present on one system and absent on the
  other would surface as thousands of removed objects. Families only one side can report are
  excluded from the diff and named in ``comparability``.
* **Environment-specific names are normalised.** A DataSource endpoint is stored as the DataSource
  name padded out and suffixed with its logical system, and the logical system differs per
  environment because BDLS rewrites it. Comparing raw would make every DataSource look replaced.
  The logical system is stripped from the identity and kept as a *fact*, so a genuine BDLS
  difference shows as one changed fact instead of an add and a remove.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .completeness import BoundedResult
from .objects import BwObjectRef

#: Object families a snapshot can capture. Selectable, because volume differs by two orders of
#: magnitude - a reference system has 593 providers and 14,034 InfoObjects.
SnapshotFamily = Literal[
    "providers",
    "transformations",
    "chains",
    "datasources",
    "dtps",
    "queries",
    "infoobjects",
]

#: The families captured when a caller names none. Chosen as the ones that answer "what changed in
#: the dataflow", which is the common question, without pulling every InfoObject and query.
DEFAULT_FAMILIES: tuple[SnapshotFamily, ...] = (
    "providers",
    "transformations",
    "chains",
    "datasources",
    "dtps",
)

#: Facts that change without anything structural changing, and are therefore never fingerprinted.
#: Naming them here rather than leaving them out silently means a reader can see the decision, and a
#: test can assert none of them reaches a fingerprint.
VOLATILE_FIELDS: frozenset[str] = frozenset(
    {
        "TSTPNM",  # last changed by
        "TIMESTMP",  # last changed at
        "CRNM",  # created by
        "CRTSTP",  # created at
        "LASTUSED",  # query last execution
        "CONTTIMESTMP",  # content timestamp
        "GENTIME",
        "MODTIME",
        "REPTIME",
        "row_estimate",
        "request_count",
        "record_count",
    }
)

ChangeKind = Literal["added", "removed", "changed"]


class ObjectFingerprint(BaseModel):
    """One object's structural identity at a point in time.

    ``facts`` is what went into the hash. Keeping it is what makes a diff explain itself: without
    it, a changed fingerprint says only that something moved.
    """

    model_config = ConfigDict(extra="forbid")

    ref: BwObjectRef
    fingerprint: str
    facts: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.ref.id


class SnapshotEdge(BaseModel):
    """A dependency edge, by canonical id, so the shape can be diffed as well as the objects."""

    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    kind: str = "transformation"

    @property
    def key(self) -> str:
        return f"{self.src}|{self.kind}|{self.dst}"


class SnapshotScope(BaseModel):
    """What the snapshot covers, and where it stopped.

    A partial snapshot is usable; a partial snapshot that does not say so is not, because a diff
    against it would report the missing part as removed.
    """

    model_config = ConfigDict(extra="forbid")

    families: list[str] = Field(default_factory=list)
    #: Families asked for but skipped because this release lacks the tables they need.
    families_unavailable: list[str] = Field(default_factory=list)
    per_family_counts: dict[str, int] = Field(default_factory=dict)
    truncated_families: list[str] = Field(default_factory=list)
    row_cap: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.truncated_families)


class Snapshot(BaseModel):
    """A system's structural metadata at one moment, as fingerprints."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    system: str
    taken_at: datetime
    bw_release: str
    abap_schema: str
    server_version: str
    #: Which contract build produced it, so a diff can tell whether the two sides were captured by
    #: the same understanding of the metadata.
    contract_revision: str | None = None
    #: Digest of which capabilities were available. Two snapshots with different digests were taken
    #: against systems that can report different things, which bounds what a diff can claim.
    capability_digest: str = ""
    available_capabilities: list[str] = Field(default_factory=list)
    objects: list[ObjectFingerprint] = Field(default_factory=list)
    edges: list[SnapshotEdge] = Field(default_factory=list)
    scope: SnapshotScope = Field(default_factory=SnapshotScope)
    caveats: list[str] = Field(default_factory=list)

    @property
    def object_count(self) -> int:
        return len(self.objects)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


class SnapshotSummary(BoundedResult):
    """A snapshot's identity without its contents, for listing and for diff headers."""

    snapshot_id: str
    system: str
    taken_at: datetime
    bw_release: str
    object_count: int = 0
    edge_count: int = 0
    families: list[str] = Field(default_factory=list)


class FactChange(BaseModel):
    """One structural fact that differs, with both sides."""

    model_config = ConfigDict(extra="forbid")

    fact: str
    before: str | None = None
    after: str | None = None


class ObjectChange(BaseModel):
    """An object that appeared, disappeared, or changed structurally."""

    model_config = ConfigDict(extra="forbid")

    ref: BwObjectRef
    change: ChangeKind
    #: Populated for ``changed`` only. Empty on a changed row would mean the fingerprint moved for a
    #: reason the fact list does not cover, which is a defect worth seeing rather than hiding.
    facts: list[FactChange] = Field(default_factory=list)


class RekeyedObject(BaseModel):
    """One object that appears added on one side and removed on the other, but is the same flow.

    Transformations and DTPs are identified by a generated technical id. An object transported
    between systems keeps its id, but one re-created by hand in each system gets a different one, so
    a plain set difference reports the same dataflow as both an addition and a removal. Measured
    between two real environments: of 1,269 and 1,271 transformations, only 471 ids matched, while
    the endpoints told a very different story.

    ``matched_on`` is the endpoint signature both sides share. This is an interpretation, not a
    metadata fact - the two objects may genuinely differ inside - so the pair also stays in
    ``added`` and ``removed``, which remain the plain set difference.
    """

    model_config = ConfigDict(extra="forbid")

    before: BwObjectRef
    after: BwObjectRef
    matched_on: str


class SnapshotDiff(BoundedResult):
    """What differs between two snapshots, and what the comparison could not cover.

    ``comparable`` is the field to read first. False means the two sides can report different things
    - a different release, or a capability present on one only - so an absence on one side is not
    evidence of a difference. The diff is still returned, restricted to what both sides can see, and
    ``comparability`` says what was excluded and why.
    """

    left: SnapshotSummary
    right: SnapshotSummary
    comparable: bool = True
    comparability: list[str] = Field(default_factory=list)
    #: Families compared, after removing any only one side can report.
    families_compared: list[str] = Field(default_factory=list)
    families_excluded: list[str] = Field(default_factory=list)
    added: list[ObjectChange] = Field(default_factory=list)
    removed: list[ObjectChange] = Field(default_factory=list)
    changed: list[ObjectChange] = Field(default_factory=list)
    #: Pairs from ``added`` and ``removed`` that connect the same two objects under different
    #: technical ids. Read this before concluding a flow was deleted and another one built.
    rekeyed: list[RekeyedObject] = Field(default_factory=list)
    added_edges: list[SnapshotEdge] = Field(default_factory=list)
    removed_edges: list[SnapshotEdge] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    #: How many objects each differing fact accounts for, highest first. This is what separates a
    #: systematic difference from a real one: 814 DataSources differing only on their logical system
    #: is BDLS doing its job, while one of them differing that way is a provider pointed at the
    #: wrong source. The per-object list says the same thing, but only after a reader counts it.
    changed_by_fact: dict[str, int] = Field(default_factory=dict)
    #: Identity rules applied before comparing, so a caller knows what was made equal on purpose.
    normalisations: list[str] = Field(default_factory=list)
    #: Which bound stopped either side's capture; a diff over a bounded capture cannot
    caveats: list[str] = Field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def structurally_identical(self) -> bool:
        """Identical once objects re-created under a new technical id are set aside.

        The useful reading of a cross-environment comparison: the same dataflow built twice is not a
        difference in what the system does, even though the ids differ.
        """
        return not self.changed and len(self.rekeyed) * 2 == len(self.added) + len(self.removed)


def fingerprint_facts(facts: dict[str, str]) -> str:
    """Hash a fact map, ignoring key order and anything volatile.

    Volatile keys are dropped here rather than at the call site, so a new reader cannot accidentally
    fingerprint a timestamp and make every object look changed on every run.
    """
    stable = {k: v for k, v in sorted(facts.items()) if k not in VOLATILE_FIELDS}
    payload = "\u001f".join(f"{k}={v}" for k, v in stable.items())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
