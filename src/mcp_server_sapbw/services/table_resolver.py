"""Physical-table <-> BW-object resolution, and generated calc-view naming.

BW generates one or more physical HANA tables per provider, following a naming convention that
encodes the provider type and the table's role:

===============  ======================================  =====================================
Provider         Generated table(s)                      Role
===============  ======================================  =====================================
classic DSO      ``<ns>A<name>00``                       active data
Advanced DSO     ``<ns>A<name>1`` / ``2`` / ``3``        inbound / active / changelog
InfoCube         ``<ns>F<name>`` / ``<ns>E<name>``       F-fact (uncompressed) / E-fact
InfoObject       ``<ns>P<name>``                         master-data attributes
===============  ======================================  =====================================

``<ns>`` is normally ``/BIC/`` (customer) or ``/BI0/`` (SAP), but a *namespaced* provider such as
``/ABC/D_STOCK`` generates tables under its own namespace, and BW's generated objects can also land
in ``/B1H/``.

**The ``/BI0/`` prefix rule.** An SAP-delivered object's name starts with ``0``, and the ``/BI0/``
namespace already encodes "SAP", so the ``0`` is dropped from the generated table name: InfoObject
``0MATNR`` has master-data tables ``/BI0/M<NAME>`` and ``/BI0/P<NAME>``, not ``/BI0/M0<NAME>``.
Resolution in either direction must add or remove that ``0``. Measured on the reference system: of
400 sampled ``/BI0/P*`` tables, 400 matched ``0`` + name and **none** matched the bare name.

The ``S``/``T``/``X``/``Y``/``Q``/``M``/``H``/``K`` table classes are master-data SID, text,
attribute-SID and hierarchy tables — they are *not* part providers and must not be mistaken for one.

Resolution in the reverse direction (table -> object) is **name-based and therefore advisory**
unless the candidate is confirmed against a catalogue of known object names; ``resolve_table``
reports which happened via :attr:`ResolvedTable.confidence`.

Pure functions only — no database access, no I/O — so the convention is testable offline.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

# Role a generated table plays for its provider.
TableRole = Literal[
    "active", "inbound", "changelog", "fact_f", "fact_e", "master_attr", "master_other"
]

# BW object kinds this module can resolve to (aligned with the provider/lineage type vocabularies).
ResolvedKind = Literal["dso", "adso", "infocube", "infoobject", "unknown"]

# Table classes that are master-data side tables, never part providers.
_MASTER_DATA_CLASSES = frozenset({"P", "Q", "S", "T", "X", "Y", "M", "H", "K", "I", "J"})

# A namespaced name splits into exactly these two parts: the namespace and the local name.
_NAMESPACE_PARTS = 2
# Length of the classic-DSO active-table suffix ("00").
_DSO_SUFFIX_LEN = 2

# ADSO table-class suffix -> role.
_ADSO_SUFFIX_ROLES: dict[str, TableRole] = {"1": "inbound", "2": "active", "3": "changelog"}

# Namespaces a generated BW table can live in when it is not the provider's own namespace.
_DEFAULT_NAMESPACE = "/BIC/"
# SAP-delivered objects live here, and their leading "0" is dropped from the table name.
_SAP_NAMESPACE = "/BI0/"
_GENERATED_NAMESPACES = ("/BIC/", "/BI0/", "/B1H/")

# Generated CompositeProvider calc views live under this package in _SYS_BIC.
CALC_VIEW_PACKAGE = "system-local.bw.bw2hana"
# Hierarchy runtime views share the package but are not provider views.
CALC_VIEW_HIER_MARKER = "/hier/"

# BW generates one calc view per BEx query, under a "query.<provider>" package suffix. It shares the
# package family with provider views but is not one, and reading it as a namespaced provider
# produced a "CompositeProvider" whose name was a synthesized path - defect D9.
CALC_VIEW_QUERY_SEGMENT = "query"

# Roles whose row counts represent the provider's own persisted data (vs. changelog/inbound).
PRIMARY_DATA_ROLES: frozenset[TableRole] = frozenset({"active", "fact_f", "fact_e", "master_attr"})


@dataclass(frozen=True)
class ResolvedTable:
    """The BW object a generated physical table belongs to."""

    table: str
    object_name: str | None
    kind: ResolvedKind
    role: TableRole | None
    namespace: str
    is_master_data: bool
    confidence: Literal["confirmed", "advisory"]

    @property
    def is_part_provider_candidate(self) -> bool:
        """True when this table represents a provider that can be a part of a composite."""
        return self.object_name is not None and not self.is_master_data


def split_namespace(name: str) -> tuple[str, str]:
    """Split a BW name into ``(namespace, local_name)``.

    ``/ABC/D_STOCK`` -> ``("/ABC/", "D_STOCK")``; a plain name gets the default ``/BIC/``
    namespace, which is where BW generates tables for non-namespaced objects.
    """
    if name.startswith("/"):
        parts = name[1:].split("/", 1)
        if len(parts) == _NAMESPACE_PARTS and parts[0]:
            return f"/{parts[0]}/", parts[1]
    return _DEFAULT_NAMESPACE, name


def candidate_tables(name: str, kind: str) -> dict[str, TableRole]:
    """Physical tables BW would generate for a provider, mapped to each table's role.

    Used both to find a provider's data (volume/freshness) and to recognise its tables in a
    dependency graph. Returns an empty mapping for kinds that persist no data of their own
    (CompositeProvider, MultiProvider, Open ODS View).
    """
    namespace, local = split_namespace(name)
    # An SAP-delivered object (name starts "0") generates into /BI0/ with the leading 0 dropped.
    if namespace == _DEFAULT_NAMESPACE and local.startswith("0") and len(local) > 1:
        namespace, local = _SAP_NAMESPACE, local[1:]
    normalized = (kind or "").strip().lower()
    if normalized in ("adso", "advanced_dso"):
        return {
            f"{namespace}A{local}1": "inbound",
            f"{namespace}A{local}2": "active",
            f"{namespace}A{local}3": "changelog",
        }
    if normalized in ("dso", "odso", "classic_dso"):
        # '00' is the active table; '40' is the activation queue (new, not-yet-activated data).
        # Both confirmed live: every generated /BIC/A* table on the reference system resolved.
        return {f"{namespace}A{local}00": "active", f"{namespace}A{local}40": "inbound"}
    if normalized in ("infocube", "cube"):
        return {f"{namespace}F{local}": "fact_f", f"{namespace}E{local}": "fact_e"}
    if normalized in ("infoobject", "iobj"):
        return {f"{namespace}P{local}": "master_attr"}
    return {}


def _namespace_of(table: str) -> tuple[str, str] | None:
    """Return ``(namespace, body)`` if ``table`` is a namespaced generated table."""
    if not table.startswith("/"):
        return None
    parts = table[1:].split("/", 1)
    if len(parts) != _NAMESPACE_PARTS or not parts[0] or not parts[1]:
        return None
    return f"/{parts[0]}/", parts[1]


def _candidate_names(table_class: str, rest: str) -> list[tuple[str, ResolvedKind, TableRole]]:
    """Candidate ``(object_name, kind, role)`` readings of a generated table body."""
    candidates: list[tuple[str, ResolvedKind, TableRole]] = []
    if table_class == "A":
        if rest.endswith("00") and len(rest) > _DSO_SUFFIX_LEN:
            candidates.append((rest[:-_DSO_SUFFIX_LEN], "dso", "active"))
        if rest.endswith("40") and len(rest) > _DSO_SUFFIX_LEN:
            candidates.append((rest[:-_DSO_SUFFIX_LEN], "dso", "inbound"))  # activation queue
        adso_role = _ADSO_SUFFIX_ROLES.get(rest[-1]) if rest else None
        if adso_role is not None:
            candidates.append((rest[:-1], "adso", adso_role))
        candidates.append((rest, "adso", "active"))  # ADSO whose name itself ends oddly
        candidates.append((rest, "dso", "active"))
    elif table_class == "F":
        candidates.append((rest, "infocube", "fact_f"))
    elif table_class == "E":
        candidates.append((rest, "infocube", "fact_e"))
    elif table_class == "P":
        candidates.append((rest, "infoobject", "master_attr"))
    elif table_class in _MASTER_DATA_CLASSES:
        candidates.append((rest, "infoobject", "master_other"))
    return [(name, kind, role) for name, kind, role in candidates if name]


def resolve_table(table: str, catalog: Mapping[str, Iterable[str]] | None = None) -> ResolvedTable:
    """Map a generated physical table back to its BW object.

    ``catalog`` optionally maps a kind (``"dso"``, ``"adso"``, ``"infocube"``, ``"infoobject"``) to
    the known object names of that kind. When a candidate reading is confirmed against it the result
    is ``confidence='confirmed'``; otherwise the first plausible reading is returned as
    ``'advisory'`` (mission Rule 2 — the guess is labelled, never presented as fact).
    """
    raw = (table or "").strip()
    upper = raw.upper()
    parsed = _namespace_of(upper)
    if parsed is None:
        return ResolvedTable(
            table=raw,
            object_name=None,
            kind="unknown",
            role=None,
            namespace="",
            is_master_data=False,
            confidence="advisory",
        )
    namespace, body = parsed
    if not body:
        return ResolvedTable(
            table=raw,
            object_name=None,
            kind="unknown",
            role=None,
            namespace=namespace,
            is_master_data=False,
            confidence="advisory",
        )

    table_class, rest = body[0], body[1:]
    is_master = table_class in _MASTER_DATA_CLASSES
    candidates = _candidate_names(table_class, rest)
    if not candidates:
        return ResolvedTable(
            table=raw,
            object_name=None,
            kind="unknown",
            role=None,
            namespace=namespace,
            is_master_data=is_master,
            confidence="advisory",
        )

    # A generated table in /B1H/ or /BIC/ may belong to a namespaced object: try both readings.
    # In /BI0/ the object's leading "0" was dropped when the table was named, so restore it - and
    # prefer that form, since it is the one that matches the catalogue (400/400 measured).
    def with_namespace(candidate: str) -> list[str]:
        if namespace == _SAP_NAMESPACE:
            return [f"0{candidate}", candidate]
        forms = [candidate]
        if namespace not in _GENERATED_NAMESPACES:
            forms.insert(0, f"{namespace}{candidate}")
        return forms

    if catalog:
        known = {kind: {str(n).strip().upper() for n in names} for kind, names in catalog.items()}
        for candidate, kind, role in candidates:
            for form in with_namespace(candidate):
                if form.upper() in known.get(kind, set()):
                    return ResolvedTable(
                        table=raw,
                        object_name=form,
                        kind=kind,
                        role=role,
                        namespace=namespace,
                        is_master_data=is_master,
                        confidence="confirmed",
                    )

    candidate, kind, role = candidates[0]
    return ResolvedTable(
        table=raw,
        object_name=with_namespace(candidate)[0],
        kind=kind,
        role=role,
        namespace=namespace,
        is_master_data=is_master,
        confidence="advisory",
    )


def calc_view_patterns(provider: str) -> list[str]:
    """SQL ``LIKE`` patterns matching the calc view BW generates for a CompositeProvider.

    A plain provider generates ``system-local.bw.bw2hana/<NAME>``. A namespaced provider such as
    ``/ABC/V_STOCK`` generates ``system-local.bw.bw2hana.abc/V_STOCK`` — the namespace becomes a
    lowercase package suffix and the local name loses its prefix. The trailing-match pattern is the
    fallback for any other namespace styling.
    """
    namespace, local = split_namespace(provider)
    patterns = [f"{CALC_VIEW_PACKAGE}/{provider}"]
    if namespace != _DEFAULT_NAMESPACE:
        suffix = namespace.strip("/").lower()
        patterns.append(f"{CALC_VIEW_PACKAGE}.{suffix}/{local}")
    patterns.append(f"%{CALC_VIEW_PACKAGE.rsplit('.', maxsplit=1)[-1]}/{local}")
    unique: list[str] = []
    for pattern in patterns:
        if pattern not in unique:
            unique.append(pattern)
    return unique


def is_hierarchy_view(view_name: str) -> bool:
    """True for generated hierarchy runtime views, which are not provider views.

    The marker is matched as a path *segment*, because BW generates both ``.../hier/...`` and names
    whose final segment is ``hier``. Only the embedded form was recognised, so a view ending in
    ``/hier`` resolved to a "provider" literally named ``hier`` and reached callers as a
    CompositeProvider consumer. Matching the segment - rather than the bare substring - also leaves
    a genuinely named provider such as ``/HIERARCHY_X`` alone.
    """
    lowered = (view_name or "").lower()
    segment = CALC_VIEW_HIER_MARKER.rstrip("/")
    return CALC_VIEW_HIER_MARKER in lowered or lowered.endswith(segment)


def provider_from_calc_view(view_name: str) -> str | None:
    """Reverse :func:`calc_view_patterns`: the provider a generated calc view belongs to.

    ``system-local.bw.bw2hana/SALES_CP`` -> ``SALES_CP``;
    ``system-local.bw.bw2hana.abc/V_STOCK`` -> ``/ABC/V_STOCK``.
    Returns ``None`` for views outside the generated package, and for hierarchy runtime views.
    """
    name = (view_name or "").strip()
    if not name or is_hierarchy_view(name):
        return None
    package, _, local = name.rpartition("/")
    if not local or not package:
        return None
    base = CALC_VIEW_PACKAGE.rsplit(".", maxsplit=1)[-1]  # "bw2hana"
    if base not in package:
        return None
    # A namespaced provider encodes its namespace as a lowercase package suffix after "bw2hana".
    _, _, suffix = package.partition(f"{base}.")
    if suffix:
        if _is_query_suffix(suffix):
            # A generated *query* view. Not a provider, and not this function's to resolve:
            # query_from_calc_view reads it. Returning a provider here is what made a BEx query
            # arrive as a CompositeProvider named '/QUERY.<PROVIDER>/<QUERY>'.
            return None
        return f"/{suffix.upper()}/{local}"
    return local


def _is_query_suffix(suffix: str) -> bool:
    """Whether a package suffix marks a generated query view rather than a provider namespace.

    A query view's suffix is ``query.<provider>`` - two segments. A namespace suffix is one segment,
    so a provider genuinely living in a ``/QUERY/`` namespace is left alone.
    """
    return suffix.lower().startswith(f"{CALC_VIEW_QUERY_SEGMENT}.")


def query_from_calc_view(view_name: str) -> tuple[str, str] | None:
    """The BEx query and the provider it reads, for a generated *query* calc view.

    ``system-local.bw.bw2hana.query.sales_cp/Q_REVENUE`` -> ``("Q_REVENEUE", "SALES_CP")``, modulo
    spelling. Returns ``None`` for anything that is not a generated query view, so a caller can try
    :func:`provider_from_calc_view` and this one in either order without double-counting.

    The query's own technical name is returned, because that is the object a BW developer looks up.
    """
    name = (view_name or "").strip()
    if not name or is_hierarchy_view(name):
        return None
    package, _, local = name.rpartition("/")
    if not local or not package:
        return None
    base = CALC_VIEW_PACKAGE.rsplit(".", maxsplit=1)[-1]
    if base not in package:
        return None
    _, _, suffix = package.partition(f"{base}.")
    if not _is_query_suffix(suffix):
        return None
    provider = suffix[len(CALC_VIEW_QUERY_SEGMENT) + 1 :]
    if not provider:
        return None
    return local, provider.upper()
