"""Font resolution for PNG diagram rendering (optional ``viz`` extra).

Pillow's built-in bitmap font cannot scale, so supersampled output needs a TrueType face. We try a
short list of faces that ship with common operating systems and fall back to the bitmap default, so
rendering always succeeds — just less crisply — rather than raising on a machine without fonts.

No font is bundled or downloaded: only faces already present on the host are used.
"""

from __future__ import annotations

from typing import Any

# Candidate TrueType faces, in preference order, across Windows / macOS / Linux.
_CANDIDATES: tuple[str, ...] = (
    "segoeui.ttf",  # Windows
    "arial.ttf",
    "calibri.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",  # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
)

# Logical role -> point size at scale 1.
_SIZES: dict[str, int] = {"title": 17, "node": 12, "small": 10}


def load_fonts(scale: int = 1) -> dict[str, Any]:
    """Return a ``{role: font}`` mapping for the diagram roles at the given supersample scale."""
    from PIL import ImageFont  # noqa: PLC0415

    fonts: dict[str, Any] = {}
    for role, size in _SIZES.items():
        fonts[role] = _load_one(ImageFont, max(1, size * max(1, scale)))
    return fonts


def _load_one(image_font: Any, size: int) -> Any:
    for candidate in _CANDIDATES:
        try:
            return image_font.truetype(candidate, size)
        except OSError:
            continue
    return image_font.load_default()
