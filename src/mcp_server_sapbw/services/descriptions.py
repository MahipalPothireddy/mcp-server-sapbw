"""Description subsystem: quality assessment + evidence-based generation (B4, mission Section 7).

Given a stored text (or its absence) and a technical name, this service decides how good the stored
description is and, when it is missing or low-value, produces a synthesized one from evidence the
server already holds. Crucially, a generated description is never presented as if it were stored:
the returned :class:`Description` carries ``origin`` (stored / generated / stored_augmented) and
``quality_flag`` (mission Rule 7). This service holds no SQL; the caller supplies the stored text
and a pre-built evidence summary.
"""

from __future__ import annotations

from ..models.description import Description, QualityFlag
from ..repositories.texts import StoredText

# Minimum word count below which a stored short text is treated as low-value (mission Section 7).
_MIN_MEANINGFUL_WORDS = 4

# Copy/template leftovers that mark a description as a copy artifact (checked case-insensitively).
_COPY_ARTIFACT_PREFIXES = ("copy of", "kopie von", "copy_of", "test_", "tmp_", "zz_")
_COPY_ARTIFACT_EXACT = frozenset({"tmp", "test", "dummy", "delete", "xxx", "todo", "n/a", "tbd"})
_COPY_ARTIFACT_SUBSTRINGS = ("zz_test", "do not use", "obsolete", "delete me")


def _is_copy_artifact(lowered: str) -> bool:
    """True when a (lower-cased) short text looks like a copy/template leftover."""
    return (
        lowered in _COPY_ARTIFACT_EXACT
        or lowered.startswith(_COPY_ARTIFACT_PREFIXES)
        or any(marker in lowered for marker in _COPY_ARTIFACT_SUBSTRINGS)
    )


class DescriptionService:
    """Assesses stored descriptions and generates labelled replacements when they are low-value."""

    def assess(self, stored_short: str | None, technical_name: str) -> QualityFlag:
        """Classify the *stored* short text: ok / missing / generic / copy_artifact."""
        if stored_short is None or not stored_short.strip():
            return "missing"
        text = stored_short.strip()
        lowered = text.lower()
        if _is_copy_artifact(lowered):
            return "copy_artifact"
        equals_name = lowered == technical_name.strip().lower()
        too_short = len(text.split()) < _MIN_MEANINGFUL_WORDS
        return "generic" if (equals_name or too_short) else "ok"

    def build(
        self,
        *,
        technical_name: str,
        stored: StoredText | None,
        generated_summary: str | None,
        evidence: list[str],
    ) -> Description:
        """Produce the final labelled description.

        - ``ok``: return the stored text verbatim (``origin='stored'``).
        - ``generic``: keep the stored short, add the generated summary as long context if available
          (``origin='stored_augmented'``), else return the stored text as-is.
        - ``missing`` / ``copy_artifact``: return the generated summary when available
          (``origin='generated'``), else a stored/placeholder result with the assessed flag.
        """
        stored_short = stored.short if stored else None
        stored_long = stored.long if stored else None
        language = stored.language if stored else None
        # Assess the substantive text: the long text when present, else the short (short texts are
        # deliberately brief, so judging them by the <4-word rule would flag almost everything).
        flag = self.assess(stored_long or stored_short, technical_name)

        if flag == "ok":
            return Description(
                description_short=stored_short,
                description_long=stored_long,
                origin="stored",
                quality_flag="ok",
                language=language,
                evidence=evidence,
            )

        if generated_summary is None:
            # Nothing to synthesize from: return whatever stored text exists, honestly flagged.
            return Description(
                description_short=stored_short,
                description_long=stored_long,
                origin="stored",
                quality_flag=flag,
                language=language,
                evidence=evidence,
            )

        if flag == "generic":
            return Description(
                description_short=stored_short,
                description_long=stored_long or generated_summary,
                origin="stored_augmented",
                quality_flag="generic",
                language=language,
                evidence=evidence,
            )

        # missing / copy_artifact: the stored text is unusable, so lead with the generated summary.
        return Description(
            description_short=generated_summary,
            description_long=None,
            origin="generated",
            quality_flag=flag,
            language=None,
            evidence=evidence,
        )
