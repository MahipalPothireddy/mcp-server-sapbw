"""Source-system topology and extractor-enhancement inventory.

Two things the BW metadata can tell you about the *edge* of the warehouse:

* **Which systems feed it, and of what kind** — the logical systems DataSources extract from, joined
  to the source-system registry. This is what distinguishes an ECC extraction from a BW-to-BW load,
  a flat file, or a database connection, and it surfaces logical systems that DataSources reference
  but the registry does not know (the classic symptom of a system copy where BDLS was not run).
* **Which DataSources look enhanced** — customer-namespace fields appended to the extract structure,
  joined to the delta method and extractor program. The fields are hard evidence an enhancement
  exists; what the exit *does* lives in the source system and needs a connector.

``source_type`` is decoded from the ABAP dictionary where the domain documents the code. Several
codes occur in the data that the domain does not list, so those carry an advisory interpretation and
say so, rather than being presented with the same authority as a dictionary-backed decode.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import Evidence, evidence_for
from .provenance import Provenance

# How a source system supplies data. Values marked advisory below are widely-used conventions that
# this release's domain does not document.
SourceSystemKind = Literal[
    "sap_r3",  # '3' - dictionary-documented
    "staging_bapi",  # 'B' - dictionary-documented
    "flat_file",  # 'F' - dictionary-documented
    "bw_system",  # 'D' - advisory
    "self",  # 'M' - advisory (the BW system itself)
    "db_connect",  # 'G' - advisory
    "hana_local",  # 'H' - advisory
    "odp",  # 'O' - advisory
    "unknown",
]


class SourceSystem(BaseModel):
    """One logical system that supplies data to this BW system."""

    model_config = ConfigDict(extra="forbid")

    logical_system: str
    kind: SourceSystemKind = "unknown"
    kind_code: str | None = None  # raw SRCTYPE, so an undecoded value stays visible
    kind_confidence: Literal["dictionary", "advisory"] = "advisory"
    evidence: Evidence | None = None
    registered: bool = True  # False when DataSources use it but the registry has no entry
    active: bool | None = None
    datasource_count: int = 0
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_evidence(self) -> SourceSystem:
        if self.evidence is None:
            detail = None
            if self.kind_confidence == "advisory" and self.kind_code:
                detail = (
                    f"Source-system type code {self.kind_code!r} is not documented by this "
                    f"system's ABAP dictionary; {self.kind!r} is the conventional reading."
                )
            self.evidence = evidence_for("source_system_kind", self.kind_confidence, detail=detail)
        return self


class SourceTopology(BaseModel):
    """The set of systems feeding this BW system, and what could not be resolved."""

    model_config = ConfigDict(extra="forbid")

    systems: list[SourceSystem] = Field(default_factory=list)
    unregistered_count: int = 0
    total_datasources: int = 0
    caveats: list[str] = Field(default_factory=list)


class DataSourceEnhancement(BaseModel):
    """A DataSource that appears to carry an extractor enhancement.

    The customer-namespace fields are metadata-confirmed. Whether an enhancement exists is therefore
    evidence-backed; what its exit code *does* is not knowable from BW and needs an ECC connector.
    """

    model_config = ConfigDict(extra="forbid")

    datasource: str
    logical_system: str | None = None
    delta_method: str | None = None  # raw RSDS/ROOSOURCE DELTA (domain has no fixed values)
    request_type: str | None = None  # decoded RSDS.TYPE (transaction data / master data / text ...)
    application: str | None = None
    extractor: str | None = None  # ROOSOURCE.EXTRACTOR program
    extraction_method: str | None = None  # ROOSOURCE.EXMETHOD
    customer_field_count: int = 0
    customer_fields: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class EnhancementInventory(BaseModel):
    """Extractor-enhancement inventory across DataSources."""

    model_config = ConfigDict(extra="forbid")

    enhanced: list[DataSourceEnhancement] = Field(default_factory=list)
    enhanced_count: int = 0  # DataSources with at least one customer-namespace field
    total_datasources: int = 0
    truncated: bool = False
    connector_required: str | None = None  # names the connector needed for the exit logic
    caveats: list[str] = Field(default_factory=list)
