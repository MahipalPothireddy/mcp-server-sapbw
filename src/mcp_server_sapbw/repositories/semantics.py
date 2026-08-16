"""Business-area repository: resolve BW's InfoArea grouping into something readable.

Three reads, in this order: the hierarchy (``RSDAREA``) for parents, the texts (``RSDAREAT``) for
names, and the provider headers for assignments. The hierarchy comes first because a provider's area
code is only useful once it can be placed - and an area a provider references but the hierarchy does
not contain is reported as unresolved rather than quietly dropped.

Assignment is BW's own ``INFOAREA`` field. Nothing is inferred from a naming convention: a wrong
functional attribution routes a change review to the wrong team, which is worse than admitting the
object is unassigned.
"""

from __future__ import annotations

from typing import Any

from ..models.provenance import UnsupportedResult
from ..models.semantics import BusinessArea, SemanticMap
from .base import Repository

#: Provider headers carrying an INFOAREA, with the logical table and the object type to count under.
#: The InfoObject header is absent on purpose: RSDIOBJ carries BWAPPL, not INFOAREA.
_PROVIDER_SOURCES: tuple[tuple[str, str], ...] = (
    ("dso_header", "dso"),
    ("adso_header", "adso"),
    ("cube_header", "cube"),
    ("composite_header", "compositeprovider"),
)

#: Guard against a pathological hierarchy (a cycle in PARENT_AREA would otherwise loop).
_MAX_AREA_DEPTH = 12


class SemanticsRepository(Repository):
    """Reads the InfoArea hierarchy and what is assigned to it."""

    def business_areas(
        self, *, limit: int = 100, offset: int = 0
    ) -> SemanticMap | UnsupportedResult:
        unsupported = self.require("info_area")
        if unsupported is not None:
            return unsupported

        parents = self._parents()
        names = self._names()
        assignments, unassigned, per_area_missing = self._assignments()

        caveats: list[str] = []
        if not self.capability.is_available("info_area_text"):
            caveats.append(
                f"{self.physical('info_area_text')} is unavailable, so areas are reported by code "
                "without a name. The grouping is unaffected; only its labels are missing."
            )

        unresolved = sorted(per_area_missing - set(parents))
        area_codes = sorted(set(parents) | set(assignments))
        built: list[BusinessArea] = []
        for area in area_codes:
            path = self._path(area, parents)
            counts = assignments.get(area, {})
            built.append(
                BusinessArea(
                    area=area,
                    name=names.get(area),
                    parent=parents.get(area) or None,
                    path=path,
                    depth=max(len(path) - 1, 0),
                    provider_counts=dict(sorted(counts.items())),
                    provider_total=sum(counts.values()),
                    provenance=[self.provenance("info_area", {"INFOAREA": area})],
                )
            )

        built.sort(key=lambda a: (-a.provider_total, a.area))
        empty = [a.area for a in built if a.provider_total == 0]

        if unassigned:
            caveats.append(
                f"{unassigned} provider(s) carry no InfoArea. They are counted, not attributed: no "
                "business area is inferred from a naming convention, because a wrong functional "
                "attribution sends a change review to the wrong team."
            )
        if unresolved:
            caveats.append(
                f"{len(unresolved)} area code(s) are referenced by a provider but absent from "
                f"{self.physical('info_area')}, so they could not be placed in the hierarchy: "
                f"{', '.join(unresolved[:10])}. A missing name is not a missing assignment."
            )
        caveats.append(
            "assignment is BW's declared INFOAREA field only. An area is a modelling grouping, so "
            "it reflects how the landscape was built rather than an authoritative business owner."
        )

        return SemanticMap(
            system=self.capability.system,
            areas=built[offset : offset + limit],
            total_count=len(built),
            limit=limit,
            offset=offset,
            unassigned_providers=unassigned,
            empty_areas=empty,
            unresolved_areas=unresolved,
            caveats=caveats,
        )

    # --- reads ----------------------------------------------------------------------------

    def _parents(self) -> dict[str, str]:
        """``{area: parent}`` from the hierarchy table. An empty parent means a root."""
        rows = self.select(
            self.dialect.build_select(
                columns=["INFOAREA", "PARENT_AREA"],
                from_logical="info_area",
            )
        )
        return {
            str(area).strip(): str(parent or "").strip()
            for area, parent in rows
            if str(area or "").strip()
        }

    def _names(self) -> dict[str, str]:
        """``{area: text}``. Absent texts are simply missing; nothing is back-filled."""
        if not self.capability.is_available("info_area_text"):
            return {}
        try:
            rows = self.select(
                self.dialect.build_select(
                    columns=["INFOAREA", "TXTLG"],
                    from_logical="info_area_text",
                )
            )
        except Exception:
            return {}
        names: dict[str, str] = {}
        for area, text in rows:
            code = str(area or "").strip()
            label = str(text or "").strip()
            if code and label and code not in names:
                names[code] = label
        return names

    def _assignments(self) -> tuple[dict[str, dict[str, int]], int, set[str]]:
        """``({area: {object_type: count}}, unassigned_total, every referenced area code)``."""
        assignments: dict[str, dict[str, int]] = {}
        unassigned = 0
        referenced: set[str] = set()

        for logical, object_type in _PROVIDER_SOURCES:
            if not self.capability.is_available(logical):
                continue
            rows = self._area_counts(logical)
            for area, count in rows:
                code = str(area or "").strip()
                if not code:
                    unassigned += count
                    continue
                referenced.add(code)
                assignments.setdefault(code, {})[object_type] = (
                    assignments.setdefault(code, {}).get(object_type, 0) + count
                )
        return assignments, unassigned, referenced

    def _area_counts(self, logical: str) -> list[tuple[Any, int]]:
        """One grouped read per provider family, so no provider row is fetched individually."""
        query = self.dialect.build_select(
            columns=["INFOAREA", "COUNT(*)"],
            from_logical=logical,
            group_by=["INFOAREA"],
        )
        rows = self.select(query)
        return [(row[0], int(row[1] or 0)) for row in rows]

    @staticmethod
    def _path(area: str, parents: dict[str, str]) -> list[str]:
        """Root-to-leaf path of codes, stopping on a cycle rather than looping."""
        path = [area]
        seen = {area}
        current = area
        for _ in range(_MAX_AREA_DEPTH):
            parent = parents.get(current, "")
            if not parent or parent in seen:
                break
            path.append(parent)
            seen.add(parent)
            current = parent
        return list(reversed(path))
