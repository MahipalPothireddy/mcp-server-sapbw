"""Provider repository (B4): the universal InfoProvider / InfoObject deep-dive.

One entry point, :meth:`ProvidersRepository.describe`, resolves any object by name across every
object-model variant present on the connected release (classic DSO, advanced DSO, InfoCube /
MultiProvider / virtual provider discriminated by RSDCUBE.CUBETYPE, CompositeProvider, InfoObject),
lists its fields, resolves MultiProvider parts, and attaches a labelled description (stored or
generated). Each variant is capability-gated: an absent table yields ``UnsupportedResult`` rather
than a guess. Objects whose tables exist but that are not found yield ``ObjectNotFound``.

CompositeProvider part-provider composition lives in RSOHCPR.XML_DEF (XML) with no relational part
table; parsing it is deferred to the lineage build (B6), so ``part_providers`` is empty for a
CompositeProvider here and a caveat says exactly that (never implying it has no parts).
"""

from __future__ import annotations

from typing import Any

from ..models.description import Description
from ..models.provenance import UnsupportedResult
from ..models.providers import (
    InfoObjectKind,
    ObjectNotFound,
    PartProviderRef,
    Provider,
    ProviderField,
    ProviderType,
)
from ..services.descriptions import DescriptionService
from .base import Repository
from .texts import TextsRepository, TextTableSpec

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

_CUBETYPE_TO_PROVIDER: dict[str, ProviderType] = {
    "B": "infocube",
    "M": "multiprovider",
    "V": "virtualprovider",
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

    # --- entry point ---------------------------------------------------------------------

    def describe(
        self, name: str, object_type: ProviderType | None = None
    ) -> Provider | ObjectNotFound | UnsupportedResult:
        """Resolve a provider/InfoObject by name (auto-detecting the type unless one is given)."""
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
        return fields

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
        fields = self._fields_from_texts("adso_text", "ADSONM", name, field_desc, key_names)
        description = self._describe(
            "adso",
            name,
            info_area=_clean(info_area),
            key_names=key_names,
            field_count=len(fields),
            part_count=0,
            evidence_tables=["adso_header", "adso_text"],
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
            provenance=[self.provenance("adso_header", {"ADSONM": name, "OBJVERS": "A"})],
        )

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
        provider_type = _CUBETYPE_TO_PROVIDER.get(str(cubetype).strip(), "infocube")
        fields = self._cube_fields(name)
        parts: list[PartProviderRef] = []
        composition: str = "none"
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
            composition_source=composition,  # type: ignore[arg-type]
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
        return fields

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
        description = self._describe(
            "compositeprovider",
            name,
            info_area=_clean(info_area),
            key_names=[],
            field_count=len(fields),
            part_count=0,
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
            part_providers=[],
            composition_source="none",
            caveats=[
                "part-provider composition is stored in RSOHCPR.XML_DEF (XML) and is not yet "
                "parsed; part_providers is empty here (deferred to the lineage build, B6)"
            ],
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
        return Provider(
            name=name,
            object_type="infoobject",
            subtype=_clean(iobjtp),
            infoobject_kind=kind,
            active=str(objstat).strip() == "ACT",
            application=_clean(appl),
            composition_source="none",
            caveats=[
                "attributes and navigation attributes are not resolved in this build "
                "(RSDBCHATR / RSDATRNAV)"
            ],
            description=description,
            provenance=[self.provenance("infoobject", {"IOBJNM": name, "OBJVERS": "A"})],
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
