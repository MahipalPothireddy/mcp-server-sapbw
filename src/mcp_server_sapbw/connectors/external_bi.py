"""Live BI-platform connectors: BusinessObjects over BIPRWS, Tableau over its repository.

Scenarios 9.7 (report schedules vs. chain completion) and 9.8 (dashboards reading calc views
directly) need two facts BW does not hold: when each report is scheduled, and which reports bypass
BW to read a calc view. Both live in the BI platform.

**The file-export route remains the default.** :class:`~.bi.FileBiConnector` reads an inventory
exported from any platform and needs no credentials, which keeps this server's trust boundary at
"read-only, BW only". These connectors are the opt-in step past that boundary, configured under a
separate ``bi_platforms`` key so that step is visible rather than inferred.

**Read-only, enforced by the far side rather than promised by this one.**

*Tableau* speaks PostgreSQL. The connection is opened with
``options='-c default_transaction_read_only=on'``, after which PostgreSQL rejects INSERT, UPDATE,
DELETE, TRUNCATE and DDL itself with SQLSTATE ``25006``. That is the database refusing, not this
code remembering - a bug in a query builder still cannot write. The session is additionally opened
inside an explicit read-only transaction, and every statement goes through one helper that rejects
anything not beginning with ``SELECT`` or ``WITH``.

*BOBJ* speaks HTTP. Every content read is a ``GET``, exactly as the ADT connector does. There are
exactly two non-GET calls and both are session lifecycle: ``POST .../logon/long`` (how BIPRWS issues
a token - there is no GET form) and ``POST .../logoff``. Neither touches BI content, and no other
method is reachable: :meth:`_BiprwsSession.get` is the only content path and hardcodes its verb.

**Neither account has to be read-only by grant**, and on this reference landscape neither is. That
is reported in :class:`~.base.ConnectorStatus` rather than glossed, so nobody reads the security
notes and assumes more than is true.

**Version differences are discovered, not configured.** BIPRWS moved: the RESTful services were
folded into the BOE web application in BI 4.3 SP03, so the root is ``/biprws`` on older releases and
``/BOE/biprws`` on newer ones. Rather than ask an operator to know which, the connector probes the
candidates and reports the one that answered - the same discipline the BW capability resolver
applies to table names, and for the same reason: a guess that half-works beats nothing, but a
measurement beats both.

**Error scrubbing.** ``httpx`` and ``psycopg`` both embed the endpoint - host included - in their
exception text. Every failure is re-raised as :class:`BiConnectorError` naming the profile and the
operation only, and the original exception is never chained into the message (mission Rule 5).
"""

from __future__ import annotations

import logging
import re
import ssl
from collections.abc import Sequence
from typing import Any, Literal

from ..core.profiles import BiPlatformProfile
from .base import ConnectorStatus
from .bi import BiDashboardSource, BiReportSchedule, BiSourceKind

_LOG = logging.getLogger(__name__)

_OK = 200
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_SERVER_ERROR = 500
#: A redirect where a JSON answer was expected. Reported distinctly because on an SSO-protected
#: deployment it is not a fault at all - it is the identity provider taking over the request, and no
#: credential this connector holds can satisfy a browser-based SAML flow.
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
#: Lower bound of the redirect range, so the 2xx success band has an upper bound to test against.
_REDIRECT_MIN = 300
#: Statuses whose meaning is exact rather than a band. A table rather than a chain of returns, so
#: adding one does not push the function past the branch limit and invite a shortcut.
_EXACT_STATUS_DETAIL: dict[int, str] = {
    _UNAUTHORIZED: "reachable, credentials rejected (401)",
    _FORBIDDEN: "reachable, authenticated but not permitted (403)",
    _NOT_FOUND: "BIPRWS is not deployed at this path (404)",
}

#: Markers that identify a federated-authentication redirect. Matched on the redirect
#: *target*, the only place the mechanism is visible - a status code alone cannot
#: distinguish an SSO hand-off from an ordinary relocation.
_SSO_MARKERS: tuple[str, ...] = (
    "saml",
    "samlrequest",
    "/adfs/",
    "login.microsoftonline.com",
    "/oauth2/",
    "openid",
    "wsfed",
    "/sso/",
)


def detect_sso(location: str) -> str | None:
    """Name the federated-authentication mechanism a redirect is handing off to, if any.

    **Why this is worth detecting rather than treating as a failure.** A BOBJ deployment behind SAML
    answers a browser and refuses a password, and those two facts together look just
    like a broken endpoint if the redirect target is not read. On the reference
    landscape the launch pad redirects
    to an Entra ID SAML endpoint, which means username-and-password access is not merely
    misconfigured - it is *architecturally* unavailable, and the fix is a Trusted Authentication
    secret or a service account, not a corrected credential.

    Returns a short mechanism name for the report, never the redirect URL itself: an
    IdP URL carries a tenant identifier and a host, and neither belongs in a payload
    (mission Rule 5).
    """
    lowered = location.lower()
    if "samlrequest" in lowered or "saml2" in lowered or "/saml" in lowered:
        return "SAML"
    if "login.microsoftonline.com" in lowered:
        return "Microsoft Entra ID"
    if "/adfs/" in lowered:
        return "AD FS"
    if "/oauth2/" in lowered or "openid" in lowered:
        return "OAuth2 / OpenID Connect"
    if any(marker in lowered for marker in _SSO_MARKERS):
        return "federated single sign-on"
    return None

#: BIPRWS web-application roots, newest layout first. BI 4.3 SP03 merged the RESTful services into
#: the BOE web application; before that they were a separate ``biprws`` webapp. Probed in order.
BIPRWS_ROOT_CANDIDATES: tuple[str, ...] = ("/BOE/biprws", "/biprws")

#: Statement prefixes the Tableau reader will send. Anything else is refused before it reaches the
#: driver, so read-only holds even if PostgreSQL's own session flag were somehow not applied.
_READ_ONLY_PREFIXES: tuple[str, ...] = ("SELECT", "WITH")


class BiConnectorError(Exception):
    """A BI platform read failed. Names the profile and operation only - never host or secrets."""


def _legacy_cipher_context(validate_certificate: bool) -> ssl.SSLContext:
    """TLS context admitting the cipher suites older Tomcat/SAP deployments still offer.

    Same concession, and same reasoning, as the ADT connector's equivalent: OpenSSL 3.x refuses
    static-RSA key exchange at its default security level, so a server offering no ECDHE suite is
    unreachable and reports a misleadingly generic handshake failure. Encryption and certificate
    validation are unchanged; what is given up is forward secrecy.
    """
    context = ssl.create_default_context()
    if not validate_certificate:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.set_ciphers("DEFAULT@SECLEVEL=1")
    return context


# --- BusinessObjects -----------------------------------------------------------------------------


class BiprwsProbe:
    """What a BIPRWS logon established: which root answered, and whether it authenticated."""

    __slots__ = ("authenticated", "detail", "root", "status", "version")

    def __init__(
        self,
        root: str | None,
        status: int | None,
        authenticated: bool,
        detail: str,
        version: str | None = None,
    ) -> None:
        self.root = root
        self.status = status
        self.authenticated = authenticated
        self.detail = detail
        self.version = version


class _BiprwsSession:
    """A logged-on BIPRWS session. GET-only for content; POST only to log on and off."""

    def __init__(self, profile: BiPlatformProfile) -> None:
        self._profile = profile
        self._client: Any | None = None
        self._token: str | None = None
        self._root: str | None = None

    # -- transport ---------------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import httpx  # noqa: PLC0415 - optional extra, imported lazily
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by install shape
            raise BiConnectorError(
                "reading BOBJ report schedules needs the 'bi' extra: "
                "pip install 'mcp-server-sapbw[bi]'"
            ) from exc
        verify: bool | str | ssl.SSLContext = self._profile.ssl_validate_certificate
        if self._profile.use_tls:
            verify = _legacy_cipher_context(self._profile.ssl_validate_certificate)
        self._client = httpx.Client(
            timeout=self._profile.timeout_seconds,
            verify=verify,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        return self._client

    def _url(self, root: str, path: str) -> str:
        return self._profile.base_url(f"{root.strip('/')}/{path.strip('/')}")

    def _transport_detail(self, exc: Exception, root: str) -> str:
        """Why the transport failed - and whether the endpoint is in fact fine (D73).

        **Refusing on principle is not the same as failing to reach**, and reporting both as
        "transport failure" sends the reader to the network team for a decision that is theirs. The
        landscape owner opened BIPRWS on the RESTful port, it answered a credential-free GET with
        the logon template, and this connector still said ``transport failure (ConnectError)`` -
        the port carries no TLS listener and the profile will not fall back to plain HTTP with a
        password in hand. True, and useless: nothing in it says the endpoint works, and nothing says
        which setting decides.

        The retry is safe by construction. It is the *same credential-free* GET the health gate
        already performs - the password is not in this request and cannot be - so establishing
        "the service is up, just not over TLS" costs one request and discloses nothing. Only
        attempted when TLS was actually required, so a profile that already allows plain HTTP does
        not probe twice.
        """
        base = f"transport failure ({type(exc).__name__}); connection details withheld"
        if not self._profile.use_tls or self._profile.allow_plain_http:
            return base
        plain = self._url(root, "logon/long").replace("https://", "http://", 1)
        response = self._plain_http_probe(plain)
        if response is None or not (_OK <= response.status_code < _REDIRECT_MIN):
            return base
        return (
            "no TLS listener on this port, but the endpoint answers a credential-free GET over "
            "plain HTTP, so the service itself is up: "
            f"{_classify_http(response.status_code)} No logon was attempted, because this profile "
            "refuses to put a credential on an unencrypted channel. To proceed, either expose "
            "BIPRWS over TLS, or record the decision explicitly by setting use_tls: false and "
            "allow_plain_http: true on this platform - both, so it cannot happen by accident."
        )

    def _plain_http_probe(self, url: str) -> Any | None:
        """One credential-free GET over plain HTTP, or ``None`` when it cannot be made.

        Its own method so a test can substitute it without reaching for the HTTP library. This
        module's tests are entirely offline by design - every transport is injected - and
        monkeypatching ``httpx`` to prove a *message* would make an optional extra mandatory for the
        whole suite to import.
        """
        try:
            import httpx  # noqa: PLC0415 - optional extra, imported lazily
        except ModuleNotFoundError:  # pragma: no cover - the caller already needs httpx
            return None
        try:
            with httpx.Client(
                timeout=self._profile.timeout_seconds, follow_redirects=False
            ) as client:
                return client.get(url, headers={"Accept": "application/json"})
        except Exception:  # any failure here just leaves the original message in place
            return None

    # -- logon -------------------------------------------------------------------------

    def logon(self) -> BiprwsProbe:
        """Find a healthy BIPRWS root, then log on to it. Never the other way round.

        **Health is checked with a credential-free GET before any password is sent.** BIPRWS answers
        a ``GET`` on ``logon/long`` with the template of attributes the ``POST`` expects, so one
        request both discovers the endpoint and proves it is working - and if it is not working, the
        credentials never go on the wire. On the reference landscape that check is what turned an
        opaque 500 into a statement: the endpoint 500s on a bare GET, so it is broken in the web
        tier, and an hour spent varying auth types and body shapes against it was wasted effort.

        Returns a probe record rather than raising, in every outcome. "Not deployed here", "deployed
        but failing to initialise", "credentials rejected" and "logged on" are four different
        findings with four different owners, and the caller reports which one it got.
        """
        client = self._ensure_client()
        roots = (
            (self._profile.base_path,) if self._profile.base_path else BIPRWS_ROOT_CANDIDATES
        )
        last: BiprwsProbe | None = None
        for root in roots:
            if not root:
                continue
            url = self._url(root, "logon/long")
            # Step 1: credential-free health check.
            try:
                probe_response = client.get(url, headers={"Accept": "application/json"})
            except Exception as exc:
                last = BiprwsProbe(root, None, False, self._transport_detail(exc, root))
                continue
            if probe_response.status_code != _OK:
                detail = _classify_http(probe_response.status_code)
                # A redirect to an identity provider is a finding in its own right,
                # and the mechanism is only visible in the redirect target. Named,
                # never quoted: an IdP URL carries a tenant id and a host.
                mechanism = detect_sso(probe_response.headers.get("location", ""))
                if mechanism:
                    detail = (
                        f"{detail} The redirect target identifies {mechanism}, so this deployment "
                        "authenticates through an external identity provider and cannot be reached "
                        "with a user name and password. Programmatic access needs BOBJ Trusted "
                        "Authentication (a shared secret, after which BIPRWS accepts a user name "
                        "without a password) or a service account exempt from SSO."
                    )
                last = BiprwsProbe(root, probe_response.status_code, False, detail)
                # A 404 means "try the other layout"; anything else means this root is the right
                # place and is broken, so there is nothing to gain from the remaining candidates.
                if probe_response.status_code == _NOT_FOUND:
                    continue
                return last

            # Step 2: the endpoint is healthy, so it is safe to authenticate. This POST and the
            # matching logoff are the only two non-GET calls this connector can make; BIPRWS issues
            # its token only via POST, and neither call touches BI content.
            payload = {
                "userName": self._profile.user,
                "password": self._profile.password.get_secret_value(),
                "auth": "secEnterprise",
            }
            try:
                response = client.post(
                    url,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
            except Exception as exc:
                last = BiprwsProbe(
                    root,
                    None,
                    False,
                    f"transport failure ({type(exc).__name__}); connection details withheld",
                )
                continue
            token = response.headers.get("X-SAP-LogonToken")
            if response.status_code == _OK and token:
                self._token = token
                self._root = root
                return BiprwsProbe(root, _OK, True, "logged on", self._server_version(root))
            last = BiprwsProbe(
                root,
                response.status_code,
                False,
                _classify_http(response.status_code),
            )
        return last or BiprwsProbe(None, None, False, "no BIPRWS root configured or reachable")

    def _server_version(self, root: str) -> str | None:
        """Best-effort platform version from the service document. Never fatal."""
        try:
            info = self.get(f"{root}/v1/info") or self.get(f"{root}/info")
        except BiConnectorError:
            return None
        if isinstance(info, dict):
            for key in ("version", "productVersion", "Version"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    # -- the only content path ---------------------------------------------------------

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """The only way this connector reads content. Hardcodes GET; returns parsed JSON or None."""
        if self._token is None or self._client is None:
            raise BiConnectorError(
                f"BOBJ profile '{self._profile.name}' is not logged on; call logon() first"
            )
        url = path if path.startswith("http") else self._profile.base_url(path)
        try:
            response = self._client.get(
                url,
                params=params or {},
                headers={"X-SAP-LogonToken": self._token},
            )
        except Exception as exc:
            raise BiConnectorError(
                f"BOBJ read on profile '{self._profile.name}' failed "
                f"({type(exc).__name__}); connection details withheld"
            ) from None
        if response.status_code != _OK:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def logoff(self) -> None:
        """Second and last non-GET call. Best effort: a stale session expires on its own."""
        if self._token is None or self._client is None or self._root is None:
            return
        try:
            self._client.post(
                self._url(self._root, "logoff"),
                headers={"X-SAP-LogonToken": self._token},
            )
        except Exception:
            # Broad on purpose: a logoff failure must never mask a successful read, and a stale
            # BIPRWS session expires on its own.
            _LOG.debug("BOBJ logoff failed on profile '%s'", self._profile.name)
        finally:
            self._token = None

    def close(self) -> None:
        self.logoff()
        if self._client is not None:
            self._client.close()
            self._client = None


def _classify_http(status: int) -> str:
    """Turn a BIPRWS status into an actionable statement.

    **The success case is named, and was not** (D73). Every failure had a sentence and a 2xx fell
    through to "unexpected HTTP status 200" - so the one observation this whole gate exists to make
    came back labelled as a surprise. Found when the landscape owner opened the RESTful port and the
    endpoint started working: the probe reported success as an anomaly.

    The 500 case earns its length. On the reference landscape ``/biprws`` answers **500 to a plain
    credential-free GET**, which proves the web application is mapped in Tomcat and failing to
    initialise - not missing, not rejecting the credentials, and not disagreeing about the request
    body. Those are four different problems with four different owners, and collapsing
    them into
    "unexpected HTTP status 500" sends whoever reads it looking in the wrong place. It cost an hour
    of trying auth types and body shapes against an endpoint that cannot answer any of them.
    """
    if _OK <= status < _REDIRECT_MIN:
        return (
            f"deployed and healthy (HTTP {status} to a credential-free GET). BIPRWS answers this "
            "request with the template of attributes a logon POST expects, so the endpoint is "
            "mapped, initialised and able to reach its CMS. Whether a credential is accepted is a "
            "separate question this observation does not answer."
        )
    exact = _EXACT_STATUS_DETAIL.get(status)
    if exact is not None:
        return exact
    if status in _REDIRECT_STATUSES:
        return (
            f"redirected (HTTP {status}) rather than answering. On a single-sign-on "
            "deployment this is the identity provider taking over: the endpoint is "
            "reachable but will not accept a user name and password, because "
            "authentication happens in a browser against the IdP."
        )
    if status >= _SERVER_ERROR:
        return (
            f"BIPRWS is deployed at this path but is failing to initialise (HTTP {status} to a "
            "credential-free GET, so this is not an authentication, authorisation or request-shape "
            "problem). Usually the web application cannot reach its CMS, or was not fully "
            "deployed. The cause is in the web tier's own log, not in anything a client sends - a "
            "BOBJ administrator has to read the biprws application log on the Tomcat node."
        )
    return f"unexpected HTTP status {status}"


class BobjConnector:
    """Reads BusinessObjects report schedules over BIPRWS. GET-only for content.

    Unconfigured without a ``bi_platforms`` profile of kind ``bobj``, in which case every dependent
    analyzer emits an ``unpopulated_reason`` rather than inventing data.
    """

    kind: Literal["bobj"] = "bobj"

    def __init__(
        self,
        profile: BiPlatformProfile | None = None,
        session: _BiprwsSession | None = None,
    ) -> None:
        if profile is not None and profile.kind != "bobj":
            raise BiConnectorError(
                f"profile '{profile.name}' is kind '{profile.kind}', not 'bobj'"
            )
        self._profile = profile
        # Injectable so the connector is fully testable offline with no HTTP stack involved.
        self._session = session if session is not None else (
            _BiprwsSession(profile) if profile is not None else None
        )
        self._probe: BiprwsProbe | None = None

    def is_configured(self) -> bool:
        return self._profile is not None and self._session is not None

    def probe(self) -> BiprwsProbe:
        """Log on (once) and report what was established. Safe to call repeatedly."""
        if self._probe is not None:
            return self._probe
        if self._session is None:
            self._probe = BiprwsProbe(None, None, False, "no BOBJ platform configured")
        else:
            self._probe = self._session.logon()
        return self._probe

    def status(self) -> ConnectorStatus:
        if self._profile is None:
            return ConnectorStatus(
                kind=self.kind,
                configured=False,
                detail=(
                    "no BOBJ platform configured; add a 'bi_platforms' entry with kind: bobj to "
                    "read report schedules over BIPRWS"
                ),
            )
        grant = (
            "account is read-only by grant"
            if self._profile.read_only_by_grant
            else "account is NOT read-only by grant; reads are GET-only by construction"
        )
        return ConnectorStatus(
            kind=self.kind,
            configured=True,
            detail=(
                f"BIPRWS over {self._profile.scheme} as profile '{self._profile.name}'; {grant}"
            ),
        )

    def platform(self) -> str | None:
        version = self._probe.version if self._probe else None
        return f"SAP BusinessObjects {version}" if version else "SAP BusinessObjects"

    def report_schedules(self) -> list[BiReportSchedule]:
        """Scheduled report instances, as far as the service exposes them.

        Returns an empty list rather than raising when the logon failed or the endpoint is absent -
        the caller reports the connector's :meth:`probe` detail, so an empty result is never
        presented as "this platform schedules nothing".
        """
        probe = self.probe()
        if not probe.authenticated or self._session is None or probe.root is None:
            return []
        schedules: list[BiReportSchedule] = []
        payload = self._session.get(
            f"{probe.root}/v1/scheduledreports", {"pageSize": str(self._row_cap())}
        )
        for entry in _iter_entries(payload):
            name = _first_str(entry, ("name", "SI_NAME", "title"))
            if not name:
                continue
            schedules.append(
                BiReportSchedule(
                    name=name,
                    provider=_first_str(entry, ("dataSource", "universe", "provider")),
                    scheduled_start=_first_str(entry, ("nextRunTime", "startTime", "beginTime")),
                    frequency=_first_str(entry, ("recurrence", "scheduleType", "frequency")),
                    owner=_first_str(entry, ("owner", "SI_OWNER", "ownerName")),
                )
            )
        return schedules

    def dashboard_sources(self) -> list[BiDashboardSource]:
        """BOBJ does not expose a calc-view-level source list over BIPRWS, so this is empty.

        Reported as an honest empty rather than approximated from universe names: scenario 9.8 asks
        whether a dashboard bypasses BW to read a calc view directly, and a universe name cannot
        answer that. The Tableau connector does answer it, from connection metadata.
        """
        return []

    def _row_cap(self) -> int:
        return self._profile.max_rows if self._profile else 0

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


def _iter_entries(payload: Any) -> Sequence[dict[str, Any]]:
    """Normalise the several shapes BIPRWS uses for a collection into a list of dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in ("entries", "items", "scheduledreports", "reports", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [e for e in value if isinstance(e, dict)]
        # A single-entity response is still one entry.
        return [payload]
    return []


def _first_str(entry: dict[str, Any], keys: Sequence[str]) -> str | None:
    """First non-blank string among ``keys``. BIPRWS field naming varies across releases."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("name") or value.get("value")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


# --- Tableau -------------------------------------------------------------------------------------

#: Repository views this connector reads, by role. Probed for existence before use, because Tableau
#: renames and reshapes repository objects across versions and an absent view must be reported as a
#: gap rather than raised as a malformed query. Same contract as the BW capability resolver.
TABLEAU_VIEWS: dict[str, tuple[str, ...]] = {
    # Extract-refresh and subscription schedules.
    "schedules": ("_schedules", "schedules"),
    "tasks": ("tasks",),
    # Workbooks and published datasources. The underscore-prefixed forms are Tableau's own
    # reporting views over the raw tables, with owner and project already resolved.
    "workbooks": ("_workbooks", "workbooks"),
    "datasources": ("_datasources", "datasources"),
    # Subscriptions. 849 of the 1,143 scheduled content tasks on the reference site are
    # subscriptions, and for every one of them ``tasks.obj_type`` is NULL - the task points at a
    # subscription, not at content, so resolving the report needs this extra hop. Without it 74% of
    # scenario 9.7's timeline reads "unresolved object 5881", which is a populated-looking list that
    # answers nothing.
    "subscriptions": ("subscriptions",),
    # Where content actually reads from: the half that answers scenario 9.8.
    #
    # Measured, not guessed. The first version of this map looked for
    # ``_datasource_connections`` / ``_workbook_connections``, which exist on neither this Tableau
    # version nor, as far as the repository shows, any other - both resolved to nothing and the
    # scenario silently returned an empty list. The real table is ``data_connections``, one row per
    # connection with a polymorphic owner (``owner_type`` + ``owner_id``) pointing at either a
    # workbook or a datasource. ``workbook_extension_connections`` exists but is about dashboard
    # extensions and holds 0 rows here, so it is not the link it looks like.
    "connections": ("data_connections",),
}

#: Task types that schedule *content*. The rest of what lives in ``tasks`` is Tableau's own
#: housekeeping - temp-directory cleanup, licence checks, OAuth code expiry - and reporting those as
#: "report schedules" would pad scenario 9.7 with platform maintenance the BW team cannot act on. On
#: the reference site these three cover 1,143 of 1,245 rows; the remaining ~30 types are one or two
#: rows each and are all internal.
TABLEAU_CONTENT_TASKS: tuple[str, ...] = (
    "RefreshExtractTask",
    "IncrementExtractTask",
    "SingleSubscriptionTask",
)

#: Connection classes that mean "reading SAP HANA directly". A workbook or datasource on one of
#: these is a scenario-9.8 candidate: it may be reading a calc view rather than going through BW.
_HANA_DBCLASSES: frozenset[str] = frozenset({"saphana", "hana"})

#: What the repository cannot tell us, stated once so every caller reports the same limit.
#:
#: **Measured, not assumed.** On the reference site all 360 ``saphana`` connections carry an empty
#: ``tablename`` *and* an empty ``dbname``, and across all 2,460 connections there are **zero**
#: values containing ``_SYS_BIC``, ``/BIC/``, ``/BI0/`` or any slash-qualified object name. So
#: ``data_connections`` records *that* a connection reads SAP HANA and never *which object* - the
#: object lives in the workbook's own XML, which is not a repository table.
#:
#: This matters because scenario 9.8 asks whether a dashboard reads the *same* calc view a
#: CompositeProvider uses. That question needs the object name, so the repository route can size the
#: population and name the dashboards, and cannot close the comparison. Saying so is the whole point
#: -- an earlier version of this connector classified 11 connections as ``calc_view`` by reading a
#: connection's *name* as an object name, which produced confident, wrong findings.
HANA_OBJECT_GAP = (
    "For direct SAP HANA connections (dbclass 'saphana') the Tableau repository records that the "
    "connection exists but not which object it reads: dbname and tablename are empty on all of "
    "them. Those dashboards can be counted and named, but the specific calc view each one reads "
    "lives in the workbook XML rather than in a repository table, so scenario 9.8's "
    "shared-versus-separate view comparison stays unresolved for them on this route. Connections "
    "that go through a published datasource (dbclass 'sqlproxy') are different and ARE resolved - "
    "see the BW generated-view findings."
)

#: Marker for a BW-generated SAP HANA view. BW can expose a BEx query (or a provider) as an external
#: SAP HANA calculation view, activated into HANA's ``_SYS_BIC`` runtime schema. Tableau records the
#: path in ``data_connections.dbname`` with separators stripped, so the marker survives but the
#: structure does not.
_SYS_BIC_MARKER = "_SYS_BIC"

#: The generated view's own name inside that path. BW's convention for a query view is
#: ``<prefix>_<provider>_Q<nn>_H``; the ``bw2hana_query_<provider>`` segment names the provider
#: separately. Matched rather than split on a separator because Tableau strips the separators.
_BW_GENERATED_VIEW = re.compile(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_Q\d+_H)")

#: The provider segment BW puts in the generated path, e.g. ``bw2hana_query_zcp_l01`` before the
#: upper-case view name begins.
#:
#: Deliberately case-SENSITIVE. With ``re.IGNORECASE`` the ``(?=[A-Z])`` lookahead also matches a
#: lower-case letter, so the lazy quantifier stopped at the first character and every provider came
#: back as the single letter ``Z``. The whole boundary here *is* the case change, so a
#: case-insensitive match cannot express it.
_BW2HANA_PROVIDER = re.compile(r"bw2hana_query_([a-z0-9_]+?)(?=[A-Z]|$)")


def extract_bw_generated_view(dbname: str | None) -> tuple[str | None, str | None]:
    """``(generated view name, provider)`` from a Tableau ``dbname``, or ``(None, None)``.

    **Why this exists.** 11 connections on the reference site carry a BW-generated HANA view path in
    ``dbname``, naming five distinct BEx queries and the providers behind them. Without parsing it,
    ``dashboard_sources`` reported "(whole database
    system-local_bw_bw2hana_query_zcp_l01ZSS_ZCP_L01_Q01_H_SYS_BIC)" - the answer was present and
    unusable, and nothing downstream could join it to the BW query subsystem.

    Advisory, and labelled so by the caller: this reads a naming convention out of a string a
    third-party product assembled, which is the class of inference mission Known Limitation 3 says
    to mark rather than trust. The view name is load-bearing and the provider a bonus; either may be
    ``None``.
    """
    if not dbname or _SYS_BIC_MARKER not in dbname:
        return None, None
    view = _BW_GENERATED_VIEW.search(dbname)
    provider = _BW2HANA_PROVIDER.search(dbname)
    return (
        view.group(1) if view else None,
        provider.group(1).upper() if provider else None,
    )


#: Host-shaped values, scrubbed from every string this connector emits.
#:
#: **Why a mechanism and not care.** A BI repository is full of host names and they are
#: not confined to columns called ``server``. The first working version of
#: ``dashboard_sources`` reported a
#: connection's ``caption`` as the dashboard name, and on the reference site that caption *is* the
#: database server's FQDN - so production host names went straight into a payload. Not fetching the
#: ``server`` column was necessary and nowhere near sufficient.
#:
#: Auditing every column of a third-party schema by hand does not scale and would not survive the
#: next Tableau version, so host removal is applied on the way out, to everything, once. Mission
#: Rule 5 is absolute about hosts and this is the only way to hold it against a schema this server
#: does not control.
_HOSTISH = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){1,}"
    r"(?:com|org|net|gov|edu|io|dev|cloud|local|localhost|corp|internal|intranet|lan)\b",
    re.IGNORECASE,
)

#: What a scrubbed host is replaced with. Named rather than blanked, so a reader can tell the
#: difference between "this field held a host and we removed it" and "this field was empty".
_HOST_REDACTED = "<host withheld>"


def _no_host(value: str | None) -> str | None:
    """Strip anything host-shaped out of a value before it can reach a payload.

    The single-label case (a bare uppercase host with no domain) is not detectable from one
    value in isolation, and is handled instead by the callers preferring *content* names
    over connection names - a workbook name is never a host.
    """
    if value is None:
        return None
    scrubbed = _HOSTISH.sub(_HOST_REDACTED, value)
    return scrubbed


def _minutes_to_clock(value: Any) -> str | None:
    """Tableau stores a schedule start as minutes past midnight. Render it as ``HH:MM``.

    Returned as local wall clock with no timezone attached, because that is exactly what the
    repository holds - the schedule is interpreted in the *site's* timezone, which is a separate
    setting. Scenario 9.7 compares this against a BW chain's p95 completion, and the BW side had the
    same trap (chain logs are UTC while RSPC displays local), so the units are stated rather than
    assumed to match.
    """
    if value is None:
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= minutes < 24 * 60:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class TableauProbe:
    """What a repository connection established: reachability and which views exist."""

    __slots__ = ("detail", "reachable", "resolved", "version")

    def __init__(
        self,
        reachable: bool,
        detail: str,
        resolved: dict[str, str] | None = None,
        version: str | None = None,
    ) -> None:
        self.reachable = reachable
        self.detail = detail
        self.resolved = resolved or {}
        self.version = version


class TableauConnector:
    """Reads Tableau's ``workgroup`` repository over PostgreSQL, read-only at the session level.

    Unconfigured without a ``bi_platforms`` profile of kind ``tableau``.
    """

    kind: Literal["tableau"] = "tableau"

    def __init__(
        self,
        profile: BiPlatformProfile | None = None,
        connect: Any | None = None,
    ) -> None:
        if profile is not None and profile.kind != "tableau":
            raise BiConnectorError(
                f"profile '{profile.name}' is kind '{profile.kind}', not 'tableau'"
            )
        self._profile = profile
        # Injectable connect callable so the connector is testable offline with no driver involved.
        self._connect = connect
        self._conn: Any | None = None
        self._probe: TableauProbe | None = None

    def is_configured(self) -> bool:
        return self._profile is not None

    # -- connection ------------------------------------------------------------------

    def _connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._profile is None:
            raise BiConnectorError("Tableau connector is not configured")
        if self._connect is None:
            try:
                import psycopg  # noqa: PLC0415 - optional extra, imported lazily
            except ModuleNotFoundError as exc:  # pragma: no cover - install shape
                raise BiConnectorError(
                    "reading the Tableau repository needs the 'bi' extra: "
                    "pip install 'mcp-server-sapbw[bi]'"
                ) from exc
            self._connect = psycopg.connect
        try:
            self._conn = self._connect(
                host=self._profile.host,
                port=self._profile.port,
                dbname=self._profile.database,
                user=self._profile.user,
                password=self._profile.password.get_secret_value(),
                connect_timeout=int(self._profile.timeout_seconds),
                sslmode="require" if self._profile.use_tls else "prefer",
                # THE read-only enforcement. Applied as a startup option, so it is in force before
                # the first statement runs rather than after a SET this code has to remember to
                # issue. PostgreSQL then rejects INSERT/UPDATE/DELETE/TRUNCATE and DDL itself with
                # SQLSTATE 25006 - the server refusing, not this connector promising.
                options="-c default_transaction_read_only=on",
            )
        except Exception as exc:
            raise BiConnectorError(
                f"Tableau repository connection for profile '{self._profile.name}' failed "
                f"({type(exc).__name__}); connection details withheld"
            ) from None
        return self._conn

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run one read. Refuses any statement that is not a SELECT or WITH.

        Belt and braces over the session flag: the flag is the enforcement, and this is the thing
        that makes "no code path can write" true of *this module* independently of the server's
        configuration having been applied.
        """
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in _READ_ONLY_PREFIXES:
            raise BiConnectorError(
                f"refused a non-read statement starting {head!r}: this connector issues only "
                f"{' / '.join(_READ_ONLY_PREFIXES)}"
            )
        conn = self._connection()
        try:
            with conn.cursor() as cursor:
                # `None`, not `()`, when there are no parameters. psycopg treats *any* non-None
                # argument as "this statement is parameterised" and then reads `%` as the start of
                # a placeholder - so an unparameterised `LIKE '%conn%'` fails with a
                # ProgrammingError that says nothing about percent signs. Passing an empty tuple
                # here cost one confusing failure before it was found.
                cursor.execute(sql, tuple(params) if params else None)
                rows = cursor.fetchall()
        except Exception as exc:
            name = self._profile.name if self._profile else "?"
            raise BiConnectorError(
                f"Tableau repository read on profile '{name}' failed "
                f"({type(exc).__name__}); query and connection details withheld"
            ) from None
        return [tuple(row) for row in rows]

    # -- capability probe ------------------------------------------------------------

    def probe(self) -> TableauProbe:
        """Connect and resolve which repository views exist. Cached; safe to call repeatedly."""
        if self._probe is not None:
            return self._probe
        if self._profile is None:
            self._probe = TableauProbe(False, "no Tableau platform configured")
            return self._probe
        try:
            present = {
                str(row[0]).lower()
                for row in self.query(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
        except BiConnectorError as exc:
            self._probe = TableauProbe(False, str(exc))
            return self._probe
        resolved: dict[str, str] = {}
        for role, candidates in TABLEAU_VIEWS.items():
            for candidate in candidates:
                if candidate.lower() in present:
                    resolved[role] = candidate
                    break
        version: str | None = None
        try:
            rows = self.query("SELECT version()")
            if rows and isinstance(rows[0][0], str):
                # The PostgreSQL version, which dates the repository; not the Tableau version,
                # which the repository does not reliably carry. Labelled accordingly.
                version = rows[0][0].split(",")[0].strip()
        except BiConnectorError:
            version = None
        self._probe = TableauProbe(
            True,
            (
                f"repository reachable; {len(resolved)} of {len(TABLEAU_VIEWS)} view role(s) "
                f"resolved out of {len(present)} tables"
            ),
            resolved,
            version,
        )
        return self._probe

    def status(self) -> ConnectorStatus:
        if self._profile is None:
            return ConnectorStatus(
                kind=self.kind,
                configured=False,
                detail=(
                    "no Tableau platform configured; add a 'bi_platforms' entry with kind: tableau "
                    "to read the workgroup repository"
                ),
            )
        grant = (
            "account is read-only by grant"
            if self._profile.read_only_by_grant
            else (
                "account is NOT read-only by grant; the session sets "
                "default_transaction_read_only=on so PostgreSQL refuses writes (SQLSTATE 25006)"
            )
        )
        return ConnectorStatus(
            kind=self.kind,
            configured=True,
            detail=(
                f"Tableau repository '{self._profile.database}' as profile "
                f"'{self._profile.name}'; {grant}"
            ),
        )

    def platform(self) -> str | None:
        return "Tableau"

    # -- the two analyzer questions --------------------------------------------------

    def report_schedules(self) -> list[BiReportSchedule]:
        """Scheduled extract refreshes and subscriptions, one entry per scheduled content task.

        Platform housekeeping tasks are excluded (:data:`TABLEAU_CONTENT_TASKS`): a temp-directory
        cleanup is not a report, and padding scenario 9.7 with maintenance jobs the BW team cannot
        act on would make the timeline unreadable.

        ``scheduled_start`` is local wall clock in the *site's* timezone. Scenario 9.7 compares it
        to a BW chain's p95 completion, and the BW log is UTC, so the two are not directly
        comparable without the site offset - which is why the unit is stated rather than implied.
        """
        probe = self.probe()
        schedules = probe.resolved.get("schedules")
        tasks = probe.resolved.get("tasks")
        if not probe.reachable or not schedules or not tasks:
            return []
        # The scheduled object's own name, so each entry names a report rather than the
        # schedule it shares. `tasks.title` is null for most rows on the reference site, so
        # the first version fell back to the schedule name and produced six identical
        # "Monday morning" entries for six different reports - a list that looks
        # populated and answers nothing.
        names = self._content_names(
            probe.resolved.get("workbooks"), probe.resolved.get("datasources")
        )
        subjects = self._subscription_subjects()
        placeholders = ", ".join(["%s"] * len(TABLEAU_CONTENT_TASKS))
        rows = self.query(
            # Relation names are interpolated, not bound: they are identifiers, which SQL cannot
            # parameterise. Safe because they come from TABLEAU_VIEWS - a frozen literal table - and
            # reached `resolved` only by exact-matching a row in information_schema, never from a
            # caller. The *values* below are bound.
            f"SELECT t.type, t.title, t.obj_type, t.obj_id, s.name, s.schedule_type, "
            f"       s.scheduled_action, s.start_at_minute, s.active "
            f"FROM {tasks} t JOIN {schedules} s ON t.schedule_id = s.id "
            f"WHERE t.type IN ({placeholders}) "
            f"ORDER BY s.start_at_minute NULLS LAST, s.name "
            f"LIMIT {self._row_cap()}",
            list(TABLEAU_CONTENT_TASKS),
        )
        out: list[BiReportSchedule] = []
        for (
            task_type,
            title,
            obj_type,
            obj_id,
            sched_name,
            schedule_type,
            action,
            start_minute,
            active,
        ) in rows:
            resolved = names.get((str(obj_type or "").lower(), obj_id))
            if resolved is None and obj_type is None:
                # A NULL obj_type means the task points at a subscription, not at content.
                subject = subjects.get(obj_id)
                resolved = f"subscription: {subject}" if subject else None
            label = str(title or resolved or f"unresolved {obj_type or 'object'} {obj_id}")
            out.append(
                BiReportSchedule(
                    name=_no_host(label) or "unknown",
                    # What the task acts on ("Workbook", "Datasource"), not a BW provider - the BW
                    # side is resolved by the analyzer, from dashboard_sources.
                    provider=str(obj_type) if obj_type else None,
                    scheduled_start=_minutes_to_clock(start_minute),
                    # The task type is the self-describing part and is reported as the frequency's
                    # substance. `schedule_type` and `scheduled_action` are small integers whose
                    # meaning is NOT decoded here: the repository ships no text for them, the
                    # schedule *names* on this site are consistent with 2=weekly / 3=monthly, and
                    # this project's standing rule is that a name is not evidence (a BW chain named
                    # "6 AM CST" runs at 05:30). They are carried raw and labelled raw, so a reader
                    # can correlate them without being told what they mean.
                    frequency=(
                        f"{task_type} on schedule '{sched_name}' "
                        f"(raw schedule_type={schedule_type}, scheduled_action={action}, "
                        f"active={active})"
                    ),
                    owner=None,
                )
            )
        return out

    def dashboard_sources(self) -> list[BiDashboardSource]:
        """Which workbooks and datasources read which database object - scenario 9.8's evidence.

        Read from ``data_connections``, whose owner is polymorphic: one row per connection, pointing
        at either a workbook or a published datasource. A connection whose class is SAP HANA is
        reading HANA directly, and its ``tablename`` says whether that is a calc view (a package
        path under the runtime schema) or a generated BW table. Anything else is ``unknown`` rather
        than guessed.

        **Columns are listed explicitly and never selected with ``*``**, because
        ``data_connections`` carries ``password`` and ``keychain`` columns. A convenience
        ``SELECT *`` here would pull stored connection credentials into this process for no reason.
        The ``server`` column is left unread for the same reason: it is a host name, this server has
        no use for it, and the least risky way to keep a host out of a payload is not to fetch it.
        """
        probe = self.probe()
        connections = probe.resolved.get("connections")
        workbooks = probe.resolved.get("workbooks")
        datasources = probe.resolved.get("datasources")
        if not probe.reachable or not connections:
            return []
        # Resolve the owning *content* name. Two reasons, and the second is a defect this fixes:
        #   1. Scenario 9.8 asks which dashboard bypasses BW, so the answer must name the dashboard.
        #   2. `data_connections.name` and `.caption` are CONNECTION names, and on the
        #      reference site they are frequently the database server's FQDN - so using
        #      them as the display name put production host names into the payload.
        #      Content names are the right field and they are also the safe one.
        names = self._content_names(workbooks, datasources)
        rows = self.query(
            # Explicit column list: `password`, `keychain` and `server` live in this table and are
            # deliberately not read. See the docstring.
            f"SELECT c.owner_type, c.owner_id, c.dbclass, c.db_subclass, "
            f"       c.dbname, c.tablename, c.has_extract "
            f"FROM {connections} c "
            f"ORDER BY c.dbclass NULLS LAST, c.id "
            f"LIMIT {self._row_cap()}"
        )
        out: list[BiDashboardSource] = []
        for owner_type, owner_id, dbclass, subclass, dbname, tablename, _has_extract in rows:
            kind_key = str(owner_type or "").lower()
            resolved_name = names.get((kind_key, owner_id))
            label = resolved_name or f"unresolved {owner_type or 'content'} {owner_id}"
            # `tablename` is the object actually read. `dbname` is a *database*, not an
            # object, so it is a qualifier only and never the source object on its own -
            # reading it as one was why 2,449 of 2,460 rows came back `unknown` at first.
            target = str(tablename).strip() if tablename else ""
            generated_view, generated_provider = extract_bw_generated_view(
                str(dbname) if dbname else None
            )
            if not target and generated_view:
                # A BW-generated HANA view for a BEx query. The most actionable row in this table:
                # it names a BW object, so the analyzer can join it back to the query subsystem.
                target = (
                    f"{generated_view} (BW-generated HANA view"
                    + (f", provider {generated_provider}" if generated_provider else "")
                    + ")"
                )
            if not target:
                target = f"(whole database {dbname})" if dbname else "(not recorded)"
            # Classified from the RAW values, never from the formatted display string. Deriving the
            # kind from `target` after formatting looked fine and was wrong: extracting the view
            # name out of the path removed the `_SYS_BIC` marker the classifier was matching on, so
            # improving the object name silently reclassified all 11 as `unknown`. A presentation
            # change must not be able to move a classification.
            kind: BiSourceKind = (
                "calc_view"
                if generated_view
                else _classify_tableau_source(
                    str(dbclass or ""), str(tablename or dbname or "")
                )
            )
            out.append(
                BiDashboardSource(
                    name=_no_host(label) or "unknown",
                    source_object=_no_host(target) or "unknown",
                    source_kind=kind,
                    # The connection CLASS, never the server. A class is what a finding needs
                    # ("this reads HANA directly") and it is not identifying.
                    connection=str(subclass or dbclass) if (subclass or dbclass) else None,
                )
            )
        return out

    def _content_names(self, workbooks: str | None, datasources: str | None) -> dict[
        tuple[str, Any], str
    ]:
        """``(owner_type, id) -> content name`` for every workbook and published datasource.

        One read per content type rather than a join per connection row: ``data_connections`` has a
        polymorphic owner, which SQL cannot join in a single statement without a union, and this
        dialect-free approach keeps the two reads independently bounded.
        """
        names: dict[tuple[str, Any], str] = {}
        for owner_type, table in (("workbook", workbooks), ("datasource", datasources)):
            if not table:
                continue
            try:
                rows = self.query(
                    f"SELECT id, name FROM {table} ORDER BY id LIMIT {self._row_cap()}"
                )
            except BiConnectorError:
                # A name lookup failing must not lose the connection inventory: the rows still
                # carry class and target, and the owner is reported as unresolved rather than the
                # whole scenario coming back empty.
                continue
            for row_id, name in rows:
                if name:
                    names[(owner_type, row_id)] = str(name)
        return names

    def _subscription_subjects(self) -> dict[Any, str]:
        """``subscription id -> subject`` for every subscription.

        The extra hop scenario 9.7 needs. A ``SingleSubscriptionTask`` carries a NULL ``obj_type``
        and an ``obj_id`` that is a *subscription* id, so without this every subscription in the
        timeline reads "unresolved object N" - 849 of 1,143 entries on the reference site.

        The subject line is used as the name because it is what a recipient actually sees, and
        because ``subscriptions`` has no name column. It can be blank, in which case the caller
        falls back rather than inventing one.
        """
        probe = self.probe()
        table = probe.resolved.get("subscriptions")
        if not table:
            return {}
        try:
            rows = self.query(
                f"SELECT id, subject FROM {table} ORDER BY id LIMIT {self._row_cap()}"
            )
        except BiConnectorError:
            return {}
        return {row_id: str(subject) for row_id, subject in rows if subject}

    def caveats(self) -> list[str]:
        """What this route could not establish. Never empty when a real limit applies.

        Surfaced as a list the analyzer attaches to its findings, so the gap travels with the answer
        instead of living only in this module's docstring. Without it, "0 dashboards classified as
        reading a calc view" reads as "no dashboard bypasses BW" - the opposite of what the data
        supports, since 360 connections read SAP HANA directly and the repository simply does not
        record which object.
        """
        probe = self.probe()
        out: list[str] = []
        if not probe.reachable:
            return [f"Tableau repository was not read: {probe.detail}"]
        missing = [role for role in TABLEAU_VIEWS if role not in probe.resolved]
        if missing:
            out.append(
                "Tableau repository objects not found for role(s) "
                f"{', '.join(sorted(missing))}; the answers those support are absent rather than "
                "empty. Repository objects are renamed between Tableau versions."
            )
        hana = self.hana_connection_count()
        if hana:
            out.append(f"{hana} connection(s) read SAP HANA directly. {HANA_OBJECT_GAP}")
        generated = self.bw_generated_view_count()
        if generated:
            out.append(
                f"{generated} connection(s) read a BW-generated SAP HANA view (a BEx query exposed "
                "as a calculation view). The view and provider names are parsed from the path "
                "Tableau stored in data_connections.dbname, with its separators already stripped, "
                "so they are advisory: the marker is reliable, the decomposition is a naming "
                "convention read out of a concatenated string."
            )
        out.append(
            "Schedule start times are local wall clock in the Tableau site's timezone, while BW "
            "chain logs are UTC. The two are not comparable without the site offset, so a safety "
            "margin computed across them must apply it first."
        )
        return out

    def hana_connection_count(self) -> int:
        """How many connections read SAP HANA directly. The scenario-9.8 population, as one number.

        Separate from :meth:`dashboard_sources` so a caller can size the problem before pulling
        thousands of rows, which is the same reason the BW list tools return a ``total_count``.
        """
        probe = self.probe()
        connections = probe.resolved.get("connections")
        if not probe.reachable or not connections:
            return 0
        placeholders = ", ".join(["%s"] * len(_HANA_DBCLASSES))
        rows = self.query(
            f"SELECT count(*) FROM {connections} WHERE lower(coalesce(dbclass,'')) "
            f"IN ({placeholders})",
            sorted(_HANA_DBCLASSES),
        )
        return int(rows[0][0]) if rows else 0

    def bw_generated_view_count(self) -> int:
        """Connections reading a BW-generated SAP HANA view (a BEx query exposed as a calc view).

        Counted with ``position()`` rather than ``LIKE``, because the marker begins with an
        underscore and ``_`` is a single-character wildcard in ``LIKE`` - so the obvious
        ``LIKE '%_SYS_BIC%'`` silently means "any character followed by SYS_BIC". That is not a
        theoretical hazard: it is one of the two reasons an earlier pass concluded this population
        was zero.
        """
        probe = self.probe()
        connections = probe.resolved.get("connections")
        if not probe.reachable or not connections:
            return 0
        rows = self.query(
            f"SELECT count(*) FROM {connections} "
            f"WHERE position(%s in coalesce(dbname,'')) > 0",
            [_SYS_BIC_MARKER],
        )
        return int(rows[0][0]) if rows else 0

    def _row_cap(self) -> int:
        return self._profile.max_rows if self._profile else 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def _classify_tableau_source(dbclass: str, target: str) -> Literal[
    "calc_view", "bw_provider", "unknown"
]:
    """Read a Tableau connection's class and target as "calc view" or "BW provider".

    Advisory by construction, and labelled ``unknown`` rather than guessed when neither signal is
    present. ``_SYS_BIC`` is HANA's runtime schema for activated calc views, so an object under it
    is being read directly; a generated BW table name means the path goes through BW.
    """
    upper = target.upper()
    # A generated BW table is checked FIRST, because it is the unambiguous signal and it can also
    # live under a HANA connection - testing calc-view first would classify every BW table read
    # through HANA as a calc view, which is the exact distinction scenario 9.8 turns on.
    if upper.startswith(("/BIC/", "/BI0/")) or "/BIC/" in upper or "/BI0/" in upper:
        return "bw_provider"
    # Two independent calc-view signals, parenthesised rather than left to operator precedence: the
    # runtime schema name is conclusive on its own, and a HANA connection to a slash-qualified
    # object is the package-path form an activated calc view takes. Relying on `and` binding than
    # `or` would be correct and unreadable, which is how a later edit turns a working rule wrong.
    if ("_SYS_BIC" in upper) or (dbclass.lower() in _HANA_DBCLASSES and "/" in target):
        return "calc_view"
    return "unknown"
