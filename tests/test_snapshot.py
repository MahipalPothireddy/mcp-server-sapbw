"""Snapshots and comparison.

Most of these tests exist because of a specific way a diff can be useless rather than wrong. A diff
that reports every object as changed on every run, or every DataSource as replaced between
environments, or a whole family as deleted because the other system could not report it, carries no
information at all - and it looks authoritative while doing so.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mcp_server_sapbw.core.snapshots import SnapshotStore, snapshot_file
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.snapshot import (
    VOLATILE_FIELDS,
    Snapshot,
    fingerprint_facts,
)
from mcp_server_sapbw.services.snapshot import SnapshotService, compare, families_for

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "dso_header": "RSDODSO",
    "adso_header": "RSOADSO",
    "cube_header": "RSDCUBE",
    "composite_header": "RSOHCPR",
    "chain_attr": "RSPCCHAINATTR",
    "datasource": "RSDS",
    "dtp": "RSBKDTP",
    "transformation": "RSTRAN",
}

# ODSOBJECT, ODSOTYPE, INFOAREA, BEXFL
_DSO = [("STAGE_DSO", "", "SALES", "X"), ("EDW_DSO", "", "SALES", "")]
# ADSONM, INFOAREA, WRITE_CHANGELOG
_ADSO = [("FIN_ADSO", "FIN", "X")]
# INFOCUBE, CUBETYPE, OBJSTAT, INFOAREA. RSDCUBE holds three object kinds behind one table -
# measured on the reference system: 74 'B', 67 'M', 2 'V'.
_CUBE = [
    ("SALES_CUBE", "B", "ACT", "SALES"),
    ("SALES_MP", "M", "ACT", "SALES"),
    ("SALES_VP", "V", "ACT", "SALES"),
]
# HCPRNM, OBJSTAT, INFOAREA
_CP = [("SALES_CP", "ACT", "SALES")]
# CHAIN_ID, OBJSTAT, ACTIVFL
_CHAIN = [("DAILY_LOAD", "ACT", "X")]
# DATASOURCE, LOGSYS, OBJSTAT, TYPE. RSDS is keyed on (DATASOURCE, LOGSYS), so one DataSource can
# hold several rows - 3 of 949 on the reference system. DS_SHARED is that case.
_DS = [
    ("DS_SALES", "SRCCLNT100", "ACT", "TRAN"),
    ("DS_SHARED", "SRCCLNT100", "ACT", "TRAN"),
    ("DS_SHARED", "OTHRCLNT200", "ACT", "TRAN"),
]
# DTP, SRC, SRCTLOGO, TGT, TGTTLOGO, UPDMODE, OBJSTAT
_DTP = [("DTP_1", "DS_SALES", "RSDS", "STAGE_DSO", "ODSO", "D", "ACT")]
# TRANID, SOURCETYPE, SOURCENAME, TARGETTYPE, TARGETNAME, OBJSTAT, STARTROUTINE, ENDROUTINE,
# EXPERT, GLBCODE, GLBCODE2. The slot is EXPERT, not EXPERTROUTINE - BW 7.50 rejected the latter.
# The DataSource source name carries its padded logical system, exactly as BW stores it.
_TRAN = [
    (
        "TR1",
        "RSDS",
        "DS_SALES              SRCCLNT100",
        "ODSO",
        "STAGE_DSO",
        "ACT",
        "C1",
        "",
        "",
        "",
        "",
    ),
    ("TR2", "ODSO", "STAGE_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
]


class ScriptedConnection:
    """Returns rows by physical table. Row shapes match the columns the service selects."""

    def __init__(self, overrides: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self.statements: list[str] = []
        self._rows: dict[str, list[tuple[Any, ...]]] = {
            "RSDODSO": list(_DSO),
            "RSOADSO": list(_ADSO),
            "RSDCUBE": list(_CUBE),
            "RSOHCPR": list(_CP),
            "RSPCCHAINATTR": list(_CHAIN),
            "RSDS": list(_DS),
            "RSBKDTP": list(_DTP),
            "RSTRAN": list(_TRAN),
        }
        self._rows.update(overrides or {})

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        for physical, rows in self._rows.items():
            if f'"{physical}"' in sql:
                return rows
        return []

    def statement_for(self, physical: str) -> str:
        """The statement issued against one table, so a test can assert what it filtered on."""
        matches = [sql for sql in self.statements if f'"{physical}"' in sql]
        assert matches, f"no statement was issued against {physical}"
        return matches[0]


def _capability(present: set[str] | None = None, release: str = "7.50") -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release=release,
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _capture(
    *,
    system: str = "qa",
    present: set[str] | None = None,
    release: str = "7.50",
    overrides: dict[str, list[tuple[Any, ...]]] | None = None,
    families: Sequence[str] | None = None,
) -> Snapshot:
    service = SnapshotService(ScriptedConnection(overrides), _capability(present, release))
    captured = service.capture(
        system=system, families=families or ["providers", "transformations", "chains"]
    )
    assert isinstance(captured, Snapshot)
    return captured


# --- capture ------------------------------------------------------------------------------


def test_captures_every_provider_kind_with_canonical_ids() -> None:
    snapshot = _capture(families=["providers"])
    assert {o.id for o in snapshot.objects} == {
        "dso:STAGE_DSO",
        "dso:EDW_DSO",
        "adso:FIN_ADSO",
        "infocube:SALES_CUBE",
        "multiprovider:SALES_MP",
        "virtualprovider:SALES_VP",
        "compositeprovider:SALES_CP",
    }
    assert snapshot.scope.per_family_counts["providers"] == 7


def test_the_cube_table_is_decoded_into_three_object_types() -> None:
    """RSDCUBE holds InfoCubes, MultiProviders and virtual providers behind one table.

    Typing them all `infocube` would mis-state 69 of the reference system's 143 objects and break
    the canonical id they share with every other tool.
    """
    by_name = {o.ref.name: o.ref.object_type for o in _capture(families=["providers"]).objects}
    assert by_name["SALES_CUBE"] == "infocube"
    assert by_name["SALES_MP"] == "multiprovider"
    assert by_name["SALES_VP"] == "virtualprovider"


def test_a_multiprovider_is_not_filtered_out_of_a_comparison() -> None:
    """The decode is only useful if the diff's type filter follows it."""
    diff = compare(_capture(families=["providers"]), _capture(families=["providers"]))
    assert diff.counts["unchanged"] == 7


def test_fingerprint_is_stable_across_captures() -> None:
    """Two readings of an unchanged system must produce identical fingerprints, or nothing works."""
    first, second = _capture(), _capture()
    assert {o.id: o.fingerprint for o in first.objects} == {
        o.id: o.fingerprint for o in second.objects
    }


def test_a_structural_change_moves_the_fingerprint() -> None:
    moved = _capture(overrides={"RSDODSO": [("STAGE_DSO", "", "FINANCE", "X"), *_DSO[1:]]})
    baseline = {o.id: o.fingerprint for o in _capture().objects}
    changed = {o.id: o.fingerprint for o in moved.objects}
    assert baseline["dso:STAGE_DSO"] != changed["dso:STAGE_DSO"]
    assert baseline["dso:EDW_DSO"] == changed["dso:EDW_DSO"]


def test_snapshot_records_what_produced_it() -> None:
    snapshot = _capture()
    assert snapshot.system == "qa"
    assert snapshot.bw_release == "7.50"
    assert snapshot.server_version
    assert snapshot.capability_digest
    assert snapshot.snapshot_id.startswith("qa-")


def test_transformations_yield_edges_as_well_as_objects() -> None:
    snapshot = _capture(families=["transformations"])
    assert {o.id for o in snapshot.objects} == {"transformation:TR1", "transformation:TR2"}
    assert {(e.src, e.dst) for e in snapshot.edges} == {
        ("datasource:DS_SALES", "dso:STAGE_DSO"),
        ("dso:STAGE_DSO", "infocube:SALES_CUBE"),
    }


def test_a_routine_is_fingerprinted_by_presence_not_by_code_id() -> None:
    """A code id changes on every re-activation, so fingerprinting it reports phantom changes."""
    baseline = {o.id: o.fingerprint for o in _capture(families=["transformations"]).objects}
    reactivated = _capture(
        families=["transformations"],
        overrides={
            "RSTRAN": [
                (
                    "TR1",
                    "RSDS",
                    "DS_SALES     SRCCLNT100",
                    "ODSO",
                    "STAGE_DSO",
                    "ACT",
                    "C9",
                    "",
                    "",
                    "",
                    "",
                ),
                _TRAN[1],
            ]
        },
    )
    assert (
        baseline["transformation:TR1"]
        == {o.id: o.fingerprint for o in reactivated.objects}["transformation:TR1"]
    )


def test_a_family_this_release_cannot_report_is_unavailable_not_empty() -> None:
    """Empty and unreportable are different: a diff would read empty as a wholesale deletion."""
    snapshot = _capture(present=set(_TABLES) - {"chain_attr"}, families=["providers", "chains"])
    assert "chains" in snapshot.scope.families_unavailable
    assert "chains" not in snapshot.scope.families
    assert any("absent from the snapshot rather than empty" in c for c in snapshot.caveats)


def test_row_cap_is_reported_not_silently_applied() -> None:
    service = SnapshotService(ScriptedConnection(), _capability())
    snapshot = service.capture(system="qa", families=["providers"], row_cap=1)
    assert isinstance(snapshot, Snapshot)
    assert snapshot.scope.truncated is True
    assert any("row cap bound" in c or "-row cap bound" in c for c in snapshot.caveats)


def test_unknown_family_is_rejected_with_the_valid_set() -> None:
    with pytest.raises(ValueError, match="unknown snapshot families"):
        families_for(["nonsense"])
    assert families_for(None)


# --- version filtering and key collisions -------------------------------------------------


def test_versioned_tables_outside_the_auto_injected_families_are_filtered() -> None:
    """The dialect injects `OBJVERS = 'A'` for RSD/RSO/RSZ/RSTRAN only.

    RSPCCHAINATTR and RSBKDTP are versioned but outside that set. Measured on the reference system:
    RSBKDTP holds 1,451 active rows against 9,462 in total, and RSPCCHAINATTR 1,463 rows for 1,115
    distinct chains. Without the filter a snapshot carries every version of every object, collapses
    them onto one id, and reports phantom changes on every run.
    """
    conn = ScriptedConnection()
    SnapshotService(conn, _capability()).capture(system="qa", families=["chains", "dtps"])
    assert "OBJVERS = 'A'" in conn.statement_for("RSPCCHAINATTR")
    assert "OBJVERS = 'A'" in conn.statement_for("RSBKDTP")


def test_auto_injected_families_are_not_filtered_twice() -> None:
    """A second explicit filter would be harmless but would hide which tables need one."""
    conn = ScriptedConnection()
    SnapshotService(conn, _capability()).capture(system="qa", families=["providers"])
    assert conn.statement_for("RSDODSO").count("OBJVERS = 'A'") == 1


def test_rows_sharing_a_name_are_merged_rather_than_overwriting_each_other() -> None:
    """RSDS is keyed on (DATASOURCE, LOGSYS); the snapshot identity is the name alone.

    Taking whichever row arrived last would make the result depend on row order, so two readings of
    one unchanged system would disagree - which is exactly what the live system did.
    """
    snapshot = _capture(families=["datasources"])
    by_name = {o.ref.name: o for o in snapshot.objects}
    assert set(by_name) == {"DS_SALES", "DS_SHARED"}
    assert by_name["DS_SHARED"].facts["LOGSYS"] == "OTHRCLNT200,SRCCLNT100"  # sorted, both kept
    assert any("more than one metadata row" in c for c in snapshot.caveats)


def test_a_merged_fingerprint_does_not_depend_on_row_order() -> None:
    first = _capture(families=["datasources"])
    reordered = _capture(families=["datasources"], overrides={"RSDS": list(reversed(_DS))})
    assert {o.id: o.fingerprint for o in first.objects} == {
        o.id: o.fingerprint for o in reordered.objects
    }


# --- volatility ---------------------------------------------------------------------------


def test_volatile_facts_never_reach_a_fingerprint() -> None:
    """Fingerprint a timestamp and everything is 'changed' every run - as good as saying nothing."""
    base = {"INFOAREA": "SALES", "ODSOTYPE": ""}
    with_volatile = {**base, "TIMESTMP": "20260101120000", "TSTPNM": "SOMEUSER"}
    assert fingerprint_facts(base) == fingerprint_facts(with_volatile)


def test_the_volatile_list_covers_the_usual_suspects() -> None:
    for field in ("TIMESTMP", "TSTPNM", "LASTUSED", "record_count"):
        assert field in VOLATILE_FIELDS


def test_fingerprint_ignores_fact_order() -> None:
    assert fingerprint_facts({"a": "1", "b": "2"}) == fingerprint_facts({"b": "2", "a": "1"})


# --- comparison ---------------------------------------------------------------------------


def test_identical_systems_compare_clean() -> None:
    diff = compare(_capture(), _capture())
    assert diff.identical is True
    assert diff.comparable is True
    assert diff.counts["changed"] == 0
    assert diff.counts["unchanged"] > 0


def test_an_added_object_is_reported_as_added() -> None:
    later = _capture(overrides={"RSDODSO": [*_DSO, ("NEW_DSO", "", "SALES", "")]})
    diff = compare(_capture(), later)
    assert [c.ref.id for c in diff.added] == ["dso:NEW_DSO"]
    assert not diff.removed


def test_a_removed_object_is_reported_as_removed() -> None:
    diff = compare(_capture(), _capture(overrides={"RSDODSO": _DSO[:1]}))
    assert [c.ref.id for c in diff.removed] == ["dso:EDW_DSO"]


def test_a_changed_object_names_the_facts_that_differ() -> None:
    """A diff that says only 'something changed' sends the reader back to the system."""
    moved = _capture(overrides={"RSDODSO": [("STAGE_DSO", "", "FINANCE", "X"), *_DSO[1:]]})
    diff = compare(_capture(), moved)
    assert [c.ref.id for c in diff.changed] == ["dso:STAGE_DSO"]
    facts = {f.fact: (f.before, f.after) for f in diff.changed[0].facts}
    assert facts == {"INFOAREA": ("SALES", "FINANCE")}


def test_edge_changes_are_reported_separately_from_object_changes() -> None:
    rerouted = _capture(
        overrides={
            "RSTRAN": [
                _TRAN[0],
                ("TR2", "ODSO", "EDW_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
            ]
        }
    )
    diff = compare(_capture(), rerouted)
    assert {(e.src, e.dst) for e in diff.added_edges} == {("dso:EDW_DSO", "infocube:SALES_CUBE")}
    assert {(e.src, e.dst) for e in diff.removed_edges} == {
        ("dso:STAGE_DSO", "infocube:SALES_CUBE")
    }


def test_the_normalisations_applied_are_stated() -> None:
    diff = compare(_capture(), _capture())
    joined = " ".join(diff.normalisations)
    assert "logical system" in joined
    assert "Timestamps" in joined


# --- the three ways a cross-environment diff goes wrong -----------------------------------


def test_a_bdls_logical_system_difference_is_not_a_replaced_datasource() -> None:
    """The logical system differs per environment. Comparing raw replaces every DataSource."""
    dev = _capture(families=["transformations"])
    prd = _capture(
        families=["transformations"],
        overrides={
            "RSTRAN": [
                (
                    "TR1",
                    "RSDS",
                    "DS_SALES              PRDCLNT200",
                    "ODSO",
                    "STAGE_DSO",
                    "ACT",
                    "C1",
                    "",
                    "",
                    "",
                    "",
                ),
                _TRAN[1],
            ]
        },
    )
    diff = compare(dev, prd)
    assert diff.identical is True, "the BDLS suffix leaked into the identity"
    assert {(e.src, e.dst) for e in dev.edges} == {(e.src, e.dst) for e in prd.edges}


def test_a_family_only_one_side_captured_is_excluded_not_diffed() -> None:
    """Otherwise its whole content reads as added or removed - a fact about the snapshots."""
    both = _capture(families=["providers", "chains"])
    one = _capture(present=set(_TABLES) - {"chain_attr"}, families=["providers", "chains"])
    diff = compare(both, one)
    assert diff.comparable is False
    assert "chains" in diff.families_excluded
    assert "chains" not in diff.families_compared
    assert not any(c.ref.object_type == "chain" for c in diff.removed)
    assert any("one side only" in c for c in diff.comparability)


def test_different_releases_are_flagged_as_limiting_the_comparison() -> None:
    diff = compare(_capture(release="7.40"), _capture(release="7.50"))
    assert diff.comparable is False
    assert any("different releases" in c for c in diff.comparability)


def test_differing_capabilities_name_what_each_side_lacks() -> None:
    diff = compare(_capture(), _capture(present=set(_TABLES) - {"composite_header"}))
    assert diff.comparable is False
    assert any("can report different metadata" in c for c in diff.comparability)


def test_truncation_on_either_side_is_carried_into_the_diff() -> None:
    service = SnapshotService(ScriptedConnection(), _capability())
    capped = service.capture(system="qa", families=["providers"], row_cap=1)
    assert isinstance(capped, Snapshot)
    diff = compare(capped, _capture(families=["providers"]))
    assert diff.truncated is True
    assert any("row cap" in c for c in diff.comparability)


# --- objects re-created under a new technical id -------------------------------------------


def test_a_transformation_rebuilt_under_a_new_id_is_matched_on_its_endpoints() -> None:
    """Measured live: of 1,269 and 1,271 transformations across two environments, 471 ids matched.

    A transformation built by hand in each system carries a different TRANID, so a plain set
    difference calls the same dataflow both an addition and a removal.
    """
    left = _capture(families=["transformations"])
    right = _capture(
        families=["transformations"],
        overrides={"RSTRAN": [_TRAN[0], ("TR9", *_TRAN[1][1:])]},
    )
    diff = compare(left, right)
    assert [c.ref.id for c in diff.removed] == ["transformation:TR2"]
    assert [c.ref.id for c in diff.added] == ["transformation:TR9"]
    assert len(diff.rekeyed) == 1
    pair = diff.rekeyed[0]
    assert (pair.before.name, pair.after.name) == ("TR2", "TR9")
    assert pair.matched_on == "dso:STAGE_DSO -> infocube:SALES_CUBE"
    assert diff.counts["rekeyed"] == 1
    assert diff.structurally_identical is True
    assert diff.identical is False  # the ids really do differ; that is not hidden


def test_a_rekeyed_pair_stays_in_added_and_removed() -> None:
    """`added` and `removed` remain the plain set difference; `rekeyed` is the interpretation."""
    diff = compare(
        _capture(families=["transformations"]),
        _capture(
            families=["transformations"], overrides={"RSTRAN": [_TRAN[0], ("TR9", *_TRAN[1][1:])]}
        ),
    )
    assert diff.counts["added"] == 1 and diff.counts["removed"] == 1
    assert any("different technical id" in c for c in diff.caveats)


def test_an_ambiguous_endpoint_pair_claims_no_rekey() -> None:
    """Eight source/target pairs on the reference system carry more than one transformation.

    With two candidates on each side there is no way to say which replaced which, so nothing is
    claimed rather than a guess being presented as a match.
    """
    left = _capture(
        families=["transformations"],
        overrides={
            "RSTRAN": [
                ("TR1", "ODSO", "STAGE_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
                ("TR2", "ODSO", "STAGE_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
            ]
        },
    )
    right = _capture(
        families=["transformations"],
        overrides={
            "RSTRAN": [
                ("TR8", "ODSO", "STAGE_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
                ("TR9", "ODSO", "STAGE_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
            ]
        },
    )
    diff = compare(left, right)
    assert diff.counts["added"] == 2 and diff.counts["removed"] == 2
    assert diff.rekeyed == []


def test_a_dtp_is_matched_on_its_endpoints_too() -> None:
    """DTP names are generated as well, so the same trap applies with SRC/TGT facts."""
    left = _capture(families=["dtps"])
    right = _capture(
        families=["dtps"],
        overrides={"RSBKDTP": [("DTP_9", "DS_SALES", "RSDS", "STAGE_DSO", "ODSO", "D", "ACT")]},
    )
    diff = compare(left, right)
    assert len(diff.rekeyed) == 1
    assert diff.rekeyed[0].matched_on == "datasource:DS_SALES -> dso:STAGE_DSO"


def test_a_dtp_endpoints_logical_system_is_a_fact_of_its_own() -> None:
    """One BW column holds two facts: the DataSource and the logical system it was extracted from.

    Measured: 988 of 1,451 active DTPs carry a padded source. Left joined, the BDLS rewrite between
    environments reports every one of them as changed and buries whatever really differs.
    """
    qa = _capture(
        families=["dtps"],
        overrides={
            "RSBKDTP": [("DTP_1", "DS_SALES     SRCCLNT100", "RSDS", "STAGE_DSO", "ODSO", "D", "A")]
        },
    )
    prd = _capture(
        families=["dtps"],
        overrides={
            "RSBKDTP": [("DTP_1", "DS_SALES     PRDCLNT200", "RSDS", "STAGE_DSO", "ODSO", "D", "A")]
        },
    )
    facts = {o.ref.name: o.facts for o in qa.objects}
    assert facts["DTP_1"]["SRC"] == "DS_SALES"
    assert facts["DTP_1"]["SRC_LOGSYS"] == "SRCCLNT100"

    diff = compare(qa, prd)
    # One changed fact, named - not a wholesale "this DTP is different".
    assert [f.fact for c in diff.changed for f in c.facts] == ["SRC_LOGSYS"]


def test_the_diff_says_how_many_objects_each_fact_accounts_for() -> None:
    """What separates a systematic difference from a real one.

    Measured across two real environments: 814 DataSources differing only on their logical system is
    BDLS doing its job; one of them differing that way would be a provider pointed at the wrong
    source. The per-object list carries the same information, but only after a reader counts it.
    """
    later = _capture(
        overrides={"RSDODSO": [("STAGE_DSO", "", "FINANCE", "X"), ("EDW_DSO", "", "FINANCE", "")]}
    )
    diff = compare(_capture(), later)
    assert diff.changed_by_fact == {"INFOAREA": 2}


def test_a_genuinely_new_flow_is_not_reported_as_a_rekey() -> None:
    left = _capture(families=["transformations"])
    right = _capture(
        families=["transformations"],
        overrides={
            "RSTRAN": [
                *_TRAN,
                ("TR3", "ODSO", "EDW_DSO", "CUBE", "SALES_CUBE", "ACT", "", "", "", "", ""),
            ]
        },
    )
    diff = compare(left, right)
    assert [c.ref.id for c in diff.added] == ["transformation:TR3"]
    assert diff.rekeyed == []
    assert diff.structurally_identical is False


# --- storage ------------------------------------------------------------------------------


def _store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(snapshot_file("qa", tmp_path))


def test_a_stored_snapshot_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = _capture()
    store.put(snapshot)
    loaded = store.get(snapshot.snapshot_id)
    assert loaded is not None
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert {o.id for o in loaded.objects} == {o.id for o in snapshot.objects}
    store.close()


def test_latest_returns_the_most_recent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    older = _capture()
    newer = _capture()
    newer.taken_at = older.taken_at + timedelta(hours=1)
    newer.snapshot_id = "qa-later"
    store.put(older)
    store.put(newer)
    latest = store.latest("qa")
    assert latest is not None and latest.snapshot_id == "qa-later"
    store.close()


def test_latest_before_a_moment_answers_what_changed_since_then(tmp_path: Path) -> None:
    """Lets a caller compare 'now' against 'the last one' without tracking ids."""
    store = _store(tmp_path)
    older = _capture()
    store.put(older)
    assert store.latest("qa", before=older.taken_at) is None
    assert store.latest("qa", before=older.taken_at + timedelta(seconds=1)) is not None
    store.close()


def test_listing_returns_summaries_not_payloads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_capture())
    listed = store.list(system="qa")
    assert len(listed) == 1
    assert listed[0].object_count > 0
    assert listed[0].families
    store.close()


def test_an_unreadable_stored_payload_reads_as_absent(tmp_path: Path) -> None:
    """A model change in a new server version must not turn an old snapshot into an error."""
    store = _store(tmp_path)
    snapshot = _capture()
    store.put(snapshot)
    # Reaching into the connection is the point: the payload has to be corrupted the way a model
    # change would corrupt it, which no public method can do.
    store._conn.execute(
        "UPDATE snapshots SET payload = ? WHERE snapshot_id = ?",
        ("{not json", snapshot.snapshot_id),
    )
    store._conn.commit()
    assert store.get(snapshot.snapshot_id) is None
    assert store.latest("qa") is None
    store.close()


def test_the_file_name_is_the_profile_alias_never_a_host(tmp_path: Path) -> None:
    path = snapshot_file("prd/../etc", tmp_path)
    assert path.parent == tmp_path
    assert "/" not in path.name and ".." not in path.name


def test_delete_and_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = _capture()
    store.put(snapshot)
    assert store.count(system="qa") == 1
    assert store.delete(snapshot.snapshot_id) is True
    assert store.delete(snapshot.snapshot_id) is False
    assert store.count() == 0
    store.close()
