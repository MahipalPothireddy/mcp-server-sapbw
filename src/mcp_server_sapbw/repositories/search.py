"""Cross-object search (B4): fuzzy match by technical name or description.

Backs bw_search_objects. Searches process chains, every present InfoProvider variant, and
InfoObjects, matching a pattern against the technical name (``id LIKE``) and, optionally, the
stored description (``UPPER(desc) LIKE``). Bare patterns are substring matches with ``_`` escaped
as a literal (BW names are full of underscores); a pattern containing ``%`` is passed through so
the caller controls the wildcards — see :func:`..core.dialect.like_term`. Results are
object-type-tagged, carry provenance citing
the table the match was found in, and are de-duplicated with name matches taking precedence over
description matches. Each per-source scan is internally capped, so ``total_count`` reflects the
collected (capped) result set for very broad patterns; narrow the pattern for exhaustive results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.dialect import LikeTerm, like_term
from ..models.providers import ProviderType, SearchHit, SearchObjectType, classify_cube_type
from .base import Repository
from .texts import TextShape

# Per-source scan cap: broad patterns are truncated here rather than scanning entire text tables.
_INTERNAL_CAP = 2000

_CUBE_TYPES: frozenset[SearchObjectType] = frozenset(
    {"infocube", "multiprovider", "virtualprovider"}
)


@dataclass(frozen=True)
class _Source:
    """A fixed-type search source (header table + optional object-level text table)."""

    object_type: SearchObjectType
    header_logical: str
    id_column: str
    text_logical: str | None = None
    text_desc_column: str | None = None
    text_shape: TextShape | None = None
    header_where: tuple[str, ...] = ()  # extra header conditions (chains need OBJVERS='A')
    text_where: tuple[str, ...] = ()  # extra text conditions (chains need OBJVERS='A')


# RSD*/RSO* tables get OBJVERS='A' auto-injected by the dialect; the chain RSP* tables do not.
_SIMPLE_SOURCES: tuple[_Source, ...] = (
    _Source(
        object_type="chain",
        header_logical="chain_attr",
        id_column="CHAIN_ID",
        text_logical="chain_text",
        text_desc_column="TXTLG",
        text_shape="classic",
        header_where=("OBJVERS = 'A'",),
        text_where=("OBJVERS = 'A'",),
    ),
    _Source(
        object_type="dso",
        header_logical="dso_header",
        id_column="ODSOBJECT",
        text_logical="dso_text",
        text_desc_column="TXTLG",
        text_shape="classic",
    ),
    _Source(
        object_type="adso",
        header_logical="adso_header",
        id_column="ADSONM",
        text_logical="adso_text",
        text_desc_column="DESCRIPTION",
        text_shape="hana",
    ),
    _Source(
        object_type="compositeprovider",
        header_logical="composite_header",
        id_column="HCPRNM",
        text_logical="composite_text",
        text_desc_column="DESCRIPTION",
        text_shape="hana",
    ),
    _Source(
        object_type="infoobject",
        header_logical="infoobject",
        id_column="IOBJNM",
        text_logical="infoobject_text",
        text_desc_column="TXTLG",
        text_shape="classic",
    ),
)


def _to_like(pattern: str) -> LikeTerm:
    """Turn a search term into a LIKE term (see :func:`..core.dialect.like_term`)."""
    return like_term(pattern)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classify_cube(cubetype: Any) -> ProviderType:
    return classify_cube_type(cubetype)


class SearchRepository(Repository):
    """Cross-object fuzzy search over chains, providers, and InfoObjects."""

    def search(
        self,
        pattern: str,
        *,
        object_types: list[str] | None = None,
        match_descriptions: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SearchHit], int]:
        wanted = set(object_types) if object_types else None
        like = _to_like(pattern)
        hits: dict[tuple[str, str], SearchHit] = {}

        for source in _SIMPLE_SOURCES:
            if wanted is not None and source.object_type not in wanted:
                continue
            if not self.capability.is_available(source.header_logical):
                continue
            self._collect_names(source, like, hits)
            if match_descriptions:
                self._collect_descriptions(source, like, hits)

        if self.capability.is_available("cube_header") and (
            wanted is None or bool(wanted & _CUBE_TYPES)
        ):
            self._collect_cube(like, wanted, match_descriptions, hits)

        ordered = sorted(hits.values(), key=lambda h: (h.object_type, h.name))
        total = len(ordered)
        return ordered[offset : offset + limit], total

    # --- fixed-type sources --------------------------------------------------------------

    def _collect_names(
        self, source: _Source, like: LikeTerm, hits: dict[tuple[str, str], SearchHit]
    ) -> None:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[source.id_column],
                    from_logical=source.header_logical,
                    where=[*source.header_where, like.clause(source.id_column)],
                    params=[like.value],
                    order_by=[source.id_column],
                ),
                limit=_INTERNAL_CAP,
            )
        )
        for row in rows:
            name = _clean(row[0])
            if name is None:
                continue
            hits[(source.object_type, name)] = SearchHit(
                name=name,
                object_type=source.object_type,
                matched_on="name",
                provenance=self.provenance(
                    source.header_logical, {source.id_column: name, "OBJVERS": "A"}
                ),
            )

    def _collect_descriptions(
        self, source: _Source, like: LikeTerm, hits: dict[tuple[str, str], SearchHit]
    ) -> None:
        if source.text_logical is None or not self.capability.is_available(source.text_logical):
            return
        where = [*source.text_where, like.clause(f"UPPER({source.text_desc_column})")]
        if source.text_shape == "hana":
            where.append("TRIM(COLNAME) = ''")
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[source.id_column, source.text_desc_column or "TXTLG"],
                    from_logical=source.text_logical,
                    where=where,
                    params=[like.value],
                    # Ordered because the scan is capped: a description search that binds the cap
                    # would otherwise return a different arbitrary slice of the matches each time
                    # it ran, so the same query would find an object and then not find it (D8).
                    order_by=[source.id_column],
                ),
                limit=_INTERNAL_CAP,
            )
        )
        for row in rows:
            name = _clean(row[0])
            if name is None or (source.object_type, name) in hits:
                continue  # name matches take precedence
            hits[(source.object_type, name)] = SearchHit(
                name=name,
                object_type=source.object_type,
                description_short=_clean(row[1]),
                matched_on="description",
                provenance=self.provenance(
                    source.text_logical, {source.id_column: name, "OBJVERS": "A"}
                ),
            )

    # --- cube family (type derived from CUBETYPE) ----------------------------------------

    def _collect_cube(
        self,
        like: LikeTerm,
        wanted: set[str] | None,
        match_descriptions: bool,
        hits: dict[tuple[str, str], SearchHit],
    ) -> None:
        name_rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["INFOCUBE", "CUBETYPE"],
                    from_logical="cube_header",
                    where=[like.clause("INFOCUBE")],
                    params=[like.value],
                    order_by=["INFOCUBE"],
                ),
                limit=_INTERNAL_CAP,
            )
        )
        for infocube, cubetype in name_rows:
            name = _clean(infocube)
            if name is None:
                continue
            object_type = _classify_cube(cubetype)
            if wanted is not None and object_type not in wanted:
                continue
            hits[(object_type, name)] = SearchHit(
                name=name,
                object_type=object_type,
                matched_on="name",
                provenance=self.provenance("cube_header", {"INFOCUBE": name, "OBJVERS": "A"}),
            )

        if match_descriptions and self.capability.is_available("cube_text"):
            self._collect_cube_descriptions(like, wanted, hits)

    def _collect_cube_descriptions(
        self, like: LikeTerm, wanted: set[str] | None, hits: dict[tuple[str, str], SearchHit]
    ) -> None:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["INFOCUBE", "TXTLG"],
                    from_logical="cube_text",
                    where=[like.clause("UPPER(TXTLG)")],
                    params=[like.value],
                    order_by=["INFOCUBE"],  # capped scan; see _collect_descriptions
                ),
                limit=_INTERNAL_CAP,
            )
        )
        by_name: dict[str, str | None] = {}
        for row in rows:
            name = _clean(row[0])
            if name is not None:
                by_name.setdefault(name, _clean(row[1]))
        types = self._cube_types(list(by_name))
        for name, description in by_name.items():
            object_type = types.get(name)
            if object_type is None or (wanted is not None and object_type not in wanted):
                continue
            if (object_type, name) in hits:
                continue
            hits[(object_type, name)] = SearchHit(
                name=name,
                object_type=object_type,
                description_short=description,
                matched_on="description",
                provenance=self.provenance("cube_text", {"INFOCUBE": name, "OBJVERS": "A"}),
            )

    def _cube_types(self, names: list[str]) -> dict[str, ProviderType]:
        """Resolve INFOCUBE -> precise provider type (CUBETYPE) for a batch of names."""
        if not names:
            return {}
        placeholders = ", ".join("?" for _ in names)
        rows = self.select(
            self.dialect.build_select(
                columns=["INFOCUBE", "CUBETYPE"],
                from_logical="cube_header",
                where=[f"INFOCUBE IN ({placeholders})"],
                params=list(names),
            )
        )
        return {str(r[0]).strip(): _classify_cube(r[1]) for r in rows}
