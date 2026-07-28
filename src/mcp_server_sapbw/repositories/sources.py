"""Source-system topology and extractor-enhancement inventory.

**Topology.** The ground truth for "what feeds this warehouse" is the set of logical systems the
DataSources actually extract from (``RSDS.LOGSYS``), not the registry: a registry entry can exist
for a system nothing reads from, and - more importantly - a DataSource can reference a logical
system the registry does not know, which is what a system copy without BDLS looks like. Both sides
are read and compared. ``RSBASIDOC.SLOGSYS`` is the correct join column (verified live: it matched
every DataSource logical system, where ``RLOGSYS`` matched one and ``RSSOURSYSTEM`` under half).
``RSBASIDOC`` has no ``OBJVERS``; it carries ``OBJSTAT``.

**Enhancement.** Customer-namespace fields in the extract structure (``RSDSSEGFD``) are hard
evidence that a DataSource was enhanced, joined to its delta method and extractor program. The
source projects' approach of also inferring risk from a DataSource *name* prefix is deliberately not
carried over: a name is not evidence, and presenting a naming guess beside metadata-derived facts is
exactly the conflation this server avoids.

**Decode honesty.** ``SRCTYPE`` is decoded from the dictionary domain where it documents a value.
The live data also contains codes the domain omits, so those get a widely-used conventional reading
labelled ``advisory`` rather than being asserted with dictionary authority (mission Rule 2).
"""

from __future__ import annotations

from typing import Any, Literal

from ..models.provenance import UnsupportedResult
from ..models.sources import (
    DataSourceEnhancement,
    EnhancementInventory,
    SourceSystem,
    SourceSystemKind,
    SourceTopology,
)
from .base import Repository

# SRCTYPE codes the dictionary domain RSSRCTYPE documents on the reference release.
_DICTIONARY_KINDS: dict[str, SourceSystemKind] = {
    "3": "sap_r3",
    "B": "staging_bapi",
    "F": "flat_file",
}
# Codes that occur in the data but are absent from the domain. Conventional readings, labelled
# advisory so they are never mistaken for a dictionary-backed decode.
_ADVISORY_KINDS: dict[str, SourceSystemKind] = {
    "D": "bw_system",
    "M": "self",
    "G": "db_connect",
    "H": "hana_local",
    "O": "odp",
}

# RSDS.TYPE (domain RSDS_REQUTYPE) - what the DataSource supplies.
_REQUEST_TYPES: dict[str, str] = {
    "D": "transaction data",
    "H": "hierarchies",
    "M": "master data attributes",
    "S": "segmented data",
    "T": "master data text",
}

_CUSTOMER_NAMESPACE_PREFIXES = ("Z", "Y")
_MAX_SYSTEMS = 200
_MAX_ENHANCED = 100
_MAX_FIELDS_PER_DS = 40


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class SourcesRepository(Repository):
    """Which systems feed BW, and which DataSources carry enhancements."""

    # --- topology --------------------------------------------------------------------------

    def get_topology(self) -> SourceTopology | UnsupportedResult:
        unsupported = self.require("datasource")
        if unsupported is not None:
            return unsupported

        usage = self._logical_system_usage()
        registry = self._registry()
        caveats: list[str] = []
        systems: list[SourceSystem] = []

        for logsys, count in sorted(usage.items(), key=lambda item: (-item[1], item[0])):
            entry = registry.get(logsys)
            code = entry[0] if entry else None
            kind, confidence = _decode_kind(code)
            systems.append(
                SourceSystem(
                    logical_system=logsys,
                    kind=kind,
                    kind_code=code,
                    kind_confidence=confidence,
                    registered=entry is not None,
                    active=entry[1] if entry else None,
                    datasource_count=count,
                    provenance=self.provenance("datasource", {"LOGSYS": logsys}),
                )
            )

        unregistered = [s for s in systems if not s.registered]
        if unregistered:
            caveats.append(
                f"{len(unregistered)} logical system(s) are referenced by DataSources but absent "
                "from the source-system registry. This is the usual signature of a system copy "
                "where BDLS was not run, or of a source system that has been decommissioned "
                "without cleaning up its DataSources"
            )
        if not self.capability.is_available("source_system"):
            caveats.append(
                "the source-system registry (RSBASIDOC) is unavailable on this release, so system "
                "kinds could not be resolved and registration could not be checked"
            )
        advisory = [s for s in systems if s.kind_code and s.kind_confidence == "advisory"]
        if advisory:
            caveats.append(
                f"{len(advisory)} system(s) carry a SRCTYPE code the ABAP dictionary domain does "
                "not document; their kind is a conventional reading marked advisory, not a "
                "dictionary-backed decode"
            )
        return SourceTopology(
            systems=systems,
            unregistered_count=len(unregistered),
            total_datasources=sum(usage.values()),
            caveats=caveats,
        )

    def _logical_system_usage(self) -> dict[str, int]:
        """Logical systems the DataSources actually extract from, with how many use each."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["LOGSYS", "COUNT(DISTINCT DATASOURCE)"],
                    from_logical="datasource",
                    where=["LOGSYS <> ''"],
                    group_by=["LOGSYS"],
                ),
                limit=_MAX_SYSTEMS,
            )
        )
        usage: dict[str, int] = {}
        for logsys, count in rows:
            name = _clean(logsys)
            if name:
                usage[name] = _as_int(count)
        return usage

    def _registry(self) -> dict[str, tuple[str | None, bool]]:
        """``{logical_system: (srctype, active)}`` from the source-system registry."""
        if not self.capability.is_available("source_system"):
            return {}
        # RSBASIDOC has OBJSTAT, not OBJVERS: compare_versions keeps the dialect from injecting one.
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["SLOGSYS", "SRCTYPE", "OBJSTAT"],
                    from_logical="source_system",
                    compare_versions=True,
                ),
                limit=_MAX_SYSTEMS,
            )
        )
        out: dict[str, tuple[str | None, bool]] = {}
        for slogsys, srctype, objstat in rows:
            name = _clean(slogsys)
            if name:
                out[name] = (_clean(srctype), (_clean(objstat) or "").upper() == "ACT")
        return out

    # --- enhancement inventory -------------------------------------------------------------

    def enhancement_inventory(self, limit: int = 50) -> EnhancementInventory | UnsupportedResult:
        unsupported = self.require("datasource_field")
        if unsupported is not None:
            return unsupported

        counts = self._customer_field_counts()
        total = self._datasource_total()
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        truncated = len(ordered) > limit
        selected = [name for name, _ in ordered[:limit]]

        detail = self._datasource_detail(selected)
        extractors = self._extractor_detail(selected)
        fields = self._customer_fields(selected)

        enhanced: list[DataSourceEnhancement] = []
        for name in selected:
            info = detail.get(name, {})
            extractor = extractors.get(name, {})
            enhanced.append(
                DataSourceEnhancement(
                    datasource=name,
                    logical_system=info.get("logsys"),
                    delta_method=info.get("delta") or extractor.get("delta"),
                    request_type=_REQUEST_TYPES.get(info.get("type") or ""),
                    application=info.get("appl"),
                    extractor=extractor.get("extractor"),
                    extraction_method=extractor.get("exmethod"),
                    customer_field_count=counts[name],
                    customer_fields=fields.get(name, [])[:_MAX_FIELDS_PER_DS],
                    provenance=[
                        self.provenance("datasource_field", {"DATASOURCE": name, "OBJVERS": "A"}),
                        self.provenance("datasource", {"DATASOURCE": name, "OBJVERS": "A"}),
                    ],
                )
            )

        caveats = [
            "Customer-namespace fields in the extract structure are metadata-confirmed evidence "
            "that an enhancement exists. They do not reveal what the exit code does, which tables "
            "it reads, or whether it performs per-record SELECTs.",
            "The enhancement logic lives in the source system's ABAP, which is not reachable over "
            "the BW database connection.",
        ]
        if not self.capability.is_available("extractor"):
            caveats.append(
                "ROOSOURCE is unavailable, so the extractor program and extraction method could "
                "not be resolved"
            )
        elif selected and not extractors:
            caveats.append(
                "ROOSOURCE holds no entry for any of the DataSources reported here, so their "
                "extractor program and extraction method are unknown. BW only carries replicated "
                "extractor definitions for a subset of DataSources; the rest are defined solely in "
                "the source system"
            )
        return EnhancementInventory(
            enhanced=enhanced,
            enhanced_count=len(ordered),
            total_datasources=total,
            truncated=truncated,
            connector_required="ECC",
            caveats=caveats,
        )

    def _customer_field_counts(self) -> dict[str, int]:
        conditions = " OR ".join(
            f"FIELDNM LIKE '{prefix}%'" for prefix in _CUSTOMER_NAMESPACE_PREFIXES
        )
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DATASOURCE", "COUNT(*)"],
                    from_logical="datasource_field",
                    where=[f"({conditions})"],
                    group_by=["DATASOURCE"],
                ),
                limit=_MAX_ENHANCED * 4,
            )
        )
        return {name: _as_int(row[1]) for row in rows if (name := _clean(row[0]))}

    def _customer_fields(self, names: list[str]) -> dict[str, list[str]]:
        if not names:
            return {}
        conditions = " OR ".join(
            f"FIELDNM LIKE '{prefix}%'" for prefix in _CUSTOMER_NAMESPACE_PREFIXES
        )
        placeholders = ", ".join("?" for _ in names)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DATASOURCE", "FIELDNM"],
                    from_logical="datasource_field",
                    where=[f"DATASOURCE IN ({placeholders})", f"({conditions})"],
                    params=list(names),
                    order_by=["DATASOURCE", "POSIT"],
                ),
                limit=_MAX_ENHANCED * _MAX_FIELDS_PER_DS,
            )
        )
        out: dict[str, list[str]] = {}
        for datasource, field in rows:
            key, value = _clean(datasource), _clean(field)
            if key and value:
                out.setdefault(key, []).append(value)
        return out

    def _datasource_total(self) -> int:
        base = self.dialect.build_select(
            columns=["DISTINCT DATASOURCE"], from_logical="datasource_field"
        )
        rows = self.select(self.dialect.count_query(base))
        return _as_int(rows[0][0]) if rows else 0

    def _datasource_detail(self, names: list[str]) -> dict[str, dict[str, str | None]]:
        if not names or not self.capability.is_available("datasource"):
            return {}
        placeholders = ", ".join("?" for _ in names)
        rows = self.select(
            self.dialect.build_select(
                columns=["DATASOURCE", "LOGSYS", "TYPE", "DELTA", "APPLNM"],
                from_logical="datasource",
                where=[f"DATASOURCE IN ({placeholders})"],
                params=list(names),
            )
        )
        out: dict[str, dict[str, str | None]] = {}
        for datasource, logsys, req_type, delta, appl in rows:
            key = _clean(datasource)
            if key and key not in out:
                out[key] = {
                    "logsys": _clean(logsys),
                    "type": _clean(req_type),
                    "delta": _clean(delta),
                    "appl": _clean(appl),
                }
        return out

    def _extractor_detail(self, names: list[str]) -> dict[str, dict[str, str | None]]:
        if not names or not self.capability.is_available("extractor"):
            return {}
        placeholders = ", ".join("?" for _ in names)
        try:
            rows = self.select(
                self.dialect.build_select(
                    columns=["OLTPSOURCE", "DELTA", "EXTRACTOR", "EXMETHOD"],
                    from_logical="extractor",
                    where=[f"OLTPSOURCE IN ({placeholders})", "OBJVERS = 'A'"],
                    params=list(names),
                )
            )
        except Exception:
            return {}  # column names vary by release; degrade rather than fail the inventory
        out: dict[str, dict[str, str | None]] = {}
        for oltp, delta, extractor, exmethod in rows:
            key = _clean(oltp)
            if key and key not in out:
                out[key] = {
                    "delta": _clean(delta),
                    "extractor": _clean(extractor),
                    "exmethod": _clean(exmethod),
                }
        return out


def _decode_kind(code: str | None) -> tuple[SourceSystemKind, Literal["dictionary", "advisory"]]:
    """Decode a SRCTYPE, saying whether the dictionary backs the reading."""
    if code is None:
        return "unknown", "advisory"
    upper = code.upper()
    if upper in _DICTIONARY_KINDS:
        return _DICTIONARY_KINDS[upper], "dictionary"
    if upper in _ADVISORY_KINDS:
        return _ADVISORY_KINDS[upper], "advisory"
    return "unknown", "advisory"
