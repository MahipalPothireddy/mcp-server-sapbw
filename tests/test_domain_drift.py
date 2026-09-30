"""The decode check: does the connected dictionary still declare what the source assumes (D44)?

Synthetic domains only. The registry under test is the real one, but the *dictionary* is scripted,
so each case states exactly which disagreement it is about.

The property that matters is not "does it find drift" - it is **which severity it assigns**. A check
that reports everything is as useless as one reporting nothing: on the reference system 147 of the
151 findings are ``info`` by design, because three registered decodes deliberately cover part of
their domain, and reporting those as defects would bury the four that are real.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.health import _RSPM_HOUSEKEEPING, _RSPM_STATUS_MAP
from mcp_server_sapbw.services.domain_drift import REGISTRY, DomainDriftService

SCHEMA = "TESTSCHEMA"
_TABLES = {"dict_domain_values": "DD07L", "dict_domain_text": "DD07T"}


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    available = set(_TABLES) if present is None else present
    return CapabilityRecord(
        system="qa",
        bw_release="BW 7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in available else None,
                present=logical in available,
                schema_name=SCHEMA if logical in available else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


class ScriptedDictionary:
    """A DD07L/DD07T stand-in. ``values`` is ``{domain: {code: text}}``."""

    def __init__(self, values: dict[str, dict[str, str]]) -> None:
        self._values = values

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        # The reader passes the language first when it can join the text table.
        domains = [str(p) for p in params if str(p) != "E"]
        rows: list[tuple[Any, ...]] = []
        for domain in domains:
            for code, text in self._values.get(domain, {}).items():
                rows.append((domain, code, text) if "DDTEXT" in sql else (domain, code))
        return rows


def _service(values: dict[str, dict[str, str]], present: set[str] | None = None) -> Any:
    return DomainDriftService(ScriptedDictionary(values), _capability(present), None)


def _findings(report: Any, domain: str) -> list[Any]:
    return [f for f in report.findings if f.domain == domain]


def _agreeing() -> dict[str, dict[str, str]]:
    """A dictionary that agrees with the source on every registered domain.

    The texts have to be the recorded ones, not blanks. A first version of this helper used empty
    strings and three tests failed on ``label_differs`` - which was the check being right and the
    fixture being lazy, so it is worth keeping the note: "agrees" means codes *and* texts.

    The same trap caught this helper a second time, from another direction. One domain can appear
    in the registry more than once under different owners: ``RSZ_OPERATOR_DOMAIN`` is decoded
    independently by the query reader and the security reader (D53), into different vocabularies,
    and
    only one records SAP's texts. Keyed naively by domain, the label-free entry overwrote the
    label-bearing one with blanks and the same three tests failed again. So labels are merged across
    entries, keeping the first non-empty one, and codes are unioned.
    """
    values: dict[str, dict[str, str]] = {}
    for entry in REGISTRY:
        bucket = values.setdefault(entry.domain, {})
        for code in entry.codes:
            label = (entry.labels or {}).get(code, "")
            if label or code not in bucket:
                bucket[code] = label
    return values


def test_a_dictionary_that_agrees_produces_no_finding_above_info() -> None:
    """Every registered domain, declared exactly as the source has it."""
    report = _service(_agreeing()).check()
    assert not isinstance(report, UnsupportedResult)
    assert report.clean, [f.detail for f in report.findings if f.severity != "info"]


def test_a_declared_value_the_source_does_not_map_is_high_severity() -> None:
    """The D24 failure mode, and the reason this check exists.

    An unmapped value does not raise. It lands in whatever fallback the decode has and comes back
    looking understood - which on the reference system once put 24.4% of a query's element-tree
    edges into a catch-all bucket. Verified live: this found 4 unmapped values of the request-status
    domain, on the only table an ADSO's load history lives in, affecting 19 of 217 providers.
    """
    complete = next(e for e in REGISTRY if e.claims_complete)
    values = _agreeing()
    values[complete.domain]["ZZ"] = "Something New"
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    finding = next(f for f in _findings(report, complete.domain) if f.code == "ZZ")
    assert finding.kind == "code_unmapped"
    assert finding.severity == "high"
    assert finding.actual == "Something New"
    assert finding.owner == complete.owner, "a finding must name where the fix goes"
    assert report.clean is False


def test_a_partial_decode_reports_its_gaps_as_info_not_as_defects() -> None:
    """Three registered decodes cover part of their domain on purpose.

    ``SUBDEFTP`` maps 4 of 14 because the rest fall through to the older heuristic (D26), and
    ``RSTLOGO`` maps 4 of 140 because the server reasons about four provider kinds. Reporting those
    as defects would have produced 147 findings on the reference system and buried the 4 real ones.
    """
    partial = next(e for e in REGISTRY if not e.claims_complete)
    values = _agreeing()
    values[partial.domain]["ZZ"] = "Unmapped"
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    finding = next(f for f in _findings(report, partial.domain) if f.code == "ZZ")
    assert finding.kind == "code_unmapped"
    assert finding.severity == "info"
    assert report.clean is True, "a deliberate partial mapping is not drift"


def test_a_code_the_dictionary_does_not_declare_is_reported_but_not_alarming() -> None:
    """The server legitimately maps codes SAP does not declare, and says so rather than hiding it.

    Measured on the reference system: ``RSZELTDIR.DEFTP`` uses ``CEL``, ``SHT`` and ``ATR`` while
    its domain declares eight values that exclude all three. The decode is right to cover them -
    they occur in data - but the label rests on observation, and that is worth stating.
    """
    entry = next(e for e in REGISTRY if e.claims_complete)
    kept = sorted(entry.codes)[0]
    values = _agreeing()
    # Only one of the source's codes survives in the dictionary; the rest become 'undeclared'.
    values[entry.domain] = {kept: values[entry.domain][kept]}
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    undeclared = [f for f in _findings(report, entry.domain) if f.kind == "code_undeclared"]
    assert undeclared, "a source code absent from the dictionary must be reported"
    assert {f.severity for f in undeclared} == {"info"}


def test_a_domain_missing_from_the_dictionary_is_the_loudest_finding() -> None:
    """If the domain is gone, every code the decode covers is unvalidated on this system."""
    report = _service({}).check()
    assert not isinstance(report, UnsupportedResult)
    absent = [f for f in report.findings if f.kind == "domain_absent"]
    assert len(absent) == len(REGISTRY)
    assert {f.severity for f in absent} == {"high"}
    assert report.domains_checked == 0
    assert all("bw_access_report" in f.detail for f in absent), (
        "a refused DD07L read and a release that lacks the domain look identical, so the finding "
        "has to point at the tool that tells them apart"
    )


def test_a_label_that_differs_is_low_and_only_where_sap_texts_are_recorded() -> None:
    """A changed text does not stop the decode, so it is not urgent - but a label shown as SAP's
    wording should be this system's wording."""
    entry = next(e for e in REGISTRY if e.labels)
    code, expected = next(iter(entry.labels.items()))  # type: ignore[union-attr]
    values = _agreeing()
    values[entry.domain][code] = "Something Else"
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    differs = [f for f in _findings(report, entry.domain) if f.kind == "label_differs"]
    finding = next(f for f in differs if f.code == code)
    assert finding.severity == "low"
    assert finding.expected == expected
    assert finding.actual == "Something Else"


def test_a_domain_decoded_by_two_mappings_is_not_reported_as_half_unmapped() -> None:
    """The checker's own first defect, and the reason ``expect`` takes a list of sources.

    A domain's values can be split across mappings **by purpose**. ``RSPM_REQUEST_STATUS`` is: nine
    codes are load outcomes and four are housekeeping states, because a deleted or moved request
    says nothing about whether data is present, so decoding it as a success or a failure would both
    be wrong - the health reader reports it as a caveat instead. Verified: 9 + 4 = 13, exactly the
    declared set, nothing missing either way.

    Registering only the outcome map reported the other four at ``high`` severity against carefully
    reasoned, correct code. **A validator that cries wolf costs more trust than it earns**, so this
    is the regression that matters most in this file.
    """
    partitioned = next(e for e in REGISTRY if e.domain == "RSPM_REQUEST_STATUS")
    assert " + " in partitioned.owner, "both source mappings must be named for the reader"
    values = _agreeing()
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    assert _findings(report, "RSPM_REQUEST_STATUS") == []

    # And the union really is the whole domain: dropping either source would reopen the false alarm.
    assert len(_RSPM_STATUS_MAP) + len(_RSPM_HOUSEKEEPING) == len(partitioned.codes)
    assert partitioned.codes >= {"D", "M", "X", ""}, "housekeeping states must be covered"
    assert partitioned.codes >= {"GG", "RR"}, "load outcomes must be covered"


def test_every_registered_domain_names_where_its_fix_would_go() -> None:
    """A finding is only actionable if it says which symbol to edit.

    Cheap to assert and easy to lose: a registry entry added without an owner produces findings that
    are true and unusable.
    """
    for entry in REGISTRY:
        assert entry.owner, entry.domain
        assert entry.column, entry.domain
        assert entry.codes, f"{entry.domain} registers no codes, so it can only report noise"


def test_the_report_always_carries_its_own_limits() -> None:
    """A clean report is a real result here, so what it does NOT prove has to travel with it."""
    report = _service(_agreeing()).check()
    assert not isinstance(report, UnsupportedResult)
    assert report.clean
    joined = " ".join(report.limitations).lower()
    assert "come to mean something different" in joined, "cannot detect a silent meaning change"
    assert "not the data" in joined, "declared-vs-in-use is a different question"
    assert "roughly fifty" in joined, "coverage of the registry itself must be stated"
    assert report.decoded_against, "a clean result means 'agrees with the reference system'"


def test_without_the_domain_table_the_check_declines_rather_than_passing() -> None:
    """The worst outcome would be a clean report because the check could not run."""
    report = _service({}, present=set()).check()
    assert isinstance(report, UnsupportedResult)


def test_labels_are_compared_only_where_the_source_records_sap_texts() -> None:
    """Many decodes map to the server's own vocabulary, not to SAP's text.

    ``RSZDEFTP`` ``'REP'`` becomes ``"query"``. Comparing that against "Query" would report a
    difference on every row and make the label check worthless, so those entries carry no labels and
    are checked for coverage only.
    """
    # Selected by domain, not by entry: a domain counts as vocabulary-only when *no* entry records
    # SAP's texts for it. RSZ_OPERATOR_DOMAIN is registered twice (D53) and only the query reader
    # records labels, so picking label-free entries alone pulled in a domain that is label-checked
    # through its other owner - the check being right about a fixture that mis-selected its case.
    labelled_domains = {e.domain for e in REGISTRY if e.labels}
    vocabulary_only = [e for e in REGISTRY if e.labels is None and e.domain not in labelled_domains]
    assert vocabulary_only, "the registry should contain vocabulary-mapping decodes"
    values = {
        e.domain: dict.fromkeys(e.codes, "SAP text differs entirely") for e in vocabulary_only
    }
    report = _service(values).check()
    assert not isinstance(report, UnsupportedResult)
    reported = {f.domain for f in report.findings if f.kind == "label_differs"}
    assert reported.isdisjoint({e.domain for e in vocabulary_only})
