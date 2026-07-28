"""Tests for the ADT source-system connector and extractor-exit analysis.

Entirely offline: the ADT transport is a scripted fetcher, so no HTTP stack and no source system are
involved. ABAP fixtures use synthetic DataSource and table names.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr

from mcp_server_sapbw.connectors.ecc import (
    EXIT_SLOTS,
    AdtError,
    AdtResponse,
    EccConnector,
)
from mcp_server_sapbw.core.profiles import EccProfile
from mcp_server_sapbw.services.exit_analysis import (
    ExitAnalysisService,
    parse_handled_datasources,
    slice_branches,
)

# A transaction-data exit serving two DataSources. The first branch does a per-record read inside a
# LOOP; the second does a single set-based read. Attributing the LOOP to both would be wrong.
_EXIT_ABAP = """\
FUNCTION EXIT_SAPLRSAP_001.
* Extractor enhancement dispatch
  CASE i_datasource.
    WHEN 'DS_SALES'.
      LOOP AT c_t_data INTO ls_data.
        SELECT single custom_field FROM tbl_side INTO lv_val
          WHERE key = ls_data-key.
        MODIFY c_t_data FROM ls_data.
      ENDLOOP.
    WHEN 'DS_FIN' OR 'DS_FIN_B'.
      SELECT field FROM tbl_other INTO TABLE lt_buffer
        FOR ALL ENTRIES IN lt_keys WHERE key = lt_keys-key.
    WHEN OTHERS.
      EXIT.
  ENDCASE.
ENDFUNCTION.
"""


class ScriptedFetcher:
    """Returns a canned ADT response per path suffix and records what was requested."""

    def __init__(self, bodies: dict[str, tuple[int, str]]) -> None:
        self._bodies = bodies
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_text(self, path: str, params: Mapping[str, str]) -> AdtResponse:
        self.calls.append((path, dict(params)))
        for name, (status, body) in self._bodies.items():
            if f"/{name.lower()}/" in path:
                return AdtResponse(status, body)
        return AdtResponse(404, "")


def _profile(**overrides: object) -> EccProfile:
    values: dict[str, object] = {
        "name": "src",
        "host": "source.example.invalid",
        "port": 44300,
        "client": "300",
        "user": "reader",
        "password": SecretStr("secret-value"),
    }
    values.update(overrides)
    return EccProfile(**values)  # type: ignore[arg-type]


def _service(bodies: dict[str, tuple[int, str]]) -> tuple[ExitAnalysisService, ScriptedFetcher]:
    fetcher = ScriptedFetcher(bodies)
    connector = EccConnector(_profile(), fetcher)
    return ExitAnalysisService(connector), fetcher


# --- profile safety --------------------------------------------------------------------------


def test_plain_http_needs_an_explicit_opt_in() -> None:
    try:
        _profile(use_tls=False)
    except ValueError as exc:
        assert "allow_plain_http" in str(exc)
    else:  # pragma: no cover - the validator must reject this
        raise AssertionError("use_tls=False without allow_plain_http must be rejected")


def test_plain_http_is_allowed_once_opted_in() -> None:
    profile = _profile(use_tls=False, allow_plain_http=True)
    assert profile.base_url.startswith("http://")


def test_tls_is_the_default() -> None:
    assert _profile().base_url.startswith("https://")


def test_status_names_the_profile_and_client_but_never_the_host() -> None:
    status = EccConnector(_profile(), ScriptedFetcher({})).status()
    assert status.configured is True
    assert "source.example.invalid" not in status.detail
    assert "secret-value" not in status.detail
    assert "src" in status.detail
    assert "300" in status.detail


# --- transport -------------------------------------------------------------------------------


def test_client_is_sent_and_include_path_is_tried_first() -> None:
    service, fetcher = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    service.inventory()
    first_path, params = fetcher.calls[0]
    assert params == {"sap-client": "300"}
    assert "/programs/includes/zxrsau01/source/main" in first_path


def test_program_path_is_tried_when_the_include_path_is_absent() -> None:
    # 404 on every include path; the fallback program path must be attempted too.
    service, fetcher = _service({})
    service.inventory()
    segments = [path.split("/programs/")[1].split("/")[0] for path, _ in fetcher.calls]
    assert "includes" in segments
    assert "programs" in segments


def test_unconfigured_connector_refuses_to_fetch() -> None:
    try:
        EccConnector().fetch_source("ZXRSAU01")
    except AdtError as exc:
        assert "not configured" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unconfigured connector must not fetch")


def test_all_four_slots_are_read() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    inventory = service.inventory()
    assert [e.include_name for e in inventory.exits] == [s.include for s in EXIT_SLOTS]
    assert [e.exit_function_module for e in inventory.exits] == [
        s.function_module for s in EXIT_SLOTS
    ]


# --- unavailability is classified, not conflated ---------------------------------------------


def test_absent_include_means_no_enhancement_of_that_kind() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    inventory = service.inventory()
    absent = [e for e in inventory.exits if e.include_name != "ZXRSAU01"]
    assert all(e.unavailable_reason == "absent" for e in absent)
    assert all("no enhancement" in (e.note or "") for e in absent)
    assert inventory.available_count == 1


def test_forbidden_is_unknown_rather_than_absent() -> None:
    service, _ = _service({"ZXRSAU01": (403, "")})
    inventory = service.inventory()
    slot = inventory.exits[0]
    assert slot.available is False
    assert slot.unavailable_reason == "forbidden"
    assert any("unknown rather than absent" in c for c in inventory.caveats)


def test_unauthorized_is_reported_distinctly() -> None:
    service, _ = _service({"ZXRSAU01": (401, "")})
    assert service.inventory().exits[0].unavailable_reason == "unauthorized"


def test_transport_failure_degrades_to_a_finding() -> None:
    class FailingFetcher:
        def get_text(self, path: str, params: Mapping[str, str]) -> AdtResponse:
            raise AdtError("ADT request to /p failed (ConnectError); connection details withheld")

    service = ExitAnalysisService(EccConnector(_profile(), FailingFetcher()))
    inventory = service.inventory()
    assert all(e.unavailable_reason == "fetch_failed" for e in inventory.exits)
    assert all("source.example.invalid" not in (e.note or "") for e in inventory.exits)


# --- dispatch parsing ------------------------------------------------------------------------


def test_handled_datasources_come_from_case_literals() -> None:
    handled = parse_handled_datasources(_EXIT_ABAP.splitlines())
    assert handled == ["DS_SALES", "DS_FIN", "DS_FIN_B"]


def test_when_others_is_not_a_datasource() -> None:
    assert "OTHERS" not in parse_handled_datasources(_EXIT_ABAP.splitlines())


def test_commented_out_branches_are_ignored() -> None:
    lines = ["  CASE i_datasource.", "*   WHEN 'DS_OLD'.", "    WHEN 'DS_NEW'.", "  ENDCASE."]
    assert parse_handled_datasources(lines) == ["DS_NEW"]


def test_if_style_dispatch_is_also_detected() -> None:
    lines = ["  IF i_datasource = 'DS_IF'.", "    SELECT a FROM tbl_x INTO lv.", "  ENDIF."]
    assert parse_handled_datasources(lines) == ["DS_IF"]


def test_branch_slicing_stops_at_the_next_when() -> None:
    branches = slice_branches(_EXIT_ABAP.splitlines())
    assert "SELECT" in " ".join(branches["DS_SALES"])
    assert "tbl_other" not in " ".join(branches["DS_SALES"])
    assert "tbl_side" not in " ".join(branches["DS_FIN"])


def test_or_branch_shares_its_lines() -> None:
    branches = slice_branches(_EXIT_ABAP.splitlines())
    assert branches["DS_FIN"] == branches["DS_FIN_B"]


# --- per-branch risk attribution -------------------------------------------------------------


def _branch(name: str) -> object:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    slot = service.inventory().exits[0]
    return next(b for b in slot.branches if b.datasource == name)


def test_per_record_select_is_attributed_only_to_its_own_branch() -> None:
    assert _branch("DS_SALES").per_record_selects == 1  # type: ignore[attr-defined]
    assert _branch("DS_FIN").per_record_selects == 0  # type: ignore[attr-defined]


def test_table_reads_are_scoped_to_the_branch() -> None:
    assert _branch("DS_SALES").table_reads == ["tbl_side"]  # type: ignore[attr-defined]
    assert _branch("DS_FIN").table_reads == ["tbl_other"]  # type: ignore[attr-defined]


def test_branch_analysis_flags_select_in_loop() -> None:
    assert "select_in_loop" in _branch("DS_SALES").anti_pattern_kinds  # type: ignore[attr-defined]


def test_unresolvable_branch_is_marked_rather_than_approximated() -> None:
    # Dispatch by IF gives no CASE branch to delimit.
    service, _ = _service(
        {"ZXRSAU01": (200, "FUNCTION f.\n  IF i_datasource = 'DS_IF'.\n  ENDIF.\nENDFUNCTION.")}
    )
    branch = service.inventory().exits[0].branches[0]
    assert branch.datasource == "DS_IF"
    assert branch.resolved is False
    assert branch.table_reads == []


# --- inventory shape -------------------------------------------------------------------------


def test_whole_include_analysis_is_still_available() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    slot = service.inventory().exits[0]
    assert slot.analysis is not None
    assert slot.analysis.kind == "exit"
    assert {d.table for d in slot.analysis.table_dependencies} == {"tbl_side", "tbl_other"}


def test_provenance_records_the_adt_path_and_omits_the_host() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    provenance = service.inventory().exits[0].provenance
    assert provenance is not None
    assert provenance.connector == "ecc_adt"
    assert provenance.object_name == "ZXRSAU01"
    assert provenance.client == "300"
    assert "source.example.invalid" not in provenance.adt_path
    assert "source.example.invalid" not in provenance.model_dump_json()


def test_source_text_is_opt_in() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    assert service.inventory().exits[0].source is None
    assert service.inventory(include_source=True).exits[0].source == _EXIT_ABAP


def test_long_source_is_truncated_with_a_note() -> None:
    long_abap = "\n".join(["* filler"] * 5000)
    service, _ = _service({"ZXRSAU01": (200, long_abap)})
    slot = service.inventory(include_source=True).exits[0]
    assert slot.line_count == 5000
    assert slot.source is not None
    assert len(slot.source.splitlines()) == 4000
    assert "truncated" in (slot.note or "")


def test_handled_union_spans_slots() -> None:
    other = "FUNCTION f.\n  CASE i_datasource.\n    WHEN 'DS_TEXT'.\n  ENDCASE.\nENDFUNCTION."
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP), "ZXRSAU03": (200, other)})
    inventory = service.inventory()
    assert inventory.handled_datasources == ["DS_SALES", "DS_FIN", "DS_FIN_B", "DS_TEXT"]
    assert inventory.available_count == 2


def test_lower_bound_caveats_are_always_present() -> None:
    service, _ = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    caveats = " ".join(service.inventory().caveats)
    assert "lower bound" in caveats
    assert "under-reported rather than over-reported" in caveats
