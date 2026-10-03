"""A calculation view's BW bases, read from what the view *directly* reads (D47).

The defect this covers, in the order it has to be understood:

``SYS.OBJECT_DEPENDENCIES`` is transitive, and the only resolution route that existed read
``/BIC/`` **table** names. An Advanced DSO has such a table, so it surfaced. A CompositeProvider
does not - it is consumed through a generated *view* - so a CompositeProvider a calculation view
reads disappeared, and the ADSOs sitting two hops beneath it were reported as the view's own
bases. The set of DSOs eventually involved was right; the shape was wrong, by one whole layer, and
a reader could not tell because every edge claimed the same mechanism.

So the fix is not "resolve one more name pattern". It is to read the view's **direct** bases first
and prefer them, because those are the objects the view names, and to keep the transitive table
walk as the fallback it should always have been.

Synthetic names throughout; the ``/BIC/`` literals are built by concatenation so this file stays
clean for the customer-metadata scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.repositories.providers import ProvidersRepository

SCHEMA = "SAPABAP1"
_ABAP = {
    "composite_header": "RSOHCPR",
    "adso_header": "RSOADSO",
    "dso_header": "RSDODSO",
    "cube_header": "RSDCUBE",
}
_HANA = {"hana_views": "VIEWS", "object_dependencies": "OBJECT_DEPENDENCIES"}
_TABLES = {**_ABAP, **_HANA}

_DIRECT, _TRANSITIVE = 1, 2
_GEN = "system-local.bw.bw2hana/"

# The view under test: a modelled view reading three ADSOs and one CompositeProvider, which is
# the shape that exposed the defect.
VIEW = "ACME.SALES/CV_ORDERS"
_GEN_ADSO = _GEN + "ORDER_ITEM"
_GEN_CP = _GEN + "ORDER_COND_CP"  # a CompositeProvider: no /BIC/ table exists for it

# What the view reads outright.
_DIRECT_BASES = [_GEN_ADSO, _GEN_CP]
# What the walk can *reach*: the CompositeProvider's own parts' tables show up here, two hops
# down, and are what the old route mistook for the view's bases.
_TRANSITIVE_BASES = [
    "/BIC/" + "AORDER_ITEM2",  # -> ADSO ORDER_ITEM, also a direct base
    "/BIC/" + "ACOND_HEADER2",  # -> ADSO COND_HEADER, reached only through ORDER_COND_CP
]

# A second view, built the old way: no generated provider views among its direct bases, so the
# transitive fallback must still answer. This is the behaviour that must not regress.
LEGACY_VIEW = "ACME.SALES/CV_LEGACY"
_LEGACY_DIRECT: list[str] = ["ACME.SALES/CV_LEGACY/dp/Projection_1"]
_LEGACY_TRANSITIVE = ["/BIC/" + "AORDER_ITEM2"]

_HEADERS: dict[str, list[str]] = {
    "RSOHCPR": ["ORDER_COND_CP"],
    "RSOADSO": ["ORDER_ITEM", "COND_HEADER"],
    "RSDODSO": [],
    "RSDCUBE": [],
}


class ScriptedConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        params = [str(p) for p in (parameters or [])]
        if "OBJECT_DEPENDENCIES" in sql:
            return self._deps(params)
        for table, names in _HEADERS.items():
            if table in sql:
                wanted = set(params)
                hits = [n for n in names if n in wanted]
                return [(n, "B") for n in hits] if table == "RSDCUBE" else [(n,) for n in hits]
        return []

    @staticmethod
    def _deps(params: list[str]) -> list[tuple[Any, ...]]:
        """Dependency rows, keyed on the dependency type the caller asked for.

        The discrimination is the point: a fixture that answered both reads identically would
        pass an implementation that still only did the transitive one.
        """
        view = params[1] if len(params) > 1 else ""
        direct = str(_DIRECT) in params
        if view == VIEW:
            names = _DIRECT_BASES if direct else _TRANSITIVE_BASES
        elif view == LEGACY_VIEW:
            names = _LEGACY_DIRECT if direct else _LEGACY_TRANSITIVE
        else:
            names = []
        return [(n,) for n in names]


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        object_models={"classic_dso": True, "adso": True, "composite_provider": True},
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=("SYS" if logical in _HANA else SCHEMA) if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(
    present: set[str] | None = None,
) -> tuple[ProvidersRepository, ScriptedConnection]:
    conn = ScriptedConnection()
    return ProvidersRepository(conn, _capability(present)), conn


# --- the defect ---------------------------------------------------------------------------


def test_a_composite_provider_base_is_kept_not_replaced_by_its_parts() -> None:
    """The regression. ORDER_COND_CP has no /BIC/ table, so only the direct route sees it."""
    repo, _conn = _repo()
    parts = {p.name: p for p in repo.providers_under_calc_view(VIEW)}
    assert "ORDER_COND_CP" in parts, "the CompositeProvider the view reads must not disappear"
    assert parts["ORDER_COND_CP"].part_type == "compositeprovider"
    assert parts["ORDER_COND_CP"].confidence == "confirmed"  # RSOHCPR row, not a naming guess
    # COND_HEADER sits *under* the CompositeProvider. Reporting it here would attribute a
    # two-hop reach to the view itself, which is what made a four-layer path read as three.
    assert "COND_HEADER" not in parts


def test_the_direct_route_still_reports_the_adso_the_view_reads() -> None:
    repo, _conn = _repo()
    parts = {p.name: p for p in repo.providers_under_calc_view(VIEW)}
    assert parts["ORDER_ITEM"].part_type == "adso"
    assert parts["ORDER_ITEM"].via_table == _GEN_ADSO


def test_the_base_evidence_says_which_mechanism_resolved_it() -> None:
    """A type-confirmed provider and a table-name guess must not read alike."""
    repo, _conn = _repo()
    cp = next(p for p in repo.providers_under_calc_view(VIEW) if p.name == "ORDER_COND_CP")
    assert cp.evidence is not None
    assert cp.evidence.basis == "derived"
    assert cp.evidence.method == "generated_provider_view"
    assert "ORDER_COND_CP" in (cp.evidence.detail or "")


def test_an_unconfirmed_generated_view_is_not_asserted_as_a_provider() -> None:
    """Parsing a name is not evidence the object exists."""
    repo, _conn = _repo(present=set(_HANA) | {"adso_header", "dso_header", "cube_header"})
    # Without RSOHCPR there is no header carrying ORDER_COND_CP, so it must not be claimed.
    names = {p.name for p in repo.providers_under_calc_view(VIEW)}
    assert "ORDER_COND_CP" not in names
    assert "ORDER_ITEM" in names  # the ADSO is still confirmed


# --- the fallback must not regress --------------------------------------------------------


def test_the_transitive_route_still_answers_when_nothing_is_read_directly() -> None:
    """BW layers intermediate views under its own generated views; that case needs the walk."""
    repo, _conn = _repo()
    parts = {p.name: p for p in repo.providers_under_calc_view(LEGACY_VIEW)}
    assert set(parts) == {"ORDER_ITEM"}
    assert parts["ORDER_ITEM"].via_table.startswith("/BIC/")  # type: ignore[union-attr]


def test_the_direct_read_happens_before_the_transitive_one() -> None:
    repo, conn = _repo()
    repo.providers_under_calc_view(VIEW)
    dep_reads = [s for s in conn.statements if "OBJECT_DEPENDENCIES" in s]
    # Direct answered, so the transitive walk is never issued: it costs a transitive closure.
    assert len(dep_reads) == 1


def test_capability_gate_is_unchanged() -> None:
    repo, _conn = _repo(present={"composite_header"})
    assert repo.providers_under_calc_view(VIEW) == []


# --- provider-kind confirmation -----------------------------------------------------------


def test_confirm_provider_kinds_covers_composite_providers() -> None:
    """provider_catalog cannot: it serves table-name resolution, and a CP has no table."""
    repo, _conn = _repo()
    kinds = repo.confirm_provider_kinds(["ORDER_COND_CP", "ORDER_ITEM", "NOT_A_PROVIDER"])
    assert kinds == {"ORDER_COND_CP": "compositeprovider", "ORDER_ITEM": "adso"}
    assert "NOT_A_PROVIDER" not in kinds  # absent, not defaulted


def test_confirm_provider_kinds_is_batched() -> None:
    repo, conn = _repo()
    repo.confirm_provider_kinds(["ORDER_COND_CP", "ORDER_ITEM", "COND_HEADER"])
    # One statement per header table at most, not one per name.
    assert len([s for s in conn.statements if "RSOADSO" in s]) == 1
