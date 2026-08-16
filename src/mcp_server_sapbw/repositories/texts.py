"""Stored-text reading across BW's two text-table shapes (B4).

BW stores object descriptions in two structurally different ways, both discovered live in B4:

* **classic** ``RSD*T`` tables (RSDODSOT, RSDCUBET, RSDIOBJT, RSTRANT): one row per language keyed
  by ``(object_id, OBJVERS, LANGU)`` with ``TXTSH`` (short) and ``TXTLG`` (long);
* **hana** ``RSO*T`` tables (RSOADSOT, RSOHCPRT): rows keyed additionally by ``COLNAME`` with
  ``DESCRIPTION`` and ``QUICK_INFO``. The object's own text is the ``COLNAME = ''`` row; the other
  rows carry per-field texts.

This repository only *reads* stored text (mission Rule 1: never writes back). Quality assessment and
generation live in the descriptions service.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..models.provenance import Provenance
from .base import Repository

TextShape = Literal["classic", "hana"]

# Primary language for descriptions. The DB (hdbcli) connection has no SAP logon language, so we
# default to English and fall back to any available language, flagging unexpected languages in the
# quality assessment rather than hiding them.
DEFAULT_LANGUAGE = "E"


@dataclass(frozen=True)
class TextTableSpec:
    """Describes how to read one object type's texts: which table, id column, and shape."""

    text_logical: str
    id_column: str
    shape: TextShape


@dataclass(frozen=True)
class StoredText:
    """A stored description as read from BW (before quality assessment/generation)."""

    short: str | None
    long: str | None
    language: str | None
    provenance: Provenance


def _clean(value: Any) -> str | None:
    """Trim a CHAR/NVARCHAR text value; return None when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lang_rank(language: str, preferred: str) -> int:
    """Rank a language: preferred first, then English, then anything else."""
    upper = language.upper()
    if upper == preferred.upper():
        return 0
    if upper == DEFAULT_LANGUAGE:
        return 1
    return 2


class TextsRepository(Repository):
    """Reads object and field descriptions across the classic and HANA text-table shapes."""

    def object_text(
        self, spec: TextTableSpec, object_id: str, *, preferred_language: str = DEFAULT_LANGUAGE
    ) -> StoredText | None:
        """Return the object's own stored description (preferred language, then English).

        Returns ``None`` when the text table is unavailable on this release or no row exists.
        """
        if not self.capability.is_available(spec.text_logical):
            return None

        if spec.shape == "classic":
            columns = ["LANGU", "TXTSH", "TXTLG"]
            where = [f"{spec.id_column} = ?"]
        else:  # hana: object-level row has an empty COLNAME
            columns = ["LANGU", "DESCRIPTION", "QUICK_INFO"]
            where = [f"{spec.id_column} = ?", "TRIM(COLNAME) = ''"]

        rows = self.select(
            self.dialect.build_select(
                columns=columns, from_logical=spec.text_logical, where=where, params=[object_id]
            )
        )
        picked, language = self._pick(rows, preferred_language)
        if picked is None:
            return None
        return StoredText(
            short=_clean(picked[1]),
            long=_clean(picked[2]),
            language=language,
            provenance=self.provenance(
                spec.text_logical, {spec.id_column: object_id, "LANGU": language or ""}
            ),
        )

    def object_texts(
        self,
        spec: TextTableSpec,
        object_ids: Sequence[str],
        *,
        preferred_language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, str]:
        """Short texts for many objects in one query: ``{object_id: short_text}``.

        A per-object :meth:`object_text` call would be correct but not affordable - an InfoObject
        can carry fifty attributes, and fifty round trips would exhaust a tool's query budget on
        descriptions alone. Objects with no row are simply absent from the result.
        """
        ids = [i for i in dict.fromkeys(str(i).strip() for i in object_ids) if i]
        if not ids or not self.capability.is_available(spec.text_logical):
            return {}

        short_column = "TXTSH" if spec.shape == "classic" else "DESCRIPTION"
        where = [f"{spec.id_column} IN ({', '.join('?' for _ in ids)})"]
        if spec.shape == "hana":
            where.append("TRIM(COLNAME) = ''")
        rows = self.select(
            self.dialect.build_select(
                columns=[spec.id_column, "LANGU", short_column],
                from_logical=spec.text_logical,
                where=where,
                params=list(ids),
            )
        )
        best: dict[str, tuple[int, str]] = {}
        for object_id, langu, short in rows:
            key = str(object_id).strip()
            text = _clean(short)
            if not key or text is None:
                continue
            rank = _lang_rank(str(langu).strip(), preferred_language)
            current = best.get(key)
            if current is None or rank < current[0]:
                best[key] = (rank, text)
        return {key: value[1] for key, value in best.items()}

    def field_texts(
        self, spec: TextTableSpec, object_id: str, *, preferred_language: str = DEFAULT_LANGUAGE
    ) -> dict[str, str]:
        """Per-field descriptions (COLNAME -> text); only the HANA shape has these, else ``{}``."""
        if spec.shape != "hana" or not self.capability.is_available(spec.text_logical):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["COLNAME", "LANGU", "DESCRIPTION"],
                from_logical=spec.text_logical,
                where=[f"{spec.id_column} = ?", "TRIM(COLNAME) <> ''"],
                params=[object_id],
            )
        )
        best: dict[str, tuple[int, str]] = {}  # colname -> (lang_rank, description)
        for colname, langu, desc in rows:
            column = str(colname).strip()
            text = _clean(desc)
            if not column or text is None:
                continue
            rank = _lang_rank(str(langu).strip(), preferred_language)
            current = best.get(column)
            if current is None or rank < current[0]:
                best[column] = (rank, text)
        return {column: value[1] for column, value in best.items()}

    @staticmethod
    def _pick(
        rows: list[tuple[Any, ...]], preferred: str
    ) -> tuple[tuple[Any, ...] | None, str | None]:
        """Pick the best-language row: preferred, then English, then the first available."""
        by_language: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            by_language.setdefault(str(row[0]).strip().upper(), row)
        for language in (preferred.upper(), DEFAULT_LANGUAGE):
            if language in by_language:
                return by_language[language], language
        if rows:
            first = rows[0]
            return first, str(first[0]).strip().upper()
        return None, None
