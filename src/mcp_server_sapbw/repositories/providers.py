"""Provider repository (B4): the universal InfoProvider / InfoObject deep-dive.

One entry point, :meth:`ProvidersRepository.describe`, resolves any object by name across every
object-model variant present on the connected release (classic DSO, advanced DSO, InfoCube /
MultiProvider / virtual provider discriminated by RSDCUBE.CUBETYPE, CompositeProvider, InfoObject),
lists its fields, resolves MultiProvider parts, and attaches a labelled description (stored or
generated). Each variant is capability-gated: an absent table yields ``UnsupportedResult`` rather
than a guess. Objects whose tables exist but that are not found yield ``ObjectNotFound``.

CompositeProvider part-provider composition has no relational part table. BW stores the whole model
as XML on the header row of ``RSOHCPR``, so it is resolved here by two routes: the **declared
model** (:meth:`ProvidersRepository.composite_model`), else the base tables of the HANA calc view BW
generates for the provider. The second is a naming-convention reading and is labelled advisory.

The declared route reads ``XML_UI`` as well as ``XML_DEF``, which is what made it usable. Only
``XML_DEF`` was probed before, and on the reference production system it is empty for *all* 108
active CompositeProviders while ``XML_UI`` holds the complete model for all 108 — so every part of
every CompositeProvider was being reported as an inference from a generated table name when BW's own
declaration was one column away. A composition that could not be derived at all stays empty with a
caveat saying so — never implying a CompositeProvider has no parts.
"""

from __future__ import annotations

from typing import Any, Literal

from ..models.aggregation import KeyFigureAggregation
from ..models.completeness import BoundHit, Completeness, bounded
from ..models.composite import (
    CompositeFieldMapping,
    CompositeInput,
    CompositeModel,
    CompositePartKind,
    CompositeViewNode,
    composite_mapping_evidence,
)
from ..models.description import Description
from ..models.evidence import evidence_for
from ..models.provenance import UnsupportedResult
from ..models.providers import (
    AttributeRef,
    CompositionSource,
    InfoObjectKind,
    ObjectNotFound,
    PartProviderRef,
    Provider,
    ProviderField,
    ProviderType,
    classify_cube_type,
)
from ..services.aggregation import build_key_figure_aggregation
from ..services.composite_parser import (
    MAX_DEFINITION_BYTES,
    CompositeParseError,
    parse_composite_model,
)
from ..services.descriptions import DescriptionService
from ..services.table_resolver import (
    ResolvedKind,
    calc_view_patterns,
    candidate_tables,
    is_hierarchy_view,
    provider_from_calc_view,
    resolve_table,
)
from .base import Repository
from .texts import TextsRepository, TextTableSpec

# HANA schema holding generated BW calc views.
_CALC_SCHEMA = "_SYS_BIC"

# InfoObject names confirmed per catalogue read when resolving generated column names back to
# InfoObjects. A wide Advanced DSO has a few hundred columns, so this keeps it to a couple of reads.
_IOBJ_CHUNK = 300

#: ``(infoobject, resolution)`` for one generated column; see ``_infoobjects_for_columns``.
_Resolution = tuple[str, Literal["confirmed", "inferred", "none"]]

#: Generated-table namespaces whose prefix is *stripped* to recover the InfoObject name. Any other
#: ``/NS/`` prefix belongs to the InfoObject itself and is kept - see ``_infoobject_candidates``.
_GENERATED_NAMESPACES = ("/BIC/", "/BI0/")

#: A declared CompositeProvider part kind -> the provider vocabulary. A calc view is not a BW
#: InfoProvider and an undecoded kind is not a type, so both map to ``None`` and the part carries no
#: ``part_type`` rather than a wrong one; ``CompositeInput.part_kind`` keeps the finer answer.
_COMPOSITE_PART_TYPE: dict[CompositePartKind, ProviderType | None] = {
    "adso": "adso",
    "infocube": "infocube",
    "dso": "dso",
    "infoobject": "infoobject",
    "compositeprovider": "compositeprovider",
    "calcview": None,
    "unknown": None,
}


def _infoobject_candidates(column: str) -> list[str]:
    """Plausible InfoObject names for a generated column, best first.

    Every form is offered to the catalogue rather than one being chosen up front, because the column
    name alone does not say which convention produced it.
    """
    upper = column.strip().upper()
    if not upper:
        return []
    for namespace in _GENERATED_NAMESPACES:
        if upper.startswith(namespace):
            stripped = upper[len(namespace) :]
            # The stripped name first (a customer InfoObject), then with a leading zero in case the
            # generated column wrapped an SAP-delivered one.
            return [form for form in (stripped, f"0{stripped}") if form]
    if upper.startswith("/") and upper.count("/") >= _NAMESPACE_SLASHES:
        # A namespace of its own: the column already is the InfoObject name on this release.
        return [upper]
    return [f"0{upper}", upper]


#: A namespaced name is ``/NS/NAME`` - two slashes before the name itself.
_NAMESPACE_SLASHES = 2

#: Provider types that union other providers instead of holding their own rows.
#:
#: These are the ones where "check the request ledger" must be redirected to the parts, because the
#: union itself has no requests to find and never will (D58). A virtual provider is not
#: in this set: it reads through to a source system at query time, so it has neither requests nor
#: parts, and expanding it would be inventing a hop that does not exist.
_UNION_PROVIDER_TYPES = frozenset({"multiprovider", "compositeprovider"})


def _first_text(texts: dict[str, str], *keys: str | None) -> str | None:
    """The first maintained text found under any of ``keys``, case-insensitively."""
    if not texts:
        return None
    folded = {key.strip().upper(): value for key, value in texts.items()}
    for key in keys:
        if not key:
            continue
        found = texts.get(key) or folded.get(key.strip().upper())
        if found:
            return found
    return None


# SYS.OBJECT_DEPENDENCIES.DEPENDENCY_TYPE: 1 = direct, 2 = transitive.
#
# Part-provider resolution needs TRANSITIVE. Verified live: BW layers a CompositeProvider's calc
# view over intermediate views, so its *direct* table dependencies are only the master-data side
# tables of its navigation attributes (0 part-provider candidates across every sample), while the
# part providers' active tables appear one or more hops down as transitive dependencies. Scoping
# the scan to a single named view keeps this cheap (tens of rows), unlike a system-wide type-2 scan.
_TRANSITIVE_DEPENDENCY = 2
_DIRECT_DEPENDENCY = 1  # what the view itself names, as against what it can reach

# Header tables probed to confirm a provider name, most specific first. Unlike the table-name
# catalogue this includes CompositeProviders, which have no generated table of their own.
# (logical table, id column, provider type) - 'cube_header' is refined by CUBETYPE.
_PROVIDER_HEADER_SOURCES: tuple[tuple[str, str, ProviderType], ...] = (
    ("composite_header", "HCPRNM", "compositeprovider"),
    ("adso_header", "ADSONM", "adso"),
    ("dso_header", "ODSOBJECT", "dso"),
    ("cube_header", "INFOCUBE", "infocube"),
)
_MAX_PART_TABLES = 400  # transitive closure of one view; bounded, and reported when hit
_MAX_CATALOG = 20000  # provider-name catalogue used to confirm table -> object readings
# How many object names a caveat lists before eliding. A caveat points at the list; it is not a
# second copy of it (a characteristic can inherit 175 attributes).
_CAVEAT_NAMES = 10

# Table-resolver kind -> provider type vocabulary.
_KIND_TO_PROVIDER_TYPE: dict[ResolvedKind, ProviderType | None] = {
    "dso": "dso",
    "adso": "adso",
    "infocube": "infocube",
    "infoobject": "infoobject",
    "unknown": None,
}

# Per-type text-table wiring (see B4 discovery: classic RSD*T vs HANA RSO*T shapes).
_TEXT_SPECS: dict[ProviderType, TextTableSpec] = {
    "dso": TextTableSpec("dso_text", "ODSOBJECT", "classic"),
    "adso": TextTableSpec("adso_text", "ADSONM", "hana"),
    "infocube": TextTableSpec("cube_text", "INFOCUBE", "classic"),
    "multiprovider": TextTableSpec("cube_text", "INFOCUBE", "classic"),
    "virtualprovider": TextTableSpec("cube_text", "INFOCUBE", "classic"),
    "compositeprovider": TextTableSpec("composite_text", "HCPRNM", "hana"),
    "infoobject": TextTableSpec("infoobject_text", "IOBJNM", "classic"),
}

_TYPE_LABEL: dict[ProviderType, str] = {
    "dso": "Classic DataStore Object",
    "adso": "Advanced DataStore Object",
    "infocube": "InfoCube",
    "multiprovider": "MultiProvider",
    "virtualprovider": "VirtualProvider",
    "compositeprovider": "CompositeProvider",
    "infoobject": "InfoObject",
}

_IOBJTP_TO_KIND: dict[str, InfoObjectKind] = {
    "CHA": "characteristic",
    "KYF": "key_figure",
    "UNI": "unit",
    "TIM": "time",
    "DPA": "data_packet",
}

# Required logical tables per probe ('infocube' probe covers all cube variants).
_REQUIRED: dict[str, tuple[str, ...]] = {
    "dso": ("dso_header", "dso_field"),
    "adso": ("adso_header", "adso_text"),
    "infocube": ("cube_header", "cube_field"),
    "compositeprovider": ("composite_header",),
    "infoobject": ("infoobject",),
}

# Auto-detect probe order.
_AUTODETECT: tuple[str, ...] = ("dso", "adso", "infocube", "compositeprovider", "infoobject")
_ALL_HEADERS = ("dso_header", "adso_header", "cube_header", "composite_header", "infoobject")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _generate_summary(
    object_type: ProviderType,
    info_area: str | None,
    key_names: list[str],
    field_count: int,
    part_count: int,
) -> str:
    """Compose an evidence-based one-line summary (used only when stored text is low-value)."""
    parts = [_TYPE_LABEL[object_type]]
    if info_area:
        parts.append(f"in info area {info_area}")
    counts: list[str] = []
    if field_count:
        counts.append(f"{field_count} field{'s' if field_count != 1 else ''}")
    if part_count:
        counts.append(f"{part_count} part provider{'s' if part_count != 1 else ''}")
    if counts:
        parts.append("with " + " and ".join(counts))
    text = " ".join(parts)
    if key_names:
        text += "; key: " + ", ".join(key_names[:6])
    return text + "."


class ProvidersRepository(Repository):
    """Universal provider/InfoObject deep-dive backing bw_describe_object."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._texts = TextsRepository(connection, capability, cache)
        self._descriptions = DescriptionService()
        # Provider-name catalogue, loaded once per instance to confirm table -> object readings.
        self._catalog_cache: dict[str, list[str]] | None = None

    # --- entry point ---------------------------------------------------------------------

    def describe(
        self, name: str, object_type: ProviderType | None = None
    ) -> Provider | ObjectNotFound | UnsupportedResult:
        """Resolve a provider/InfoObject by name. Cached (scope ``provider``).

        The cache key includes the requested type, since an explicit type skips auto-detection and
        can therefore resolve differently from a bare name. A not-found is never cached — the object
        may simply not be transported yet.
        """
        key = f"{name.strip()}|{object_type or 'auto'}"
        return self.cached_model(
            "provider",
            key,
            model=Provider,
            build=lambda: self._describe_uncached(name, object_type),
        )

    def _describe_uncached(
        self, name: str, object_type: ProviderType | None = None
    ) -> Provider | ObjectNotFound | UnsupportedResult:
        name = name.strip()
        if object_type is not None:
            return self._describe_typed(name, object_type)

        searched: list[str] = []
        for probe in _AUTODETECT:
            if not all(self.capability.is_available(t) for t in _REQUIRED[probe]):
                continue
            searched.append(probe)
            provider = self._dispatch(name, probe)
            if provider is not None:
                return provider
        if not searched:
            unsupported = self.require(*_ALL_HEADERS)
            if unsupported is not None:
                return unsupported
        return ObjectNotFound(
            name=name,
            searched_types=searched,
            detail=f"'{name}' not found in [{', '.join(searched)}] on {self.capability.bw_release}",
        )

    def _describe_typed(
        self, name: str, object_type: ProviderType
    ) -> Provider | ObjectNotFound | UnsupportedResult:
        probe = (
            "infocube"
            if object_type in ("infocube", "multiprovider", "virtualprovider")
            else object_type
        )
        unsupported = self.require(*_REQUIRED[probe])
        if unsupported is not None:
            return unsupported
        provider = self._dispatch(name, probe)
        if provider is None:
            return ObjectNotFound(
                name=name,
                searched_types=[object_type],
                detail=f"'{name}' not found as {object_type} on {self.capability.bw_release}",
            )
        return provider

    def _dispatch(self, name: str, probe: str) -> Provider | None:
        if probe == "dso":
            return self._build_dso(name)
        if probe == "adso":
            return self._build_adso(name)
        if probe == "infocube":
            return self._build_cube(name)
        if probe == "compositeprovider":
            return self._build_composite(name)
        return self._build_infoobject(name)

    # --- per-type builders (return None when the header row is absent) --------------------

    def _build_dso(self, name: str) -> Provider | None:
        header = self.select(
            self.dialect.build_select(
                columns=["ODSOTYPE", "INFOAREA", "OWNER", "BWAPPL"],
                from_logical="dso_header",
                where=["ODSOBJECT = ?"],
                params=[name],
            )
        )
        if not header:
            return None
        odsotype, info_area, owner, appl = header[0]
        fields = self._dso_fields(name)
        key_names = [f.name for f in fields if f.is_key]
        description = self._describe(
            "dso",
            name,
            info_area=_clean(info_area),
            key_names=key_names,
            field_count=len(fields),
            part_count=0,
            evidence_tables=["dso_header", "dso_field"],
        )
        return Provider(
            name=name,
            object_type="dso",
            subtype=_clean(odsotype),
            active=True,  # RSDODSO has no OBJSTAT; an active-version row means active
            info_area=_clean(info_area),
            owner=_clean(owner),
            application=_clean(appl),
            key_field_names=key_names,
            fields=fields,
            composition_source="none",
            description=description,
            provenance=[self.provenance("dso_header", {"ODSOBJECT": name, "OBJVERS": "A"})],
        )

    def _dso_fields(self, name: str) -> list[ProviderField]:
        rows = self.select(
            self.dialect.build_select(
                columns=["IOBJNM", "POSIT", "KEYFLAG"],
                from_logical="dso_field",
                where=["ODSOBJECT = ?"],
                params=[name],
                order_by=["POSIT"],
            )
        )
        fields: list[ProviderField] = []
        seen: set[str] = set()  # RSDODSOIOBJ repeats an IOBJNM per ODSTABLE; keep one
        for iobjnm, posit, keyflag in rows:
            field_name = str(iobjnm).strip()
            if not field_name or field_name in seen:
                continue
            seen.add(field_name)
            fields.append(
                ProviderField(
                    name=field_name,
                    position=_as_int(posit),
                    is_key=str(keyflag).strip() == "X",
                    role="field",
                    provenance=self.provenance(
                        "dso_field", {"ODSOBJECT": name, "IOBJNM": field_name}
                    ),
                )
            )
        return self._annotate_nav_attribute_fields(fields)

    def _build_adso(self, name: str) -> Provider | None:
        header = self.select(
            self.dialect.build_select(
                columns=["INFOAREA", "OWNER", "BWAPPL"],
                from_logical="adso_header",
                where=["ADSONM = ?"],
                params=[name],
            )
        )
        if not header:
            return None
        info_area, owner, appl = header[0]
        spec = _TEXT_SPECS["adso"]
        field_desc = self._texts.field_texts(spec, name)
        key_names = self._adso_keys(name)
        fields, field_caveats = self._adso_fields(name, field_desc, key_names)
        description = self._describe(
            "adso",
            name,
            info_area=_clean(info_area),
            key_names=key_names,
            field_count=len(fields),
            part_count=0,
            evidence_tables=["adso_header", "adso_text", "dict_columns"],
        )
        return Provider(
            name=name,
            object_type="adso",
            active=True,  # RSOADSO has no OBJSTAT; an active-version row means active
            info_area=_clean(info_area),
            owner=_clean(owner),
            application=_clean(appl),
            key_field_names=key_names,
            fields=fields,
            composition_source="none",
            description=description,
            caveats=field_caveats,
            provenance=[self.provenance("adso_header", {"ADSONM": name, "OBJVERS": "A"})],
        )

    def _adso_fields(
        self, name: str, field_desc: dict[str, str], key_names: list[str]
    ) -> tuple[list[ProviderField], list[str]]:
        """An Advanced DSO's real field list, from its generated active table's dictionary columns.

        Defect D14. This was previously built from ``RSOADSOT`` - the *text* table - as
        "every column that happens to have a description, plus the key fields". Two failures
        at once,
        measured on a production ADSO whose active table has **332** columns:

        * **missing fields.** 15 were reported. A field with no maintained description was invisible
          unless it was part of the semantic key, so the field list was a description inventory
          wearing a field list's name.
        * **fields that are not fields.** ``RSOADSOT.COLNAME`` also holds BW's escape-encoded
          internal identifiers, so entries like ``!23!2F!2F!2F0COUNTRY!2F0COUNTRY``
          (``#///0COUNTRY``
          encoded) and ``!23!2F!2F!2FDATA!C2!A7`` arrived as fields with provenance citing the text
          table. Nothing downstream could tell them from real columns.

        The dictionary is the authority: it carries every column, its position, and ``KEYFLAG`` -
        which was verified to agree exactly with ``RSOADSOKEYFIELDS`` on the same object. Text rows
        now *decorate* that list, so an encoded key simply matches no column and disappears.

        Falls back to the previous reading when the dictionary or the generated table cannot be
        reached, and says so, because a thin field list that admits it is thin beats a silent one.
        """
        caveats: list[str] = []
        active = self._adso_active_table(name)
        if active is None or not self.capability.is_available("dict_columns"):
            caveats.append(
                "field list derived from maintained field texts rather than the generated active "
                "table's dictionary columns, because the dictionary could not be read here; fields "
                "with no description are therefore absent"
            )
            return (
                self._fields_from_texts("adso_text", "ADSONM", name, field_desc, key_names),
                caveats,
            )

        rows = self.select(
            self.dialect.build_select(
                columns=["FIELDNAME", "POSITION", "KEYFLAG"],
                from_logical="dict_columns",
                where=["TABNAME = ?", "TRIM(FIELDNAME) <> ''"],
                params=[active],
                order_by=["POSITION"],
            )
        )
        if not rows:
            caveats.append(
                f"the generated active table {active} carries no dictionary columns, so the field "
                "list falls back to maintained field texts and omits undescribed fields"
            )
            return (
                self._fields_from_texts("adso_text", "ADSONM", name, field_desc, key_names),
                caveats,
            )

        key_set = {k.strip().upper() for k in key_names}
        columns = [
            (str(field).strip(), _as_int(position), str(keyflag).strip().upper() == "X")
            for field, position, keyflag in rows
            if _clean(field)
        ]
        resolved = self._infoobjects_for_columns([column for column, _pos, _key in columns])
        fields = [
            ProviderField(
                name=column,
                position=position,
                # Either source may know: KEYFLAG is the table's own key, RSOADSOKEYFIELDS is the
                # ADSO's declared semantic key. They agreed on the object measured; a union means a
                # disagreement understates neither.
                is_key=is_key or column.upper() in key_set,
                role="field",
                # Looked up by InfoObject *first*. RSOADSOT.COLNAME holds InfoObject names, not
                # column names - 52,949 rows on the reference system read like '0CALDAY' - so a
                # column-keyed join finds nothing and every field comes back undescribed. The column
                # form is still tried, since a release that keys it differently then still resolves.
                description=_first_text(
                    field_desc, resolved.get(column, (None, "none"))[0], column
                ),
                name_layer="hana_column",
                infoobject=resolved.get(column, (None, "none"))[0],
                infoobject_resolution=resolved.get(column, (None, "none"))[1],
                provenance=self.provenance(
                    "dict_columns", {"TABNAME": active, "FIELDNAME": column}
                ),
            )
            for column, position, is_key in columns
        ]
        undescribed = sum(1 for f in fields if f.description is None)
        if undescribed and field_desc and undescribed == len(fields):
            # Rows exist for this object but none of them key to a field. Measured on the reference
            # system: BW writes escape-encoded internal identifiers into COLNAME - '!23!2F...' for
            # '#///...' - and for one production ADSO none of its 10 text rows matched a column of
            # its active table. "Descriptions are unavailable here" and "nobody maintained them" are
            # different answers and only one of them is about the object.
            caveats.append(
                f"{self.physical('adso_text')} holds {len(field_desc)} text row(s) for this "
                "object, none of which key to a field of its active table - BW writes "
                "escape-encoded "
                "internal identifiers there - so per-field descriptions are unavailable on this "
                "release rather than unmaintained"
            )
        elif undescribed:
            caveats.append(
                f"{undescribed} of {len(fields)} fields carry no maintained description in "
                f"{self.physical('adso_text')}; the field itself is still declared by the "
                "dictionary"
            )
        caveats.append(
            "field names are generated table columns, not InfoObject names (name_layer="
            "'hana_column'). The InfoObject is read back off the column by BW's generation "
            "convention and confirmed against the InfoObject catalogue where possible - see "
            "infoobject_resolution; no table on this release declares the mapping."
        )
        return fields, caveats

    def _adso_active_table(self, name: str) -> str | None:
        """The generated active table for an ADSO, using the shared naming convention."""
        for table, role in candidate_tables(name, "adso").items():
            if role == "active":
                return table
        return None

    def _infoobjects_for_columns(self, columns: list[str]) -> dict[str, _Resolution]:
        """``column -> (infoobject, resolution)``, confirmed against the catalogue in one read.

        Reading BW's generation convention backwards, which is a guess until the catalogue agrees -
        hence two outcomes rather than one. The candidate forms were established against the
        reference system rather than assumed:

        * ``/BIC/<NAME>`` -> ``<NAME>``. A customer InfoObject is generated with the ``/BIC/``
          prefix and confirmed in the catalogue without it.
        * ``BILL_NUM`` -> ``0BILL_NUM``. An SAP-delivered InfoObject loses its leading zero.
        * ``/B299/S_IPNUM_CR`` -> itself. Other namespaces are *not* ``/BIC/``: 381 InfoObjects on
          the reference system carry a namespace in their own name, so the column already is the
          name. Prepending a zero here produced ``0/B299/S_IPNUM_CR``, which is not a name of
          anything - the kind of confident nonsense the resolution flag exists to prevent.

        Every plausible form is offered to the catalogue and the first *confirmed* one wins, so a
        column is only reported as inferred when nothing in the catalogue matched any reading of it.
        """
        candidates: dict[str, list[str]] = {}
        for column in columns:
            forms = _infoobject_candidates(column)
            if forms:
                candidates[column] = forms
        if not candidates:
            return {}
        if not self.capability.is_available("infoobject"):
            return {column: (forms[0], "inferred") for column, forms in candidates.items()}

        wanted = sorted({form for forms in candidates.values() for form in forms})
        confirmed: set[str] = set()
        for chunk in [wanted[i : i + _IOBJ_CHUNK] for i in range(0, len(wanted), _IOBJ_CHUNK)]:
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=["IOBJNM"],
                    from_logical="infoobject",
                    where=[f"IOBJNM IN ({placeholders})"],
                    params=list(chunk),
                    order_by=["IOBJNM"],
                )
            )
            confirmed.update(str(r[0]).strip().upper() for r in rows if _clean(r[0]))

        resolved: dict[str, _Resolution] = {}
        for column, forms in candidates.items():
            match = next((form for form in forms if form in confirmed), None)
            resolved[column] = (match, "confirmed") if match else (forms[0], "inferred")
        return resolved

    def _adso_keys(self, name: str) -> list[str]:
        if not self.capability.is_available("adso_keyfields"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["FIELDNM"],
                from_logical="adso_keyfields",
                where=["ADSONM = ?"],
                params=[name],
                order_by=["POSIT"],
            )
        )
        return [str(r[0]).strip() for r in rows if _clean(r[0])]

    def _build_cube(self, name: str) -> Provider | None:
        header = self.select(
            self.dialect.build_select(
                columns=["CUBETYPE", "OBJSTAT", "INFOAREA", "OWNER", "BWAPPL"],
                from_logical="cube_header",
                where=["INFOCUBE = ?"],
                params=[name],
            )
        )
        if not header:
            return None
        cubetype, objstat, info_area, owner, appl = header[0]
        provider_type = classify_cube_type(cubetype)
        fields = self._cube_fields(name)
        parts: list[PartProviderRef] = []
        # Annotated with the alias rather than `str`, which is what let the mismatch through here.
        composition: CompositionSource = "none"
        if provider_type == "multiprovider" and self.capability.is_available("multiprovider_part"):
            parts = self._multiprovider_parts(name)
            composition = "relational"
        description = self._describe(
            provider_type,
            name,
            info_area=_clean(info_area),
            key_names=[],
            field_count=len(fields),
            part_count=len(parts),
            evidence_tables=["cube_header"],
        )
        return Provider(
            name=name,
            object_type=provider_type,
            subtype=_clean(cubetype),
            active=str(objstat).strip() == "ACT",
            info_area=_clean(info_area),
            owner=_clean(owner),
            application=_clean(appl),
            fields=fields,
            part_providers=parts,
            composition_source=composition,
            description=description,
            provenance=[self.provenance("cube_header", {"INFOCUBE": name, "OBJVERS": "A"})],
        )

    def _cube_fields(self, name: str) -> list[ProviderField]:
        rows = self.select(
            self.dialect.build_select(
                columns=["IOBJNM", "POSIT"],
                from_logical="cube_field",
                where=["INFOCUBE = ?"],
                params=[name],
                order_by=["POSIT"],
            )
        )
        fields: list[ProviderField] = []
        seen: set[str] = set()
        for iobjnm, posit in rows:
            field_name = str(iobjnm).strip()
            if not field_name or field_name in seen:
                continue
            seen.add(field_name)
            fields.append(
                ProviderField(
                    name=field_name,
                    position=_as_int(posit),
                    role="field",
                    provenance=self.provenance(
                        "cube_field", {"INFOCUBE": name, "IOBJNM": field_name}
                    ),
                )
            )
        return self._annotate_nav_attribute_fields(fields)

    def _multiprovider_parts(self, name: str) -> list[PartProviderRef]:
        rows = self.select(
            self.dialect.build_select(
                columns=["PARTCUBE", "POSIT"],
                from_logical="multiprovider_part",
                where=["INFOCUBE = ?"],
                params=[name],
                order_by=["POSIT"],
            )
        )
        parts: list[PartProviderRef] = []
        for partcube, posit in rows:
            part_name = str(partcube).strip()
            if not part_name:
                continue
            parts.append(
                PartProviderRef(
                    name=part_name,
                    position=_as_int(posit),
                    provenance=self.provenance(
                        "multiprovider_part", {"INFOCUBE": name, "PARTCUBE": part_name}
                    ),
                )
            )
        return parts

    def _build_composite(self, name: str) -> Provider | None:
        header = self.select(
            self.dialect.build_select(
                columns=["OBJSTAT", "INFOAREA", "OWNER", "BWAPPL"],
                from_logical="composite_header",
                where=["HCPRNM = ?"],
                params=[name],
            )
        )
        if not header:
            return None
        objstat, info_area, owner, appl = header[0]
        spec = _TEXT_SPECS["compositeprovider"]
        field_desc = self._texts.field_texts(spec, name)
        fields = self._fields_from_texts("composite_text", "HCPRNM", name, field_desc, [])
        parts, composition, caveats = self.composite_parts(name)
        description = self._describe(
            "compositeprovider",
            name,
            info_area=_clean(info_area),
            key_names=[],
            field_count=len(fields),
            part_count=len(parts),
            evidence_tables=["composite_header", "composite_text"],
        )
        return Provider(
            name=name,
            object_type="compositeprovider",
            active=str(objstat).strip() == "ACT",
            info_area=_clean(info_area),
            owner=_clean(owner),
            application=_clean(appl),
            fields=fields,
            part_providers=parts,
            composition_source=composition,
            caveats=caveats,
            description=description,
            provenance=[self.provenance("composite_header", {"HCPRNM": name, "OBJVERS": "A"})],
        )

    def _build_infoobject(self, name: str) -> Provider | None:
        header = self.select(
            self.dialect.build_select(
                columns=["IOBJTP", "OBJSTAT", "BWAPPL"],
                from_logical="infoobject",
                where=["IOBJNM = ?"],
                params=[name],
            )
        )
        if not header:
            return None
        iobjtp, objstat, appl = header[0]
        kind = _IOBJTP_TO_KIND.get(str(iobjtp).strip(), "other")
        description = self._describe(
            "infoobject",
            name,
            info_area=None,
            key_names=[],
            field_count=0,
            part_count=0,
            evidence_tables=["infoobject", "infoobject_text"],
        )
        aggregation = self._key_figure_aggregation(name) if kind == "key_figure" else None
        caveats: list[str] = []
        if kind == "key_figure" and aggregation is None:
            caveats.append(
                "this is a key figure but its aggregation could not be read (RSDKYF is "
                "unavailable or holds no active row), so whether its values can be summed is "
                "unknown rather than unrestricted"
            )
        elif aggregation is not None and not aggregation.summable:
            caveats.extend(aggregation.summability_caveats)

        attributes: list[AttributeRef] = []
        if kind == "characteristic":
            attributes, attribute_caveats = self._attributes(name)
            caveats.extend(attribute_caveats)
            restricted = sorted(a.name for a in attributes if a.auth_relevant)
            if restricted:
                caveats.append(
                    "these navigation attributes are authorisation-relevant in their own right, "
                    "so a query drilling down by one returns different rows per user even when "
                    "the characteristic itself is unrestricted: " + ", ".join(restricted)
                )
        return Provider(
            name=name,
            object_type="infoobject",
            subtype=_clean(iobjtp),
            infoobject_kind=kind,
            active=str(objstat).strip() == "ACT",
            application=_clean(appl),
            composition_source="none",
            aggregation=aggregation,
            attributes=attributes,
            caveats=caveats,
            description=description,
            provenance=[self.provenance("infoobject", {"IOBJNM": name, "OBJVERS": "A"})],
        )

    def _attributes(self, name: str) -> tuple[list[AttributeRef], list[str]]:
        """A characteristic's display and navigation attributes, with the inheritance resolved.

        The two tables are keyed differently and it matters. ``RSDBCHATR`` is keyed on the *basic*
        characteristic, so a reference characteristic has no rows of its own and inherits its base's
        attribute list. ``RSDATRNAV`` is keyed on the characteristic itself, so that same inherited
        attribute carries a navigation name belonging to the reference. Keying both on one name is
        the obvious implementation and is wrong in both directions: on the reference system 26% of
        characteristics are references, and 724 of 4,129 navigation attributes exist only under a
        reference name.

        Returns the attributes plus any caveats, so an unreadable table is stated rather than
        rendered as "this characteristic has no attributes".
        """
        caveats: list[str] = []
        if not self.capability.is_available("attribute"):
            return [], [
                f"attributes are not resolved: {self.physical('attribute')} is absent on this "
                "release, so whether this characteristic has attributes is unknown"
            ]

        base = self._basic_characteristic(name)
        if base is None:
            base = name
            caveats.append(
                f"the basic characteristic behind {name} could not be read from "
                f"{self.physical('characteristic')}, so attributes were looked up under its own "
                "name; a reference characteristic's inherited attributes would be missed"
            )

        rows = self.select(
            self.dialect.build_select(
                columns=["ATTRINM", "POSIT", "ATTRITP", "ATRTIMFL", "NODISPINQUERYFL"],
                from_logical="attribute",
                where=["CHABASNM = ?"],
                params=[base],
                order_by=["POSIT", "ATTRINM"],
            )
        )
        if not rows:
            return [], caveats

        nav = self._nav_attributes(name)
        if nav is None:
            caveats.append(
                f"navigation attributes are not resolved: {self.physical('nav_attribute')} is "
                "absent on this release, so an attribute reported as display-only here may in fact "
                "be navigable"
            )
        nav_rows = nav or {}

        names = [str(r[0]).strip() for r in rows]
        descriptions = self._texts.object_texts(_TEXT_SPECS["infoobject"], names)

        attributes: list[AttributeRef] = []
        for attrinm, posit, attritp, timfl, nodisp in rows:
            attribute = str(attrinm).strip()
            if not attribute:
                continue
            nav_row = nav_rows.get(attribute)
            provenance = [self.provenance("attribute", {"CHABASNM": base, "ATTRINM": attribute})]
            if nav_row is not None:
                provenance.append(
                    self.provenance("nav_attribute", {"CHANM": name, "ATTRINM": attribute})
                )
            attributes.append(
                AttributeRef(
                    name=attribute,
                    description=descriptions.get(attribute),
                    kind="navigation" if str(attritp).strip().upper() == "NAV" else "display",
                    position=_as_int(posit),
                    # Domain RSDCNVFL, whose values are '0'/'1' - not the 'X' every neighbouring
                    # flag uses. Measured: 688 attributes are time-dependent on the reference
                    # system, all of which an 'X' test would report as not.
                    time_dependent=str(timfl).strip() == "1",
                    hidden_in_query=str(nodisp).strip().upper() == "X",
                    navigation_name=None if nav_row is None else nav_row[0],
                    navigable=nav_row is not None and nav_row[0] is not None,
                    auth_relevant=nav_row is not None and nav_row[1],
                    text_from_characteristic=nav_row is not None and nav_row[2],
                    transitive=nav_row is not None and nav_row[3],
                    inherited_from=base if base != name else None,
                    provenance=provenance,
                )
            )
        not_exposed = sorted(
            a.name for a in attributes if a.kind == "navigation" and not a.navigable
        )
        if not_exposed:
            caveats.append(
                f"{len(not_exposed)} attributes are navigable on the basic characteristic {base} "
                f"but carry no navigation name under {name}, so a query on {name} cannot drill "
                "down by them: "
                + ", ".join(not_exposed[:_CAVEAT_NAMES])
                + (" ..." if len(not_exposed) > _CAVEAT_NAMES else "")
            )
        return attributes, caveats

    def _annotate_nav_attribute_fields(self, fields: list[ProviderField]) -> list[ProviderField]:
        """Resolve provider fields that are navigation attributes back to what they come from.

        A navigation attribute appears in a provider's field list under its own technical name -
        ``<characteristic>__<attribute>`` in almost every case - and nothing else in the field row
        says the value is not stored there but read from the characteristic's master data. On the
        reference system 2,756 InfoCube field rows are navigation attributes, so a field list
        without this is 2,756 names a reader has to decode by convention.

        One query for the whole field list. The name is matched against stored ``ATRNAVNM`` rather
        than split on ``__``, because the convention holds for 4,127 of 4,129 rows and splitting
        would invent a characteristic for the other two.
        """
        if not fields or not self.capability.is_available("nav_attribute"):
            return fields
        names = sorted({f.name for f in fields})
        rows = self.select(
            self.dialect.build_select(
                columns=["ATRNAVNM", "CHANM", "ATTRINM"],
                from_logical="nav_attribute",
                where=[f"ATRNAVNM IN ({', '.join('?' for _ in names)})"],
                params=names,
            )
        )
        resolved = {
            str(navnm).strip(): (_clean(chanm), _clean(attrinm)) for navnm, chanm, attrinm in rows
        }
        if not resolved:
            return fields
        annotated: list[ProviderField] = []
        for field in fields:
            match = resolved.get(field.name)
            if match is None:
                annotated.append(field)
                continue
            annotated.append(
                field.model_copy(
                    update={
                        "role": "navigation_attribute",
                        "attribute_of": match[0],
                        "attribute_name": match[1],
                    }
                )
            )
        return annotated

    def _basic_characteristic(self, name: str) -> str | None:
        """``RSDCHA.CHABASNM`` for a characteristic: the object its attribute list belongs to."""
        if not self.capability.is_available("characteristic"):
            return None
        rows = self.select(
            self.dialect.build_select(
                columns=["CHABASNM"],
                from_logical="characteristic",
                where=["CHANM = ?"],
                params=[name],
            )
        )
        if not rows:
            return None
        return _clean(rows[0][0]) or name

    def _nav_attributes(self, name: str) -> dict[str, tuple[str | None, bool, bool, bool]] | None:
        """``{ATTRINM: (nav_name, auth_relevant, text_from_characteristic, transitive)}``.

        ``None`` when the table is absent, which is a different statement from an empty mapping.
        """
        if not self.capability.is_available("nav_attribute"):
            return None
        rows = self.select(
            self.dialect.build_select(
                columns=["ATTRINM", "ATRNAVNM", "AUTHRELFL", "TXTFROMCHAFL", "TRANSITIVEFL"],
                from_logical="nav_attribute",
                where=["CHANM = ?"],
                params=[name],
            )
        )
        resolved: dict[str, tuple[str | None, bool, bool, bool]] = {}
        for attrinm, navnm, authfl, txtfl, transfl in rows:
            attribute = str(attrinm).strip()
            if not attribute:
                continue
            resolved[attribute] = (
                _clean(navnm),
                str(authfl).strip().upper() == "X",
                str(txtfl).strip().upper() == "X",
                str(transfl).strip().upper() == "X",
            )
        return resolved

    def _key_figure_aggregation(self, name: str) -> KeyFigureAggregation | None:
        """Read a key figure's aggregation and unit handling from ``RSDKYF``.

        One read covers both questions, because they are the same question: a figure's number is
        only meaningful together with how it combines and what it is denominated in.
        """
        if not self.capability.is_available("keyfigure"):
            return None
        rows = self.select(
            self.dialect.build_select(
                columns=[
                    "KYFTP",
                    "DATATP",
                    "AGGRGEN",
                    "AGGREXC",
                    "AGGRCHA",
                    "NCUMFL",
                    "FIXCUKY",
                    "FIXUNIT",
                    "UNINM",
                    "KYFSEMANTIC",
                ],
                from_logical="keyfigure",
                where=["KYFNM = ?"],
                params=[name],
            )
        )
        if not rows:
            return None
        kyftp, datatp, aggrgen, aggrexc, aggrcha, ncumfl, fixcuky, fixunit, uninm, semantic = rows[
            0
        ]
        return build_key_figure_aggregation(
            key_figure=name,
            kyftp=kyftp,
            datatp=datatp,
            aggrgen=aggrgen,
            aggrexc=aggrexc,
            aggrcha=aggrcha,
            ncumfl=ncumfl,
            fixcuky=fixcuky,
            fixunit=fixunit,
            uninm=uninm,
            semantic=semantic,
            provenance=self.provenance("keyfigure", {"KYFNM": name, "OBJVERS": "A"}),
        )

    # --- shared helpers ------------------------------------------------------------------

    def _fields_from_texts(
        self,
        text_logical: str,
        id_column: str,
        object_id: str,
        field_desc: dict[str, str],
        key_names: list[str],
    ) -> list[ProviderField]:
        """Build fields for HANA-shape objects (ADSO/CompositeProvider) from per-column texts.

        Any key field without a text row is still included so the semantic key is never dropped.
        """
        names = sorted(set(field_desc) | set(key_names))
        key_set = set(key_names)
        return [
            ProviderField(
                name=column,
                description=field_desc.get(column),
                is_key=column in key_set,
                role="field",
                provenance=self.provenance(text_logical, {id_column: object_id, "COLNAME": column}),
            )
            for column in names
        ]

    def _describe(
        self,
        object_type: ProviderType,
        name: str,
        *,
        info_area: str | None,
        key_names: list[str],
        field_count: int,
        part_count: int,
        evidence_tables: list[str],
    ) -> Description:
        spec = _TEXT_SPECS[object_type]
        stored = (
            self._texts.object_text(spec, name)
            if self.capability.is_available(spec.text_logical)
            else None
        )
        summary = _generate_summary(object_type, info_area, key_names, field_count, part_count)
        evidence = [self.physical(t) for t in evidence_tables]
        return self._descriptions.build(
            technical_name=name, stored=stored, generated_summary=summary, evidence=evidence
        )

    # --- CompositeProvider composition ---------------------------------------------------

    def data_bearing_parts(self, name: str) -> tuple[list[str], str]:
        """The parts of a union provider that can actually hold data, or ``([], reason)``.

        Exists because "which object do I check the request ledger on?" has a different answer for a
        union provider than for anything else, and getting that wrong is invisible (D58). A
        MultiProvider and a CompositeProvider hold **no data and no requests of their own** - they
        union their parts at query time - so a currency check aimed at one finds nothing and is
        perfectly entitled to report nothing. The answer looks like a clean bill of health on a
        provider whose parts may be months behind.

        Returns ``(part names, source)`` where source is ``relational`` for a MultiProvider read
        the part table, the composite reader's own label for a CompositeProvider, ``not_union`` when
        the provider holds its own data and needs no expansion, or ``unresolved`` when it is a union
        whose parts could not be read - which is a gap to report, never "it has no parts".
        """
        described = self.describe(name)
        if not isinstance(described, Provider):
            return [], "unresolved"
        if described.object_type not in _UNION_PROVIDER_TYPES:
            return [], "not_union"
        parts = [p.name for p in described.part_providers if p.name]
        if not parts:
            return [], "unresolved"
        return parts, described.composition_source

    def composite_parts(
        self, name: str
    ) -> tuple[list[PartProviderRef], CompositionSource, list[str]]:
        """Resolve a CompositeProvider's part providers.

        Two routes, tried in order:

        1. **Declared model** — the XML BW stores on ``RSOHCPR``, which names each input of each
           view node directly. This is BW's own declaration, so a part resolved here is observed
           rather than inferred, and it is the only route that also yields the field mapping.
        2. **Generated calc view** — every activated CompositeProvider generates a calc view in
           ``_SYS_BIC``; its base tables (from ``SYS.OBJECT_DEPENDENCIES``) resolve back to the part
           providers by BW's table-naming convention. Kept as the fallback for a release or a
           provider whose model cannot be read, and labelled advisory because a naming convention
           can be wrong even when every row was read correctly.

        Returns ``(parts, composition_source, caveats)``. A model that could not be read is reported
        as a finding rather than silently falling through.
        """
        caveats: list[str] = []
        model = self.composite_model(name)
        if model is not None and model.parsed and model.inputs:
            parts = self._parts_from_model(model)
            caveats.extend(model.caveats)
            unknown = sorted(
                {i.alias_kind or "?" for i in model.part_inputs if i.part_kind == "unknown"}
            )
            if unknown:
                caveats.append(
                    "these inputs carry a kind code this release does not decode, so their part "
                    f"type is reported as unknown rather than guessed: {', '.join(unknown)}"
                )
            if parts:
                return parts, "declared_model", caveats
        if model is not None and not model.parsed and model.unparsed_reason:
            caveats.append(
                "the declared CompositeProvider model could not be read, so parts fall back to the "
                f"generated calc view's base tables: {model.unparsed_reason}"
            )

        parts, view_caveats = self._composite_parts_via_calc_view(name)
        caveats.extend(view_caveats)
        if parts:
            if any(p.confidence == "advisory" for p in parts):
                caveats.append(
                    "part providers marked advisory were resolved by table-naming convention "
                    "only (not confirmed against the provider catalogue)"
                )
            return parts, "calc_view", caveats

        caveats.append(
            "part-provider composition could not be derived from the CompositeProvider's stored "
            "model or from a generated calc view; part_providers is empty but the provider almost "
            "certainly has parts (treat as a gap, not as 'no parts')"
        )
        return [], "none", caveats

    def _parts_from_model(self, model: CompositeModel) -> list[PartProviderRef]:
        """The declared inputs as part references, deduplicated and ordered by name.

        A part can appear more than once — the same object joined to itself under two aliases is a
        legitimate model — so the first occurrence wins and the rest are dropped rather than
        producing two identical entries a caller would count twice.
        """
        parts: list[PartProviderRef] = []
        seen: set[str] = set()
        for index, candidate in enumerate(model.part_inputs, start=1):
            if not candidate.part_name or candidate.part_name in seen:
                continue
            seen.add(candidate.part_name)
            parts.append(
                PartProviderRef(
                    name=candidate.part_name,
                    part_type=_COMPOSITE_PART_TYPE.get(candidate.part_kind),
                    position=index,
                    confidence="confirmed",
                    evidence=evidence_for(
                        "composite_part",
                        "declared_model",
                        detail=(
                            f"The CompositeProvider's stored model declares input "
                            f"{candidate.alias!r} as {candidate.entity_ref!r}, which decodes to "
                            f"this object. Not read from a generated table's name."
                        ),
                    ),
                    provenance=self.provenance(
                        "composite_header", {"HCPRNM": model.provider, "OBJVERS": "A"}
                    ),
                )
            )
        parts.sort(key=lambda p: p.name)
        return parts

    # --- the declared CompositeProvider model --------------------------------------------

    def composite_model(self, name: str) -> CompositeModel | None:
        """The CompositeProvider's stored model: its inputs and their field mappings.

        ``None`` when this release has no ``RSOHCPR`` at all, which is a different answer from a
        model that could not be parsed — that comes back as a ``CompositeModel`` with
        ``parsed=False`` and a reason, because "we could not read it" must never look like "it has
        no parts".

        Cached: the model is a large LOB, the parse is pure, and field-level lineage asks for the
        same provider once per field of a query.
        """
        if not self.capability.is_available("composite_header"):
            return None
        return self.cached_model(
            "composite_model",
            name,
            model=CompositeModel,
            build=lambda: self._composite_model_uncached(name),
            cache_when=lambda built: built.parsed,
        )

    def _composite_model_uncached(self, name: str) -> CompositeModel:
        provenance = self.provenance("composite_header", {"HCPRNM": name, "OBJVERS": "A"})
        base = CompositeModel(provider=name, provenance=provenance)
        sizes = self._composite_definition_sizes(name)
        if sizes is None:
            base.unparsed_reason = (
                "neither XML_UI nor XML_DEF could be read on RSOHCPR for this release, so the "
                "declared composition is unavailable here"
            )
            return base
        # XML_UI first, deliberately. It is the column populated on the measured release (108 of 108
        # active CompositeProviders, against 0 for XML_DEF), and reading the empty one first is how
        # the declared composition came to look absent.
        for column in ("XML_UI", "XML_DEF"):
            size = sizes.get(column, 0)
            if size <= 0:
                continue
            if size > MAX_DEFINITION_BYTES:
                base.unparsed_reason = (
                    f"the stored model is {size:,} bytes in {column}, above the "
                    f"{MAX_DEFINITION_BYTES:,} bound this server fetches; it was not read, so "
                    "nothing here is a statement about its composition"
                )
                base.completeness = bounded(
                    "row_cap", scope="definition_bytes", limit=MAX_DEFINITION_BYTES
                )
                base.caveats.append(f"model not read: {size:,} bytes exceeds the bound")
                return base
            definition = self._composite_definition_body(name, column)
            if definition is None:
                continue
            try:
                parsed = parse_composite_model(definition)
            except CompositeParseError as exc:
                base.unparsed_reason = exc.reason
                return base
            return self._to_composite_model(base, parsed, column=column)
        base.unparsed_reason = (
            "RSOHCPR holds no stored model for this CompositeProvider in either XML_UI or XML_DEF, "
            "so its declared composition is unavailable; parts can still be resolved from the "
            "generated calc view's base tables, which is a naming-convention reading"
        )
        return base

    def _composite_definition_sizes(self, name: str) -> dict[str, int] | None:
        """Byte length of each model column, without fetching either LOB.

        Read first and always: the size is what decides whether the definition can be fetched, and
        asking afterwards would mean the LOB had already crossed the wire. ``None`` when neither
        column exists on this release — which is a capability fact, not an empty model.
        """
        sizes: dict[str, int] = {}
        for column in ("XML_UI", "XML_DEF"):
            try:
                rows = self.select(
                    self.dialect.build_select(
                        columns=[f"LENGTH({column})"],
                        from_logical="composite_header",
                        where=["HCPRNM = ?"],
                        params=[name],
                    )
                )
            except Exception:
                continue  # column absent on this release; the other one may still be there
            if rows and rows[0][0] is not None:
                try:
                    sizes[column] = int(rows[0][0])
                except (TypeError, ValueError):
                    continue
            elif rows:
                sizes[column] = 0
        return sizes or None

    def _composite_definition_body(self, name: str, column: str) -> str | None:
        """Fetch and decode one model column. ``None`` when it cannot be read as text."""
        try:
            rows = self.select(
                self.dialect.build_select(
                    columns=[column],
                    from_logical="composite_header",
                    where=["HCPRNM = ?"],
                    params=[name],
                )
            )
        except Exception:
            return None
        if not rows or rows[0][0] is None:
            return None
        raw = rows[0][0]
        if isinstance(raw, str):
            return raw
        try:
            # errors="replace" rather than strict: a single undecodable byte in a 273 KB model must
            # not discard the whole composition, and the parser only reads ASCII names and codes.
            return bytes(raw).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            return None

    def _to_composite_model(
        self, base: CompositeModel, parsed: Any, *, column: str
    ) -> CompositeModel:
        """Map the parser's carrier onto the model, keeping every count exact."""
        base.parsed = True
        base.source_column = "XML_UI" if column == "XML_UI" else "XML_DEF"
        base.node_type = parsed.node_type
        base.node_name = parsed.node_name
        base.nodes = [CompositeViewNode(**node) for node in parsed.nodes]
        base.schema_version = parsed.root.get("schema_version")
        base.with_hana_model = bool(parsed.root.get("with_hana_model"))
        base.element_count = parsed.element_count
        base.completeness = Completeness(
            bounds=[
                BoundHit(bound="row_cap", scope=scope, limit=limit)
                for scope, limit in parsed.bounds
            ]
        )
        base.input_count = len(parsed.inputs)
        for entry in parsed.inputs:
            base.inputs.append(
                CompositeInput(
                    alias=entry["alias"],
                    node_name=entry["node_name"],
                    part_name=entry["part_name"],
                    part_kind=entry["part_kind"],
                    alias_kind=entry["alias_kind"],
                    entity_ref=entry["entity_ref"],
                    internal_node=entry["internal_node"],
                    runtime_view_name=entry["runtime_view_name"],
                    select_all=entry["select_all"],
                    mappings=[CompositeFieldMapping(**m) for m in entry["mappings"]],
                    mapping_count=entry["mapping_count"],
                    evidence=composite_mapping_evidence(entry["part_kind"]),
                )
            )
        # Only inputs that name neither an object nor an internal node are a gap. An input reading
        # another node of the same model is a fact about the model, not a missing part, and counting
        # it as one reported 40 non-existent objects on the measured system.
        nameless = [i.alias for i in base.inputs if not i.part_name and not i.internal_node]
        if nameless:
            base.caveats.append(
                "these inputs name no object this parser could decode, so their part is unresolved "
                f"rather than absent: {', '.join(sorted(nameless)[:8])}"
            )
        for hit in base.completeness.bounds:
            base.caveats.append(
                f"the {hit.scope} list stopped at this reader's bound of {hit.limit}, so it is a "
                "deterministic prefix rather than the whole set"
            )
        return base

    def _composite_parts_via_calc_view(self, name: str) -> tuple[list[PartProviderRef], list[str]]:
        """Resolve parts from the base tables of the CompositeProvider's generated calc view."""
        if not (
            self.capability.is_available("hana_views")
            and self.capability.is_available("object_dependencies")
        ):
            return [], [
                "HANA catalog views are unavailable, so the calc-view route to part providers "
                "could not be used"
            ]
        view = self._generated_calc_view(name)
        if view is None:
            return [], [f"no generated calc view was found for CompositeProvider {name}"]
        return self.providers_under_calc_view(view), []

    def providers_under_calc_view(self, view: str) -> list[PartProviderRef]:
        """The BW objects a calculation view sits on.

        Two routes, tried in order of what they establish rather than blended:

        1. **Direct dependencies.** What the view itself reads: BW's generated per-provider view
           (``system-local.bw.bw2hana/<OBJECT>``, parsed and type-confirmed) or a ``/BIC/`` table
           read outright. If anything is found here it *is* the answer, because these are the
           objects the view names.
        2. **Transitive ``/BIC/`` tables**, used only when the first route finds nothing. BW layers
           intermediate views between a generated view and a provider's active table, so for a
           BW-generated view the provider table is reachable only transitively.

        Route 1 did not exist, and that is defect D47. Route 2 alone resolves a
        CompositeProvider base to *its parts' tables* - the CP itself has no table - so a
        CompositeProvider a view reads disappeared from the graph and the ADSOs two hops beneath
        it were reported as the view's own bases. On the measured system that turned a four-layer
        path into a three-layer one and attributed a provider's content to the wrong object.
        Preferring the direct route also stops the transitive one reporting a two-hop reach as a
        direct read.

        Public because the lineage walk needs it for a calc view it reached through a declared
        CompositeProvider input, not only for the view BW generated for a provider.
        """
        if not (
            self.capability.is_available("hana_views")
            and self.capability.is_available("object_dependencies")
        ):
            return []
        direct = self._calc_view_direct_providers(view)
        if direct:
            return direct
        return self._calc_view_transitive_providers(view)

    def _calc_view_direct_providers(self, view: str) -> list[PartProviderRef]:
        """Objects the view reads outright: generated per-provider views, or ``/BIC/`` tables.

        Neither schema nor object type is constrained in SQL, because the two shapes differ in
        both: a generated provider view is a VIEW in ``_SYS_BIC``, a ``/BIC/`` table is a TABLE in
        the ABAP schema. Filtering to either one is what hid the other.
        """
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT BASE_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENT_OBJECT_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                    ],
                    params=[_CALC_SCHEMA, view, _DIRECT_DEPENDENCY],
                    order_by=["BASE_OBJECT_NAME"],
                ),
                limit=_MAX_PART_TABLES,
            )
        )
        names = [str(name).strip() for (name,) in rows if str(name).strip()]
        if not names:
            return []

        # Generated per-provider views, type-confirmed against the header tables in one batch.
        parsed = {
            name: provider
            for name in names
            if name != view and (provider := provider_from_calc_view(name)) is not None
        }
        kinds = self.confirm_provider_kinds(sorted(set(parsed.values()))) if parsed else {}
        catalog = self.provider_catalog()
        parts: list[PartProviderRef] = []
        seen: set[str] = set()
        for name, provider in parsed.items():
            kind = kinds.get(provider)
            if kind is None:
                continue  # parses, but no header row carries it: not asserted as a provider
            if provider in seen:
                continue
            seen.add(provider)
            parts.append(
                PartProviderRef(
                    name=provider,
                    part_type=kind,
                    via_table=name,
                    confidence="confirmed",
                    # Explicit, because PartProviderRef's default sentence describes the
                    # generated-*table* route and would misdescribe this one.
                    evidence=evidence_for(
                        "calc_view_base",
                        "generated_provider_view",
                        detail=(
                            f"The view reads {name}, BW's generated view for {provider}, whose "
                            f"type was confirmed as {kind} against the provider catalogue."
                        ),
                    ),
                    provenance=self.provenance(
                        "object_dependencies",
                        {"DEPENDENT_OBJECT_NAME": view, "BASE_OBJECT_NAME": name},
                    ),
                )
            )

        # Tables the view reads outright, resolved the same way as the transitive route.
        for name in names:
            if name in parsed:
                continue
            resolved = resolve_table(name, catalog)
            if not resolved.is_part_provider_candidate or resolved.object_name is None:
                continue
            if resolved.object_name in seen:
                continue
            seen.add(resolved.object_name)
            parts.append(
                PartProviderRef(
                    name=resolved.object_name,
                    part_type=_KIND_TO_PROVIDER_TYPE.get(resolved.kind),
                    via_table=name,
                    confidence=resolved.confidence,
                    provenance=self.provenance(
                        "object_dependencies",
                        {"DEPENDENT_OBJECT_NAME": view, "BASE_OBJECT_NAME": name},
                    ),
                )
            )
        parts.sort(key=lambda p: p.name)
        return parts

    def confirm_provider_kinds(self, names: list[str]) -> dict[str, ProviderType]:
        """Confirm provider names against the header tables, returning name -> provider type.

        Batched, most specific header first, and it covers ``composite_header`` - which
        :meth:`provider_catalog` does not, because that one serves table-name resolution and a
        CompositeProvider has no generated table to resolve. A name no header row carries is
        absent from the result rather than defaulted, so a caller cannot mistake "not confirmed"
        for a kind.
        """
        remaining = [n for n in dict.fromkeys(names) if n]
        kinds: dict[str, ProviderType] = {}
        for logical, id_column, kind in _PROVIDER_HEADER_SOURCES:
            if not remaining or not self.capability.is_available(logical):
                continue
            is_cube = logical == "cube_header"
            placeholders = ", ".join("?" for _ in remaining)
            rows = self.select(
                self.dialect.build_select(
                    columns=[id_column, "CUBETYPE"] if is_cube else [id_column],
                    from_logical=logical,
                    where=[f"{id_column} IN ({placeholders})"],
                    params=list(remaining),
                    order_by=[id_column],
                )
            )
            for row in rows:
                name = str(row[0]).strip()
                if not name or name in kinds:
                    continue
                kinds[name] = classify_cube_type(row[1]) if is_cube else kind
            remaining = [n for n in remaining if n not in kinds]
        return kinds

    def _calc_view_transitive_providers(self, view: str) -> list[PartProviderRef]:
        """Provider tables reachable transitively, for views whose bases are BW-generated chains."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT BASE_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENT_OBJECT_NAME = ?",
                        "BASE_SCHEMA_NAME = ?",
                        "BASE_OBJECT_TYPE = ?",
                        "DEPENDENCY_TYPE = ?",
                    ],
                    params=[
                        _CALC_SCHEMA,
                        view,
                        self.capability.abap_schema,
                        "TABLE",
                        _TRANSITIVE_DEPENDENCY,
                    ],
                    order_by=["BASE_OBJECT_NAME"],
                ),
                limit=_MAX_PART_TABLES,
            )
        )
        catalog = self.provider_catalog()
        parts: list[PartProviderRef] = []
        seen: set[str] = set()
        for (base_table,) in rows:
            table = str(base_table).strip()
            if not table:
                continue
            resolved = resolve_table(table, catalog)
            if not resolved.is_part_provider_candidate or resolved.object_name is None:
                continue  # master-data side tables and unreadable names are not part providers
            if resolved.object_name in seen:
                continue
            seen.add(resolved.object_name)
            parts.append(
                PartProviderRef(
                    name=resolved.object_name,
                    part_type=_KIND_TO_PROVIDER_TYPE.get(resolved.kind),
                    via_table=table,
                    confidence=resolved.confidence,
                    provenance=self.provenance(
                        "object_dependencies",
                        {"DEPENDENT_OBJECT_NAME": view, "BASE_OBJECT_NAME": table},
                    ),
                )
            )
        parts.sort(key=lambda p: p.name)
        return parts

    def _generated_calc_view(self, provider: str) -> str | None:
        """Find the ``_SYS_BIC`` calc view BW generated for a provider (shortest match wins)."""
        for pattern in calc_view_patterns(provider):
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["VIEW_NAME"],
                        from_logical="hana_views",
                        where=["SCHEMA_NAME = ?", "VIEW_NAME LIKE ?"],
                        params=[_CALC_SCHEMA, pattern],
                        order_by=["LENGTH(VIEW_NAME)", "VIEW_NAME"],
                    ),
                    limit=5,
                )
            )
            for (view_name,) in rows:
                candidate = str(view_name).strip()
                if candidate and not is_hierarchy_view(candidate):
                    return candidate
        return None

    def provider_catalog(self) -> dict[str, list[str]]:
        """Known object names per kind, used to confirm table -> object readings.

        Also the candidate set for consumer analysis, which needs "every persisted provider" rather
        than one named object. Cached per repository instance.
        """
        if self._catalog_cache is not None:
            return self._catalog_cache
        catalog: dict[str, list[str]] = {}
        for kind, logical, column in (
            ("dso", "dso_header", "ODSOBJECT"),
            ("adso", "adso_header", "ADSONM"),
            ("infocube", "cube_header", "INFOCUBE"),
        ):
            if not self.capability.is_available(logical):
                continue
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[column], from_logical=logical, order_by=[column]
                    ),
                    limit=_MAX_CATALOG,
                )
            )
            catalog[kind] = [str(r[0]).strip() for r in rows if str(r[0]).strip()]
        self._catalog_cache = catalog
        return catalog
