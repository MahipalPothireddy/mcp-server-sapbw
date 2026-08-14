"""Release portability: every entry point, against every capability shape.

The server is meant to be pointed at any BW-on-HANA system, and has only ever been validated live
against one. Portability rests on the capability resolver plus the rule that a repository checks
availability *before* building SQL. This suite turns that from a claim into a checked property.

For each shape in ``release_shapes.SHAPES``, every public entry point is invoked and must:

* never raise ``DialectError`` — that would mean SQL was built naming a table this release lacks,
  which is the exact failure the resolver exists to prevent;
* never raise anything else uncaught — a missing table is a documented gap, not a crash;
* return a structured ``UnsupportedResult`` when it genuinely cannot answer.

The connection returns no rows for everything, so this exercises degradation rather than results.
That is deliberate: results are covered by the per-domain suites, and what is untested elsewhere is
what happens on a system shaped differently from the reference one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any

import pytest

from mcp_server_sapbw.core.dialect import DialectError
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.hana import HanaRepository
from mcp_server_sapbw.repositories.health import HealthRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.queries import QueriesRepository
from mcp_server_sapbw.repositories.search import SearchRepository
from mcp_server_sapbw.repositories.security import SecurityRepository
from mcp_server_sapbw.repositories.sources import SourcesRepository
from mcp_server_sapbw.repositories.threex import ThreeXRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.services.analyzers import Analyzers
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.load_closure import LoadClosureService
from mcp_server_sapbw.services.routine_register import RoutineRegisterService
from tests.release_shapes import ALL_LOGICAL, FULL_75, SHAPES, _physical, capability


class EmptyConnection:
    """Answers every statement with no rows, so only degradation behaviour is under test."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        return []


def _entry_points(connection: EmptyConnection, record: Any) -> dict[str, Callable[[], Any]]:
    """Every public read path, keyed by the tool it backs."""
    chains = ChainsRepository(connection, record)
    providers = ProvidersRepository(connection, record)
    search = SearchRepository(connection, record)
    transformations = TransformationsRepository(connection, record)
    queries = QueriesRepository(connection, record)
    hana = HanaRepository(connection, record)
    lineage = LineageService(connection, record)
    analyzers = Analyzers(connection, record)
    closure = LoadClosureService(connection, record)
    register = RoutineRegisterService(connection, record)
    health = HealthRepository(connection, record)
    sources = SourcesRepository(connection, record)
    threex = ThreeXRepository(connection, record)
    security = SecurityRepository(connection, record)

    return {
        "bw_list_chains": lambda: chains.list_chains(limit=5),
        "bw_get_chain": lambda: chains.get_chain("ANY_CHAIN"),
        "bw_get_chain_runtimes": lambda: chains.get_chain_runtimes("ANY_CHAIN", days=30),
        "bw_get_schedule_matrix": lambda: chains.get_schedule_matrix(limit=5),
        "bw_describe_object": lambda: providers.describe("ANY_OBJECT"),
        "bw_search_objects": lambda: search.search("ANY", limit=5),
        "bw_list_transformations": lambda: transformations.list_transformations(limit=5),
        "bw_get_transformation": lambda: transformations.get_transformation("ANY_TRAN"),
        "bw_get_routine_code": lambda: transformations.get_routine_code("ANY_TRAN"),
        "bw_analyze_routine": lambda: transformations.analyze_routines("ANY_TRAN"),
        "bw_list_queries": lambda: queries.list_queries(limit=5),
        "bw_get_query": lambda: queries.get_query("ANY_QUERY"),
        "bw_get_query_lineage": lambda: queries.get_query_lineage("ANY_QUERY"),
        "bw_get_query_usage": lambda: queries.get_query_usage("ANY_QUERY"),
        "bw_list_calc_views": lambda: hana.list_calc_views(limit=5),
        "bw_get_calc_view_lineage": lambda: hana.get_calc_view_lineage("ANY_VIEW"),
        "bw_get_hana_crossings": lambda: hana.get_hana_crossings(limit=5),
        "bw_get_lineage": lambda: lineage.get_lineage("ANY_OBJECT", depth=2),
        "bw_impact_analysis": lambda: lineage.impact_analysis("ANY_OBJECT", depth=2),
        "bw_trace_to_source": lambda: lineage.trace_to_source("ANY_OBJECT", depth=2),
        "bw_check_load_latency": lambda: analyzers.check_load_latency(limit=3),
        "bw_check_schedule_risk": lambda: analyzers.schedule_risk(limit=3),
        "bw_find_layer_violations": lambda: analyzers.find_layer_violations(limit=3),
        "bw_find_unused_providers": lambda: analyzers.find_unused_providers(limit=3),
        "bw_get_routine_register": lambda: register.build(limit=3),
        "bw_get_provider_health": lambda: health.get_health("ANY_PROVIDER"),
        "bw_get_load_closure_chain": lambda: closure.chain_to_providers("ANY_CHAIN"),
        "bw_get_load_closure_provider": lambda: closure.provider_to_chains("ANY_PROVIDER"),
        "bw_get_source_systems": sources.get_topology,
        "bw_list_extractor_enhancements": lambda: sources.enhancement_inventory(limit=3),
        "bw_list_3x_flows": lambda: threex.list_flows(limit=3),
        "bw_get_transfer_rules": lambda: threex.get_transfer_rules("ANY_TS"),
        "bw_list_update_rules": lambda: threex.list_update_rules(limit=3),
        "bw_security_overview": lambda: security.overview(limit=3),
        "bw_list_analysis_auths": lambda: security.list_authorisations(limit=3),
        "bw_get_analysis_auth": lambda: security.get_authorisation("ANY_AUTH"),
        **{
            f"scenario_{scenario}": (lambda s=scenario: analyzers.run_scenario(s, limit=3))  # type: ignore[misc]
            for scenario in ("9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "9.8")
        },
    }


@pytest.mark.parametrize("shape_name", sorted(SHAPES))
def test_no_entry_point_builds_sql_for_an_absent_table(shape_name: str) -> None:
    """Mission acceptance criterion: no tool ever queries a non-existent table."""
    record = capability(SHAPES[shape_name])
    connection = EmptyConnection()
    failures: list[str] = []

    for tool, call in _entry_points(connection, record).items():
        try:
            call()
        except DialectError as exc:
            failures.append(f"{tool}: built SQL for an unavailable table ({exc})")
        except Exception as exc:
            failures.append(f"{tool}: raised {type(exc).__name__}: {exc}")

    assert not failures, f"shape '{shape_name}':\n" + "\n".join(failures)


@pytest.mark.parametrize("shape_name", sorted(SHAPES))
def test_every_statement_names_only_available_tables(shape_name: str) -> None:
    """Belt and braces: inspect the SQL actually issued, not just that nothing raised."""
    present = SHAPES[shape_name]
    record = capability(present)
    connection = EmptyConnection()
    for call in _entry_points(connection, record).values():
        with suppress(Exception):  # raising is covered by the test above
            call()

    absent_physical = {
        status.logical_name: record.table(status.logical_name)
        for status in record.tables.values()
        if not status.present
    }
    leaked = [
        (logical, statement)
        for logical in absent_physical
        for statement in connection.statements
        if f'"{_physical(logical)}"' in statement
    ]
    assert not leaked, f"shape '{shape_name}' issued SQL naming absent tables: {leaked[:3]}"


def test_nothing_available_still_answers_every_tool() -> None:
    """With no tables at all, every entry point must report the gap rather than fail."""
    record = capability(SHAPES["nothing_available"])
    connection = EmptyConnection()
    unsupported = 0
    for call in _entry_points(connection, record).values():
        result = call()
        if isinstance(result, UnsupportedResult):
            unsupported += 1
    assert unsupported > 20, "most tools should report an unsupported release, not return data"


def test_the_reference_shape_is_the_full_catalogue() -> None:
    """A guard on the fixture itself: if a new table is added, the shapes must be revisited."""
    assert set(FULL_75) == set(ALL_LOGICAL)
