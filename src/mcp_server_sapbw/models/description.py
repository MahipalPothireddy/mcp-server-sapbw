"""Object-description model (B4).

Every describable object (provider, InfoObject, and later query/transformation/routine) returns a
:class:`Description` carrying not just the text but where it came from and how trustworthy it is
(mission Section 7). A stored description and a synthesized one are never indistinguishable
(mission Rule 7): ``origin`` and ``quality_flag`` make the difference explicit, and ``evidence``
lists the metadata sources the description was built from (its provenance, in compact form).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# How the returned description was produced.
#   stored           - taken verbatim from the object's text table
#   generated        - synthesized from evidence (no usable stored text existed)
#   stored_augmented - a usable stored text, expanded with generated context
DescriptionOrigin = Literal["stored", "generated", "stored_augmented"]

# Quality of the *stored* text (assessed even when we then generate/augment).
#   ok             - a usable, meaningful stored description
#   missing        - no stored text at all
#   generic        - too short / equals the technical name / unexpected language
#   copy_artifact  - a copy/template leftover ("Copy of ...", "ZZ_TEST", "tmp", ...)
QualityFlag = Literal["ok", "missing", "generic", "copy_artifact"]


class Description(BaseModel):
    """A resolved object description with provenance and a generated-vs-stored label.

    ``evidence`` is the provenance for the description (mission Rule 3), in the compact
    ``TABLE`` or ``TABLE:key`` form the mission specifies, e.g. ``["RSDODSOT", "RSTRAN:0ABC123"]``.
    """

    model_config = ConfigDict(extra="forbid")

    description_short: str | None = None
    description_long: str | None = None
    origin: DescriptionOrigin
    quality_flag: QualityFlag
    language: str | None = None  # language code of the stored text actually used (None if none)
    evidence: list[str] = Field(default_factory=list)

    @classmethod
    def missing(cls, evidence: list[str] | None = None) -> Description:
        """A no-stored-text, not-yet-generated placeholder (quality_flag='missing')."""
        return cls(origin="stored", quality_flag="missing", evidence=evidence or [])
