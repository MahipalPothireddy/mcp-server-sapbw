"""Tests for the live BI connectors (BOBJ over BIPRWS, Tableau over its repository).

Entirely offline. The Tableau connector takes an injectable ``connect`` callable and the BOBJ
connector an injectable session, so neither test opens a socket.

**What these pin, and why each one exists.** Every case below corresponds to something that was
actually wrong in the first working version of this module, found by running it against a real
landscape and reading the output rather than by reasoning about the code:

* host names reached a payload through ``data_connections.caption``, which on the reference site is
  the database server's FQDN - so host scrubbing is asserted on the way out, not assumed;
* ``report_schedules`` named the *schedule* rather than the report, producing six identical
  "Monday morning" rows for six different reports, because ``tasks.title`` is null for
  subscriptions;
* a BW-generated HANA view path was reported verbatim as "(whole database <concatenated path>)";
* extracting that view name then silently reclassified all 11 rows as ``unknown``, because the
  classifier was matching a marker in the display string;
* the provider regex captured one letter, because ``re.IGNORECASE`` defeated its own uppercase
  boundary;
* an empty parameter tuple made psycopg treat ``LIKE '%conn%'`` as parameterised.

Every identifier here is synthetic and every host is an RFC-reserved documentation form, so
this file passes the customer-metadata and host-name leak checks it sits alongside.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from mcp_server_sapbw.connectors.external_bi import (
    HANA_OBJECT_GAP,
    BiConnectorError,
    BiprwsProbe,
    BobjConnector,
    TableauConnector,
    _BiprwsSession,
    _classify_http,
    _minutes_to_clock,
    _no_host,
    detect_sso,
    extract_bw_generated_view,
)
from mcp_server_sapbw.core.profiles import (
    BiPlatformProfile,
    _build_bi_platform_profile,
    _split_host_url,
)

# Synthetic samples that would themselves trip the customer-metadata and host-name leak
# checks. They live under tests/fixtures/, the one directory the check skips - see that
# module's docstring for why proving these guarantees needs realistic-looking values.
from .fixtures.bi_samples import (
    BW_VIEW_CASES,
    BW_VIEW_NAME,
    BW_VIEW_PATH,
    BW_VIEW_PROVIDER,
    HOSTS_TO_SCRUB,
    NAME_WITH_EMBEDDED_HOST,
    NAMES_TO_KEEP,
    PLAIN_DATABASE,
    PLAIN_TABLE,
    SCRUBBED_DOMAIN,
)

# --- fixtures -----------------------------------------------------------------------------------


def tableau_profile(**overrides: Any) -> BiPlatformProfile:
    defaults: dict[str, Any] = {
        "name": "tab",
        "kind": "tableau",
        "host": "repo.example.invalid",
        "port": 8060,
        "user": "reader",
        "password": SecretStr("pw"),  # pragma: allowlist secret
        "database": "workgroup",
    }
    return BiPlatformProfile(**{**defaults, **overrides})


def bobj_profile(**overrides: Any) -> BiPlatformProfile:
    defaults: dict[str, Any] = {
        "name": "boe",
        "kind": "bobj",
        "host": "boe.example.invalid",
        "port": 8443,
        "user": "reader",
        "password": SecretStr("pw"),  # pragma: allowlist secret
    }
    return BiPlatformProfile(**{**defaults, **overrides})


class FakeCursor:
    """Answers the connector's reads by matching on distinctive fragments of each statement."""

    def __init__(self, script: dict[str, list[tuple[Any, ...]]]) -> None:
        self._script = script
        self._rows: list[tuple[Any, ...]] = []
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        self.params = params
        for marker, rows in self._script.items():
            if marker in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeConnection:
    def __init__(self, script: dict[str, list[tuple[Any, ...]]]) -> None:
        self._script = script
        self.cursors: list[FakeCursor] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self._script)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


def tableau_with(script: dict[str, list[tuple[Any, ...]]]) -> tuple[
    TableauConnector, dict[str, Any]
]:
    """A connector wired to a scripted connection, plus the kwargs it was opened with."""
    seen: dict[str, Any] = {}

    def connect(**kwargs: Any) -> FakeConnection:
        seen.update(kwargs)
        return FakeConnection(script)

    return TableauConnector(tableau_profile(), connect=connect), seen


#: Present in every script: the capability probe reads information_schema then version().
def base_script(*tables: str) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "information_schema.tables": [(t,) for t in tables],
        "SELECT version()": [("PostgreSQL 15.6 on x86_64",)],
    }


ALL_TABLES = (
    "_schedules",
    "tasks",
    "_workbooks",
    "_datasources",
    "data_connections",
    "subscriptions",
)


# --- profile model ------------------------------------------------------------------------------


def test_tableau_profile_requires_a_database() -> None:
    """Defaulting it would risk reading the wrong database and reporting it as authoritative."""
    with pytest.raises(ValueError, match="needs 'database'"):
        tableau_profile(database=None)


def test_plain_http_needs_an_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_plain_http"):
        bobj_profile(use_tls=False)


def test_plain_http_is_allowed_when_opted_in() -> None:
    profile = bobj_profile(use_tls=False, allow_plain_http=True)
    assert profile.scheme == "http"


def test_a_profile_of_the_wrong_kind_is_refused_by_each_connector() -> None:
    """A kind mismatch is a configuration error, not something to paper over with a default."""
    with pytest.raises(BiConnectorError, match="not 'bobj'"):
        BobjConnector(tableau_profile())
    with pytest.raises(BiConnectorError, match="not 'tableau'"):
        TableauConnector(bobj_profile())


# --- read-only enforcement ----------------------------------------------------------------------


def test_the_session_is_opened_read_only() -> None:
    """The enforcement, asserted rather than trusted: PostgreSQL must be told before any statement.

    Verified live too - a CREATE against the real repository is refused with SQLSTATE 25006 - but
    that check cannot run in CI, so the startup option itself is pinned here.
    """
    connector, opened = tableau_with(base_script(*ALL_TABLES))
    connector.probe()
    assert opened["options"] == "-c default_transaction_read_only=on"


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE t (x int)",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "TRUNCATE t",
        "GRANT ALL ON t TO public",
        "  update t set x = 1",
    ],
)
def test_non_read_statements_are_refused_before_the_driver(statement: str) -> None:
    connector, _ = tableau_with(base_script(*ALL_TABLES))
    with pytest.raises(BiConnectorError, match="refused a non-read statement"):
        connector.query(statement)


@pytest.mark.parametrize(
    "statement", ["SELECT 1", "select 1", "WITH a AS (SELECT 1) SELECT * FROM a"]
)
def test_reads_are_allowed(statement: str) -> None:
    connector, _ = tableau_with(base_script(*ALL_TABLES))
    connector.query(statement)


def test_an_unparameterised_statement_passes_none_not_an_empty_tuple() -> None:
    """psycopg reads ``%`` as a placeholder whenever *any* params argument is given.

    Passing ``()`` made an unparameterised ``LIKE '%conn%'`` fail with a ProgrammingError that said
    nothing about percent signs. One confusing failure, pinned so it cannot come back.
    """
    connector, _ = tableau_with(base_script(*ALL_TABLES))
    connector.query("SELECT 1 WHERE x LIKE '%conn%'")
    conn = connector._connection()  # asserting what actually reached the driver
    assert conn.cursors[-1].params is None


# --- capability probe ---------------------------------------------------------------------------


def test_the_probe_resolves_present_views_and_reports_absent_roles() -> None:
    connector, _ = tableau_with(base_script("_schedules", "tasks", "data_connections"))
    probe = connector.probe()
    assert probe.reachable is True
    assert probe.resolved["schedules"] == "_schedules"
    assert probe.resolved["connections"] == "data_connections"
    assert "workbooks" not in probe.resolved
    caveats = connector.caveats()
    assert any("not found for role" in c for c in caveats)


def test_an_absent_view_yields_an_empty_answer_rather_than_a_raised_query() -> None:
    """Tableau renames repository objects between versions; an absent one is a gap, not a crash."""
    connector, _ = tableau_with(base_script("tasks"))  # no _schedules
    assert connector.report_schedules() == []
    assert connector.dashboard_sources() == []


# --- report schedules ---------------------------------------------------------------------------

_SCHEDULE_SCRIPT: dict[str, list[tuple[Any, ...]]] = {
    **base_script(*ALL_TABLES),
    # (type, title, obj_type, obj_id, schedule name, schedule_type, action, start_at_minute, active)
    "JOIN _schedules": [
        ("RefreshExtractTask", None, "Workbook", 11, "Nightly", 1, 0, 390, True),
        ("SingleSubscriptionTask", None, None, 77, "Monday morning", 2, 1, 60, True),
        ("SingleSubscriptionTask", None, None, 78, "Monday morning", 2, 1, 60, True),
    ],
    "SELECT id, name FROM _workbooks": [(11, "Demo Margin Dashboard")],
    "SELECT id, name FROM _datasources": [],
    "SELECT id, subject FROM subscriptions": [
        (77, "Weekly regional summary"),
        (78, "Weekly national summary"),
    ],
}


def test_a_schedule_names_the_report_not_the_shared_schedule() -> None:
    """The defect this replaces produced N identical rows for N different reports."""
    connector, _ = tableau_with(_SCHEDULE_SCRIPT)
    names = [s.name for s in connector.report_schedules()]
    assert names == [
        "Demo Margin Dashboard",
        "subscription: Weekly regional summary",
        "subscription: Weekly national summary",
    ]
    # The shared schedule name must not be the entry's identity.
    assert "Monday morning" not in names


def test_a_subscription_resolves_through_the_extra_hop() -> None:
    """849 of 1,143 scheduled tasks on the reference site are subscriptions with a NULL obj_type."""
    connector, _ = tableau_with(_SCHEDULE_SCRIPT)
    subs = [s for s in connector.report_schedules() if s.provider is None]
    assert len(subs) == 2
    assert all(s.name.startswith("subscription: ") for s in subs)


def test_an_unresolvable_object_says_so_rather_than_being_dropped() -> None:
    script = {**_SCHEDULE_SCRIPT, "SELECT id, subject FROM subscriptions": []}
    connector, _ = tableau_with(script)
    labels = [s.name for s in connector.report_schedules()]
    assert any(label.startswith("unresolved") for label in labels)


def test_raw_schedule_codes_are_carried_but_not_decoded() -> None:
    """The repository ships no text for these integers, and a name is not evidence.

    The standing rule from the BW side: a chain named "6 AM CST" runs at 05:30. The codes are
    reported verbatim and labelled raw so a reader can correlate them without being told a meaning
    this server cannot support.
    """
    connector, _ = tableau_with(_SCHEDULE_SCRIPT)
    first = connector.report_schedules()[0]
    assert "raw schedule_type=1" in (first.frequency or "")
    assert "scheduled_action=0" in (first.frequency or "")
    assert "RefreshExtractTask" in (first.frequency or "")


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "00:00"), (60, "01:00"), (390, "06:30"), (1439, "23:59"), (1440, None), (-1, None),
     (None, None), ("x", None)],
)
def test_minutes_past_midnight_render_as_a_clock(minutes: Any, expected: str | None) -> None:
    assert _minutes_to_clock(minutes) == expected


def test_housekeeping_tasks_are_excluded_from_report_schedules() -> None:
    """A temp-directory cleanup is not a report, and padding 9.7 with maintenance jobs the
    BW team cannot act on makes the timeline unreadable.
    """
    connector, _ = tableau_with(_SCHEDULE_SCRIPT)
    connector.report_schedules()
    sql = " ".join(connector._connection().cursors[-1].executed)
    assert "t.type IN" in sql


# --- dashboard sources and classification -------------------------------------------------------


_CONNECTION_SCRIPT: dict[str, list[tuple[Any, ...]]] = {
    **base_script(*ALL_TABLES),
    # (owner_type, owner_id, dbclass, db_subclass, dbname, tablename, has_extract)
    "FROM data_connections c": [
        ("Workbook", 11, "sqlproxy", None, BW_VIEW_PATH, None, False),
        ("Workbook", 11, "saphana", None, None, None, False),
        ("Datasource", 22, "sqlserver", None, PLAIN_DATABASE, PLAIN_TABLE, True),
        ("Workbook", 99, "hyper", None, "extractdb", "Extract", True),
    ],
    "SELECT id, name FROM _workbooks": [(11, "Demo Margin Dashboard")],
    "SELECT id, name FROM _datasources": [(22, "Demo Sales Datasource")],
    "SELECT id, subject FROM subscriptions": [],
}


def test_a_bw_generated_view_is_parsed_into_a_usable_object_name() -> None:
    """The path is reported as a BW object, so the analyzer can join it to the query subsystem."""
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    first = connector.dashboard_sources()[0]
    assert first.name == "Demo Margin Dashboard"
    assert first.source_object.startswith(BW_VIEW_NAME)
    assert f"provider {BW_VIEW_PROVIDER}" in first.source_object
    assert first.source_kind == "calc_view"


def test_classification_survives_a_change_to_the_display_string() -> None:
    """The regression that improving the object name introduced.

    The classifier had been matching the ``_SYS_BIC`` marker inside the formatted display text, so
    extracting the view name out of the path reclassified every one of these rows as ``unknown``. A
    presentation change must not be able to move a classification, which is why the kind is derived
    from the raw columns.
    """
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    calc_views = [d for d in connector.dashboard_sources() if d.source_kind == "calc_view"]
    assert len(calc_views) == 1
    # The marker is deliberately NOT in the reported object name any more.
    assert "_SYS_BIC" not in calc_views[0].source_object


def test_a_direct_hana_connection_recording_no_object_is_reported_as_unknown() -> None:
    """Not guessed. 360 such rows on the reference site carry no dbname and no tablename."""
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    hana = [d for d in connector.dashboard_sources() if d.connection == "saphana"]
    assert len(hana) == 1
    assert hana[0].source_kind == "unknown"
    assert hana[0].source_object == "(not recorded)"


def test_a_database_name_is_never_reported_as_the_source_object() -> None:
    """Reading `dbname` as an object was why 2,449 of 2,460 rows classified as unknown at first."""
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    hyper = [d for d in connector.dashboard_sources() if d.source_object == "Extract"]
    assert len(hyper) == 1


def test_the_connection_field_carries_the_class_never_the_server() -> None:
    """A finding needs "this reads HANA directly". A host is not part of that answer and
    must never appear in one.
    """
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    for source in connector.dashboard_sources():
        assert source.connection in {"sqlproxy", "saphana", "sqlserver", "hyper", None}
    sql = " ".join(connector._connection().cursors[-1].executed)
    # The server column is not merely unused - it is never fetched.
    assert "c.server" not in sql
    assert "c.password" not in sql
    assert "SELECT *" not in sql


def test_an_unresolvable_owner_is_labelled_rather_than_hidden() -> None:
    connector, _ = tableau_with(_CONNECTION_SCRIPT)
    unresolved = [d for d in connector.dashboard_sources() if d.name.startswith("unresolved")]
    assert len(unresolved) == 1  # workbook 99 has no row in _workbooks


# --- the BW generated-view parser ---------------------------------------------------------------


# Each case pairs a dbname with what must be parsed out of it. Three carry no marker and
# must therefore claim nothing - a parser that guesses is worse than one that declines.
@pytest.mark.parametrize(("dbname", "view", "provider"), BW_VIEW_CASES)
def test_generated_view_extraction(
    dbname: str | None, view: str | None, provider: str | None
) -> None:
    assert extract_bw_generated_view(dbname) == (view, provider)


def test_the_provider_match_is_case_sensitive() -> None:
    """With ``re.IGNORECASE`` the uppercase boundary matched lowercase too, so the lazy
    quantifier stopped at once and every provider came back as the single letter ``Z``.
    The boundary *is* the case change, so it cannot be expressed case-insensitively.
    """
    _, provider = extract_bw_generated_view(BW_VIEW_PATH)
    assert provider == BW_VIEW_PROVIDER
    assert provider != "Z"


# --- host scrubbing -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", HOSTS_TO_SCRUB)
def test_host_shaped_values_are_scrubbed(value: str) -> None:
    """A BI repository is full of hosts and they are not confined to a column called `server`."""
    scrubbed = _no_host(value)
    assert scrubbed is not None
    assert "<host withheld>" in scrubbed


@pytest.mark.parametrize("value", NAMES_TO_KEEP)
def test_ordinary_names_are_left_alone(value: str) -> None:
    assert _no_host(value) == value


def test_scrubbing_is_applied_to_every_emitted_name() -> None:
    """Asserted on the output, because the leak came in through a column nobody suspected."""
    script = {
        **_CONNECTION_SCRIPT,
        "SELECT id, name FROM _workbooks": [(11, NAME_WITH_EMBEDDED_HOST)],
    }
    connector, _ = tableau_with(script)
    names = [d.name for d in connector.dashboard_sources()]
    assert not any(SCRUBBED_DOMAIN in n for n in names)
    assert any("<host withheld>" in n for n in names)


# --- status and caveats -------------------------------------------------------------------------


def test_status_reports_that_the_account_is_not_read_only_by_grant() -> None:
    """Recorded, not glossed: nobody should read the security notes and assume more than is true."""
    connector, _ = tableau_with(base_script(*ALL_TABLES))
    detail = connector.status().detail
    assert "NOT read-only by grant" in detail
    assert "25006" in detail


def test_status_reports_a_locked_down_account_differently() -> None:
    connector = TableauConnector(tableau_profile(read_only_by_grant=True))
    assert "is read-only by grant" in connector.status().detail


def test_status_never_contains_the_host_or_password() -> None:
    for connector in (
        TableauConnector(tableau_profile()),
        BobjConnector(bobj_profile()),
    ):
        detail = connector.status().detail
        assert "example.invalid" not in detail
        assert "pw" not in detail.split()


def test_an_unconfigured_connector_says_how_to_configure_it_not_that_it_is_deferred() -> None:
    for connector, key in ((TableauConnector(), "tableau"), (BobjConnector(), "bobj")):
        status = connector.status()
        assert status.configured is False
        assert "deferred" not in status.detail
        assert f"kind: {key}" in status.detail


def test_the_hana_object_gap_is_scoped_to_direct_connections() -> None:
    """The first version of this statement was a blanket claim and was wrong for sqlproxy rows."""
    assert "saphana" in HANA_OBJECT_GAP
    assert "sqlproxy" in HANA_OBJECT_GAP


def test_caveats_always_state_the_timezone_mismatch() -> None:
    """Tableau stores site-local wall clock and BW chain logs are UTC."""
    connector, _ = tableau_with(base_script(*ALL_TABLES))
    assert any("UTC" in c for c in connector.caveats())


def test_caveats_report_the_hana_population_when_there_is_one() -> None:
    script = {
        **base_script(*ALL_TABLES),
        "SELECT count(*) FROM data_connections WHERE lower": [(360,)],
        "position(": [(11,)],
    }
    connector, _ = tableau_with(script)
    caveats = connector.caveats()
    assert any("360 connection(s) read SAP HANA directly" in c for c in caveats)


def test_an_unreachable_repository_yields_one_honest_caveat() -> None:
    def connect(**_: Any) -> FakeConnection:
        raise OSError("refused")

    connector = TableauConnector(tableau_profile(), connect=connect)
    probe = connector.probe()
    assert probe.reachable is False
    assert connector.caveats() == [f"Tableau repository was not read: {probe.detail}"]
    # And the failure text carries no connection detail (mission Rule 5).
    assert "example.invalid" not in probe.detail


# --- BOBJ ---------------------------------------------------------------------------------------


class FakeBiprwsSession:
    def __init__(self, probe: Any, payload: Any = None) -> None:
        self._probe = probe
        self._payload = payload
        self.closed = False

    def logon(self) -> Any:
        return self._probe

    def get(self, path: str, params: Any = None) -> Any:
        return self._payload

    def close(self) -> None:
        self.closed = True


def test_bobj_reports_an_unauthenticated_probe_rather_than_an_empty_schedule_list() -> None:
    probe = BiprwsProbe("/biprws", 401, False, "reachable, credentials rejected (401)")
    connector = BobjConnector(bobj_profile(), session=FakeBiprwsSession(probe))
    assert connector.report_schedules() == []
    assert connector.probe().authenticated is False
    assert "401" in connector.probe().detail


def test_bobj_parses_the_collection_shapes_biprws_uses() -> None:
    probe = BiprwsProbe("/biprws", 200, True, "logged on", "4.3")
    payload = {
        "entries": [
            {"name": "Demo Daily Sales", "nextRunTime": "06:30", "recurrence": "Daily"},
            {"SI_NAME": "Demo Monthly Close", "startTime": "01:00", "owner": "someone"},
            {"missing": "no name at all"},
        ]
    }
    connector = BobjConnector(bobj_profile(), session=FakeBiprwsSession(probe, payload))
    schedules = connector.report_schedules()
    assert [s.name for s in schedules] == ["Demo Daily Sales", "Demo Monthly Close"]
    assert schedules[0].scheduled_start == "06:30"
    assert connector.platform() == "SAP BusinessObjects 4.3"


def test_bobj_reports_no_dashboard_sources_rather_than_approximating_them() -> None:
    """A universe name cannot answer "does this bypass BW", so nothing is claimed."""
    probe = BiprwsProbe("/biprws", 200, True, "logged on")
    connector = BobjConnector(bobj_profile(), session=FakeBiprwsSession(probe, {}))
    assert connector.dashboard_sources() == []


# --- BIPRWS root discovery and health (added after the live 500) ---------------------------------
#
# The reference landscape answers a credential-free GET on /biprws with HTTP 500, and 404 on
# /BOE/biprws. That combination is informative -- deployed at the first path, not merged into BOE,
# so a pre-SP03 4.x layout with a broken web application -- and none of it is reachable by POSTing
# credentials and reading the failure. These tests pin the discovery order, the health gate, and the
# rule that a password is never sent at an endpoint already known to be unhealthy.


class RecordingHttpClient:
    """Records every request and answers from an **exact** ``(method, url)`` map.

    Keyed on the whole URL, not a substring. The first version matched suffixes, and
    ``"/biprws/logon/long"`` is a substring of ``"/BOE/biprws/logon/long"`` - so the root that was
    supposed to 404 answered 200 and the root-discovery test asserted the wrong thing. That is the
    same loose-matcher mistake that has twice certified a real gap as covered in this project, in a
    fixture this time rather than in a probe.
    """

    def __init__(self, responses: dict[tuple[str, str], tuple[int, dict[str, str]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def _answer(self, method: str, url: str) -> Any:
        self.calls.append((method, url))
        found = self._responses.get((method, url))
        # Unmapped means "nothing deployed here", which is what a real BIPRWS 404 says.
        return _Response(*found) if found else _Response(404, {})

    def get(self, url: str, params: Any = None, headers: Any = None) -> Any:
        return self._answer("GET", url)

    def post(self, url: str, json: Any = None, content: Any = None, headers: Any = None) -> Any:
        return self._answer("POST", url)

    def close(self) -> None:
        return None


class _Response:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status_code = status
        self.headers = headers
        self.text = ""

    def json(self) -> Any:
        return {}


def session_with(client: RecordingHttpClient, **profile_kwargs: Any) -> Any:
    """A session wired to a fake transport: no socket, no TLS, no optional dependency."""
    session = _BiprwsSession(bobj_profile(**profile_kwargs))
    session._client = client
    return session


def session_for(profile: BiPlatformProfile, client: RecordingHttpClient) -> Any:
    """Same, for a profile built elsewhere (e.g. one carrying a URL in its host field)."""
    session = _BiprwsSession(profile)
    session._client = client
    return session


def test_a_credential_free_get_gates_the_logon() -> None:
    """The endpoint is proved healthy before any password is sent."""
    healthy = probe_url("/BOE/biprws")
    client = RecordingHttpClient(
        {
            ("GET", healthy): (200, {}),
            ("POST", healthy): (200, {"X-SAP-LogonToken": "t"}),
        }
    )
    probe = session_with(client).logon()
    assert probe.authenticated is True
    methods = [m for m, _ in client.calls]
    # The GET precedes the POST, and there is exactly one of each per root tried.
    assert methods[0] == "GET"
    assert methods.count("POST") == 1


def test_a_failing_endpoint_never_receives_the_credentials() -> None:
    """The point of the health gate: a 500 root is broken for every request."""
    client = RecordingHttpClient({("GET", probe_url("/BOE/biprws")): (500, {})})
    probe = session_with(client).logon()
    assert probe.authenticated is False
    assert probe.status == 500
    assert "failing to initialise" in probe.detail
    assert not any(method == "POST" for method, _ in client.calls)


def test_a_404_root_moves_on_to_the_next_layout() -> None:
    """BIPRWS was folded into the BOE web application in BI 4.3 SP03, so both are tried."""
    legacy = probe_url("/biprws")
    client = RecordingHttpClient(
        {("GET", legacy): (200, {}), ("POST", legacy): (200, {"X-SAP-LogonToken": "t"})}
    )
    probe = session_with(client).logon()
    # /BOE/biprws is tried first and 404s (unmapped), then the legacy /biprws layout succeeds.
    assert probe.root == "/biprws"
    assert client.calls[0] == ("GET", probe_url("/BOE/biprws"))
    assert probe.authenticated is True


def test_a_broken_root_stops_the_search_rather_than_trying_the_rest() -> None:
    """A non-404 means this IS the right path and it is broken; the other layout cannot help."""
    client = RecordingHttpClient({("GET", probe_url("/BOE/biprws")): (500, {})})
    probe = session_with(client).logon()
    assert probe.root == "/BOE/biprws"
    # The legacy layout is never tried: a non-404 means this path IS BIPRWS and it is broken.
    assert probe_url("/biprws") not in [url for _, url in client.calls]


def test_an_explicit_base_path_skips_the_search() -> None:
    """Pinning the root is allowed; it just means the version probe is not worth paying for."""
    client = RecordingHttpClient({("GET", probe_url("/biprws")): (500, {})})
    probe = session_with(client, base_path="/biprws").logon()
    assert probe.root == "/biprws"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "credentials rejected"),
        (403, "not permitted"),
        (404, "not deployed at this path"),
        (500, "failing to initialise"),
        (503, "failing to initialise"),
        (418, "unexpected HTTP status 418"),
    ],
)
def test_each_failure_mode_is_named_distinctly(status: int, fragment: str) -> None:
    """Four different problems with four different owners must not collapse into one."""
    assert fragment in _classify_http(status)


def probe_url(root: str) -> str:
    """The URL the session builds for a root, so the assertion above is not a copy of the code."""
    return bobj_profile().base_url(f"{root.strip('/')}/logon/long")


# --- a URL where a host name is expected --------------------------------------------------------
#
# What an operator has to hand is the URL they use in a browser, so that is what lands in the
# environment variable. Refusing it would be technically correct and would send them re-typing
# information already present. The rule that earns its keep is the *path* rule: /BOE/BI is the
# launch pad, and adopting it as the API root aims the connector at HTML.


@pytest.mark.parametrize(
    ("value", "host", "port", "tls", "base_path"),
    [
        # A bare host passes through untouched, so no existing configuration changes shape.
        ("repo.example.invalid", "repo.example.invalid", None, None, None),
        # A full URL contributes host, port and scheme.
        ("https://boe.example.invalid:8443/", "boe.example.invalid", 8443, True, None),
        # The launch-pad path is discarded rather than adopted - the case that actually occurred.
        ("https://boe.example.invalid:8443/BOE/BI", "boe.example.invalid", 8443, True, None),
        ("https://boe.example.invalid:8443/BOE/portal", "boe.example.invalid", 8443, True, None),
        # A real API path is kept.
        (
            "https://boe.example.invalid:8443/biprws",
            "boe.example.invalid",
            8443,
            True,
            "/biprws",
        ),
        # Default ports are derived from the scheme when none is given.
        ("https://boe.example.invalid", "boe.example.invalid", 443, True, None),
        ("http://boe.example.invalid", "boe.example.invalid", 80, False, None),
    ],
)
def test_a_url_is_split_rather_than_refused(
    value: str, host: str, port: int | None, tls: bool | None, base_path: str | None
) -> None:
    assert _split_host_url(value) == (host, port, tls, base_path)


def url_profile(url: str, **extra: Any) -> BiPlatformProfile:
    """Build a profile the way the loader does, so the URL splitting is actually exercised.

    Constructing ``BiPlatformProfile`` directly bypasses it - the splitting lives in the builder,
    alongside the ``${VAR}`` resolution, which is where the ECC profile parses too. A
    first version of these tests asserted against a directly-constructed model and so
    proved nothing.
    """
    raw: dict[str, Any] = {
        "kind": "bobj",
        "host": url,
        "port": 6405,  # deliberately contradicts the URL, so precedence is observable
        "user": "reader",
        "password": "${PW}",
        **extra,
    }
    return _build_bi_platform_profile("boe", raw, {"PW": "pw"})  # pragma: allowlist secret


def test_a_url_port_overrides_the_profile_port() -> None:
    """A URL saying :8443 and a profile saying 6405 is a contradiction; the URL is more specific."""
    profile = url_profile("https://boe.example.invalid:8443/BOE/BI")
    assert profile.host == "boe.example.invalid"
    assert profile.port == 8443
    assert profile.use_tls is True
    # And the launch-pad path did not become the service root.
    assert profile.base_path is None


def test_a_bare_host_still_uses_the_configured_port() -> None:
    """No behaviour changes for a configuration that was already a host name."""
    profile = url_profile("boe.example.invalid")
    assert profile.host == "boe.example.invalid"
    assert profile.port == 6405


def test_an_explicit_base_path_wins_over_one_found_in_a_url() -> None:
    """Pinning the API root is the operator being deliberate; a URL path is usually a copied browser
    address."""
    profile = url_profile("https://boe.example.invalid:8443/biprws", base_path="/BOE/biprws")
    assert profile.base_path == "/BOE/biprws"


def test_a_launch_pad_url_does_not_become_the_service_root() -> None:
    """Asserted through the connector, because that is where the consequence would land."""
    profile = url_profile("https://boe.example.invalid:8443/BOE/BI")
    client = RecordingHttpClient({})
    session_for(profile, client).logon()
    # With no base_path pinned, both candidate roots are probed and neither is /BOE/BI.
    tried = [url for _, url in client.calls]
    assert tried, "the session should have probed at least one root"
    assert not any("/BOE/BI/" in url for url in tried)


# --- single sign-on detection -------------------------------------------------------------------
#
# A BOBJ deployment behind SAML answers a browser and refuses a password, and those two facts look
# exactly like a broken endpoint unless the redirect target is read. Detecting it changes the advice
# from "fix your credentials" to "you need Trusted Authentication", which is a different job.


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("https://idp.example.invalid/abc-123/saml2?SAMLRequest=abc", "SAML"),
        ("https://idp.example.invalid/saml/login", "SAML"),
        ("https://login.microsoftonline.com/tenant/saml2?SAMLRequest=x", "SAML"),
        ("https://login.microsoftonline.com/tenant/oauth2/authorize", "Microsoft Entra ID"),
        ("https://idp.example.invalid/adfs/ls/?wa=wsignin1.0", "AD FS"),
        ("https://idp.example.invalid/oauth2/authorize", "OAuth2 / OpenID Connect"),
        ("https://idp.example.invalid/sso/redirect", "federated single sign-on"),
        # Not an SSO hand-off: an ordinary relocation must not be reported as one.
        ("https://boe.example.invalid:8443/biprws/v1", None),
        ("", None),
    ],
)
def test_sso_mechanisms_are_named_from_the_redirect_target(
    location: str, expected: str | None
) -> None:
    assert detect_sso(location) == expected


def test_an_sso_redirect_is_reported_as_sso_not_as_a_broken_endpoint() -> None:
    """The distinction the detection exists to make, asserted end to end."""
    root = probe_url("/BOE/biprws")
    client = RecordingHttpClient(
        {("GET", root): (302, {"location": "https://idp.example.invalid/t/saml2?SAMLRequest=x"})}
    )
    probe = session_with(client).logon()
    assert probe.authenticated is False
    assert probe.status == 302
    assert "SAML" in probe.detail
    assert "Trusted Authentication" in probe.detail
    # No password was sent at an endpoint that cannot accept one.
    assert not any(method == "POST" for method, _ in client.calls)


def test_an_sso_redirect_never_quotes_the_identity_provider_url() -> None:
    """An IdP URL carries a tenant identifier and a host; the mechanism name carries neither."""
    root = probe_url("/BOE/biprws")
    idp = "https://login.microsoftonline.com/d38f6e9b-1b5d-45cf-bb8f-92f46845a5ab/saml2"
    client = RecordingHttpClient({("GET", root): (302, {"location": idp})})
    probe = session_with(client).logon()
    assert "microsoftonline" not in probe.detail
    assert "d38f6e9b" not in probe.detail


def test_a_plain_redirect_is_still_reported_as_a_redirect() -> None:
    """Without an SSO marker the status is still worth naming - it is not a JSON answer."""
    root = probe_url("/BOE/biprws")
    client = RecordingHttpClient(
        {("GET", root): (307, {"location": "https://boe.example.invalid:8443/elsewhere"})}
    )
    probe = session_with(client).logon()
    assert "redirected" in probe.detail
    assert "SAML" not in probe.detail
