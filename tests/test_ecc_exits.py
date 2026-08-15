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
from mcp_server_sapbw.services.analyzers import _exit_index, _exit_risk
from mcp_server_sapbw.services.exit_analysis import (
    ExitAnalysisService,
    parse_dynamic_dispatch,
    parse_handled_datasources,
    satellite_program_name,
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


# --- satellite exit programs (logic reached by a runtime-named PERFORM) -----------------------
#
# The defect these cover: an include that dispatches on a computed program name looks nearly empty
# to a static reader, so the honest-seeming conclusion "this enhancement does almost nothing" is
# exactly wrong. Everything worth reporting is in the satellite.

# Synthetic satellite prefixes, assembled at runtime so this file carries no literal
# customer-namespace token. Two of them, because a site may use one prefix for transaction-data
# enhancements and another for master data, and that per-slot attribution has to be covered.
_PFX_TX = "Z" + "AA_"  # contributed by the transaction-data exit
_PFX_MD = "Z" + "BB_"  # contributed by the master-data exit
_SAT_TX = _PFX_TX + "DS_SALES"

# Inline logic for one DataSource, a computed dispatch for the rest. This mirrors the shape found on
# the reference system, where the dispatch sat at the end of the include rather than in a branch.
_DISPATCHING_EXIT = f"""\
FUNCTION EXIT_SAPLRSAP_001.
  DATA lv_prog TYPE progname.
  CASE i_datasource.
    WHEN 'DS_INLINE'.
      SELECT field FROM tbl_inline INTO lv_val WHERE key = 'X'.
    WHEN OTHERS.
      CONCATENATE '{_PFX_TX}' i_datasource INTO lv_prog.
      PERFORM execute_user_exit IN PROGRAM (lv_prog) IF FOUND.
  ENDCASE.
ENDFUNCTION.
"""

# The master-data exit dispatching to a DIFFERENT prefix. A site may name its transaction-data and
# master-data satellites differently, and the prefix each slot contributes has to stay attributed to
# that slot rather than being pooled.
_DISPATCHING_EXIT_MD = f"""\
FUNCTION EXIT_SAPLRSAP_002.
  DATA lv_prog TYPE progname.
  CONCATENATE '{_PFX_MD}' i_datasource INTO lv_prog.
  PERFORM execute_user_exit IN PROGRAM (lv_prog) IF FOUND.
ENDFUNCTION.
"""

# One unguarded FOR ALL ENTRIES and one SELECT inside a LOOP: both invisible from the include.
_SATELLITE_ABAP = f"""\
PROGRAM {_SAT_TX}.
FORM execute_user_exit.
  SELECT partner FROM tbl_partner INTO TABLE lt_partner
    FOR ALL ENTRIES IN lt_keys WHERE key = lt_keys-key.
  LOOP AT c_t_data INTO ls_data.
    SELECT single rate FROM tbl_rate INTO lv_rate WHERE id = ls_data-id.
  ENDLOOP.
ENDFORM.
"""


def test_dynamic_dispatch_is_detected_and_the_naming_rule_recovered() -> None:
    dynamic, prefixes = parse_dynamic_dispatch(_DISPATCHING_EXIT.splitlines())
    assert dynamic is True
    assert prefixes == [_PFX_TX]


def test_a_literal_program_name_is_not_a_dynamic_dispatch() -> None:
    # `IN PROGRAM zfoo` names the program statically, so it is followable and must not be flagged.
    lines = ["  PERFORM do_it IN PROGRAM zfoo IF FOUND."]
    assert parse_dynamic_dispatch(lines) == (False, [])


def test_unrelated_literals_are_not_mistaken_for_a_naming_rule() -> None:
    lines = [
        "  CONCATENATE i_datasource '-' sy-datum INTO lv_msg.",
        "  CONCATENATE 'Error in ' i_datasource INTO lv_text.",
        "  PERFORM x IN PROGRAM (lv_prog).",
    ]
    dynamic, prefixes = parse_dynamic_dispatch(lines)
    assert dynamic is True
    assert prefixes == []


def test_a_namespaced_datasource_drops_its_namespace() -> None:
    # Verified against a real system: a DataSource in a partner namespace is served by a program
    # named from the local part alone. Rejecting the slash instead of stripping it loses the
    # satellite entirely and reports "no satellite" for a program that exists.
    assert satellite_program_name(_PFX_TX, "/PARTNER/SOME_DS") == _PFX_TX + "SOME_DS"
    assert satellite_program_name(_PFX_TX, "/PARTNER/OTHER_ATTR") == _PFX_TX + "OTHER_ATTR"


def test_a_name_with_an_inner_slash_is_not_forced_into_a_candidate() -> None:
    assert satellite_program_name(_PFX_TX, "AAA/BBB") is None
    assert satellite_program_name(_PFX_TX, "/NS/") is None


def test_an_overlong_candidate_is_rejected_without_a_request() -> None:
    assert satellite_program_name(_PFX_TX, "D" * 40) is None
    assert satellite_program_name(_PFX_TX, "DS_SALES") == _SAT_TX


def _dispatching_service(
    extra: dict[str, tuple[int, str]] | None = None, **profile_overrides: object
) -> tuple[ExitAnalysisService, ScriptedFetcher]:
    bodies = {"ZXRSAU01": (200, _DISPATCHING_EXIT)}
    bodies.update(extra or {})
    fetcher = ScriptedFetcher(bodies)
    connector = EccConnector(_profile(**profile_overrides), fetcher)
    return ExitAnalysisService(connector), fetcher


def test_satellite_logic_is_read_and_attributed_to_its_datasource() -> None:
    service, _ = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    inventory = service.inventory(datasources=["DS_SALES"])
    found = [s for s in inventory.satellites if s.available]
    assert [s.program_name for s in found] == [_SAT_TX]
    satellite = found[0]
    assert satellite.datasource == "DS_SALES"
    assert satellite.per_record_selects == 1
    assert satellite.unguarded_for_all_entries == 1
    assert satellite.table_reads == ["tbl_partner", "tbl_rate"]
    # The prefix came out of the transaction-data exit, so the attribution is evidence, not a guess.
    assert satellite.dispatched_from == "transaction_data"
    assert satellite.prefix == _PFX_TX


def test_the_include_alone_would_have_reported_none_of_that_risk() -> None:
    """The point of the feature: the same read without satellites finds nothing for DS_SALES."""
    service, _ = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    slot = service.inventory().exits[0]
    inline = {b.datasource for b in slot.branches}
    assert "DS_SALES" not in inline
    assert slot.dynamic_dispatch is True
    assert slot.satellite_prefixes == [_PFX_TX]


def test_a_checked_absence_is_recorded_rather_than_omitted() -> None:
    # "No satellite exists" is a measurement; dropping it would leave a checked absence
    # indistinguishable from one nobody looked for.
    service, _ = _dispatching_service()
    inventory = service.inventory(datasources=["DS_NOTHING"])
    assert [(s.program_name, s.unavailable_reason) for s in inventory.satellites] == [
        ((_PFX_TX + "DS_NOTHING"), "absent")
    ]
    assert inventory.satellites_found_count == 0
    assert inventory.satellite_candidates_considered == 1


def test_an_unreadable_satellite_is_unknown_not_absent() -> None:
    service, _ = _dispatching_service({_SAT_TX: (403, "")})
    inventory = service.inventory(datasources=["DS_SALES"])
    assert inventory.satellites[0].unavailable_reason == "forbidden"
    assert any("unknown rather than absent" in c for c in inventory.caveats)


def test_the_program_path_is_tried_first_for_a_satellite() -> None:
    # A satellite is a standalone program. Trying the include path first would spend an extra 404 on
    # every one of potentially hundreds of probes.
    service, fetcher = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    service.inventory(datasources=["DS_SALES"])
    satellite_calls = [path for path, _ in fetcher.calls if _SAT_TX.lower() in path]
    assert satellite_calls
    assert "/programs/programs/" in satellite_calls[0]


def test_a_program_is_probed_only_once_per_service() -> None:
    service, fetcher = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    service.inventory(datasources=["DS_SALES", "DS_SALES"])
    probes = [path for path, _ in fetcher.calls if _SAT_TX.lower() in path]
    assert len(probes) == 1


def test_the_probe_budget_is_bounded_and_says_when_it_binds() -> None:
    service, fetcher = _dispatching_service(
        {(_PFX_TX + "DS_A"): (200, _SATELLITE_ABAP)}, max_satellite_fetches=1
    )
    inventory = service.inventory(datasources=["DS_A", "DS_B", "DS_C"])
    probes = [path for path, _ in fetcher.calls if ("/" + _PFX_TX.lower()) in path]
    assert len(probes) == 1
    assert inventory.satellite_candidates_considered == 3
    assert any("budget of 1 request(s) was exhausted" in c for c in inventory.caveats)


def test_a_zero_budget_reads_no_satellite_at_all() -> None:
    service, fetcher = _dispatching_service(
        {_SAT_TX: (200, _SATELLITE_ABAP)}, max_satellite_fetches=0
    )
    service.inventory(datasources=["DS_SALES"])
    assert not [path for path, _ in fetcher.calls if ("/" + _PFX_TX.lower()) in path]


def test_no_candidate_list_means_no_probes_but_the_rule_is_reported() -> None:
    service, fetcher = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    inventory = service.inventory()
    assert not [path for path, _ in fetcher.calls if ("/" + _PFX_TX.lower()) in path]
    assert inventory.satellite_prefixes == [_PFX_TX]
    assert any((_PFX_TX + "<DATASOURCE>") in c for c in inventory.caveats)


def test_an_unreadable_naming_rule_is_stated_rather_than_passed_over() -> None:
    # Dynamic dispatch with no recoverable prefix. Saying nothing here would read as "there is
    # nothing else", which is the one conclusion the evidence rules out.
    opaque = (
        "FUNCTION f.\n  lv_prog = build_name( i_datasource ).\n"
        "  PERFORM x IN PROGRAM (lv_prog).\nENDFUNCTION."
    )
    service, _ = _service({"ZXRSAU01": (200, opaque)})
    inventory = service.inventory(datasources=["DS_SALES"])
    assert inventory.satellites == []
    caveats = " ".join(inventory.caveats)
    assert "not in the include" in caveats
    assert "satellite_program_prefixes" in caveats


def test_a_different_prefix_per_datasource_kind_stays_attributed_to_its_slot() -> None:
    """A site may name transaction-data and master-data satellites differently.

    Nothing has to be configured for that: each prefix is evidence from the slot whose dispatch
    produced it, so the two are reported separately instead of being pooled into one naming rule.
    """
    fetcher = ScriptedFetcher(
        {
            "ZXRSAU01": (200, _DISPATCHING_EXIT),
            "ZXRSAU02": (200, _DISPATCHING_EXIT_MD),
            _PFX_TX + "DS_TXN": (200, _SATELLITE_ABAP),
            _PFX_MD + "DS_ATTR": (200, _SATELLITE_ABAP),
        }
    )
    service = ExitAnalysisService(EccConnector(_profile(), fetcher))
    inventory = service.inventory(datasources=["DS_TXN", "DS_ATTR"])

    assert set(inventory.satellite_prefixes) == {_PFX_TX, _PFX_MD}
    by_program = {s.program_name: s for s in inventory.satellites if s.available}
    assert by_program[_PFX_TX + "DS_TXN"].dispatched_from == "transaction_data"
    assert by_program[_PFX_MD + "DS_ATTR"].dispatched_from == "master_data_attributes"


def test_each_slot_reports_only_the_prefix_it_dispatches_to() -> None:
    fetcher = ScriptedFetcher(
        {"ZXRSAU01": (200, _DISPATCHING_EXIT), "ZXRSAU02": (200, _DISPATCHING_EXIT_MD)}
    )
    service = ExitAnalysisService(EccConnector(_profile(), fetcher))
    slots = {s.data_kind: s for s in service.inventory().exits}
    assert slots["transaction_data"].satellite_prefixes == [_PFX_TX]
    assert slots["master_data_attributes"].satellite_prefixes == [_PFX_MD]


def test_a_partner_namespace_prefix_is_accepted() -> None:
    """The naming rule is whatever the site uses; a /PARTNER/ prefix is as valid as a Z one."""
    exit_abap = (
        "FUNCTION f.\n"
        "  CONCATENATE '/PARTNER/EXIT_' i_datasource INTO lv_prog.\n"
        "  PERFORM x IN PROGRAM (lv_prog).\nENDFUNCTION."
    )
    dynamic, prefixes = parse_dynamic_dispatch(exit_abap.splitlines())
    assert dynamic is True
    assert prefixes == ["/PARTNER/EXIT_"]
    assert satellite_program_name("/PARTNER/EXIT_", "DS_SALES") == "/PARTNER/EXIT_DS_SALES"


def test_no_prefix_convention_is_assumed_by_default() -> None:
    """Publishing this means never shipping one customer's naming rule as a default."""
    assert _profile().satellite_program_prefixes == []


def test_a_configured_prefix_supplements_the_derived_one() -> None:
    service, _ = _dispatching_service(
        {(_PFX_MD + "DS_SALES"): (200, _SATELLITE_ABAP)},
        satellite_program_prefixes=[_PFX_MD],
    )
    inventory = service.inventory(datasources=["DS_SALES"])
    assert inventory.satellite_prefixes == [_PFX_TX, _PFX_MD]
    configured = next(s for s in inventory.satellites if s.prefix == _PFX_MD)
    # A configured prefix carries no evidence of which DataSource kind it serves, so none is
    # claimed.
    assert configured.dispatched_from is None
    assert configured.available is True


def test_no_naming_rule_and_no_dynamic_dispatch_costs_nothing() -> None:
    service, fetcher = _service({"ZXRSAU01": (200, _EXIT_ABAP)})
    inventory = service.inventory(datasources=["DS_SALES"])
    assert inventory.satellites == []
    assert inventory.satellite_prefixes == []
    assert not [path for path, _ in fetcher.calls if ("/" + _PFX_TX.lower()) in path]


def test_satellite_provenance_names_the_program_and_omits_the_host() -> None:
    service, _ = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    inventory = service.inventory(datasources=["DS_SALES"])
    satellite = next(s for s in inventory.satellites if s.available)
    assert satellite.provenance is not None
    assert satellite.provenance.object_kind == "program"
    assert satellite.provenance.object_name == _SAT_TX
    assert "source.example.invalid" not in satellite.provenance.model_dump_json()


def test_satellite_risk_reaches_the_scenario_index() -> None:
    """The wiring that makes 9.6 report the satellite's risk against the DataSource."""
    service, _ = _dispatching_service({_SAT_TX: (200, _SATELLITE_ABAP)})
    inventory = service.inventory(datasources=["DS_SALES"])
    index = _exit_index(inventory)
    entries = index["DS_SALES"]
    assert [code_id for code_id, _ in entries] == [_SAT_TX]
    reads, per_record, resolved = _exit_risk(entries)
    assert resolved is True
    assert per_record == 1
    assert reads == ["tbl_partner", "tbl_rate"]


def test_an_absent_satellite_does_not_enter_the_risk_index() -> None:
    service, _ = _dispatching_service()
    index = _exit_index(service.inventory(datasources=["DS_NOTHING"]))
    assert "DS_NOTHING" not in index
