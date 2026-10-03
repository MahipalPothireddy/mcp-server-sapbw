"""ECC source-system connector: extractor-exit ABAP over ADT.

**Why this exists.** BW proves *that* a DataSource was enhanced - customer-namespace fields sit in
its extract structure - but the enhancement's logic is ABAP in the source system. Scenario 9.6 asks
which tables that logic reads (a read into another team's data is a coordination risk) and whether
it does per-record ``SELECT``s (a performance risk that scales with extract volume). Neither
question is answerable from BW.

**Correcting an earlier claim in this module.** A previous version stated that ABAP source is
unreachable because ``REPOSRC.DATA`` is compressed. That reasoning was about reading the *database
table* directly, and it is beside the point: ADT (``/sap/bc/adt``) is SAP's own HTTP service and
renders source server-side as ``text/plain``. Source is reachable; the compression is irrelevant.

**Read-only, structurally.** ADT can write - but only via ``POST``/``PUT`` and only after fetching
an ``X-CSRF-Token``. This connector:

* issues ``GET`` exclusively - :meth:`HttpxAdtFetcher.get_text` hardcodes the method and no other
  request helper exists, so there is no code path that can write (mission Rule 1);
* never requests a CSRF token, without which ADT rejects any modifying call;
* sends ``X-sap-adt-sessiontype: stateless`` so ADT takes no locks on the objects it reads.

**Exit slots.** Enhancement ``RSAP0001`` has four components, one per DataSource kind, each calling
a customer include named by SAP's ``ZXnnnU01`` convention:

===================  ==========  ============================
Exit function module Include     Serves
===================  ==========  ============================
EXIT_SAPLRSAP_001    ZXRSAU01    transaction data
EXIT_SAPLRSAP_002    ZXRSAU02    master-data attributes
EXIT_SAPLRSAP_003    ZXRSAU03    master-data texts
EXIT_SAPLRSAP_004    ZXRSAU04    master-data hierarchies
===================  ==========  ============================

These are SAP-defined names, identical on every ABAP system, and carry no customer information.

**Error scrubbing.** ``httpx`` embeds the full request URL - host included - in its exception text.
Every failure is therefore re-raised as :class:`AdtError` naming only the profile and the ADT path
(mission Rule 5). The underlying exception is never chained into the message.
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import unescape
from typing import Any, Literal, Protocol, runtime_checkable

from ..core.profiles import EccProfile
from ..models.ecc import AdtProvenance, ExitDataKind, ExitUnavailableReason, TableOwner
from .base import ConnectorStatus

# HTTP status codes ADT uses that this connector interprets rather than merely reports.
_OK = 200
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404


class ExitSlot:
    """One extractor-exit slot: its function module, customer include, and what it serves."""

    __slots__ = ("data_kind", "function_module", "include")

    def __init__(self, function_module: str, include: str, data_kind: ExitDataKind) -> None:
        self.function_module = function_module
        self.include = include
        self.data_kind = data_kind


# Enhancement RSAP0001's four components, in slot order.
EXIT_SLOTS: tuple[ExitSlot, ...] = (
    ExitSlot("EXIT_SAPLRSAP_001", "ZXRSAU01", "transaction_data"),
    ExitSlot("EXIT_SAPLRSAP_002", "ZXRSAU02", "master_data_attributes"),
    ExitSlot("EXIT_SAPLRSAP_003", "ZXRSAU03", "master_data_texts"),
    ExitSlot("EXIT_SAPLRSAP_004", "ZXRSAU04", "master_data_hierarchies"),
)


#: Results requested per ownership search. Small on purpose: the search returns the table plus its
#: related objects, and a handful is enough to rank among. A large page costs bytes for nothing.
_OWNER_SEARCH_RESULTS = 10

#: ADT object types that *are* the table, ranked most-authoritative first, then everything else.
#:
#: **This ranking is the correctness of the whole ownership read.** A search for ``VBRP`` returns
#: its
#: maintenance object (``SOBJ/MO``, package ``VFW``) *before* the table itself (``TABL/DT``, package
#: ``VF``). First-match therefore reports the wrong package - and a wrong owner inside a finding
#: about
#: ownership is worse than no finding, because it reads as precise.
_OBJECT_TYPE_RANK: dict[str, int] = {
    "TABL/DT": 0,  # transparent table - the thing being read
    "TABL/DS": 1,  # structure
    "VIEW/DV": 2,  # view
    "DTEL/DE": 8,  # data element that merely shares the name
    "SOBJ/MO": 9,  # maintenance object
}
_UNRANKED_TYPE = 5

_OBJECT_REFERENCE = re.compile(
    r"<adtcore:objectReference\b([^>]*)>?",
    re.IGNORECASE,
)
_NAME_ATTR = re.compile(r'adtcore:name="([^"]*)"')
_TYPE_ATTR = re.compile(r'adtcore:type="([^"]*)"')
_PACKAGE_ATTR = re.compile(r'adtcore:packageName="([^"]*)"')
_DESCRIPTION_ATTR = re.compile(r'adtcore:description="([^"]*)"')


def _best_object_reference(xml: str, name: str) -> tuple[str, str, str] | None:
    """Pick the reference that *is* the named object, returning ``(type, package, description)``.

    Exact name match first, then :data:`_OBJECT_TYPE_RANK`. Returns ``None`` when the search found
    nothing with this exact name, so the caller reports the object as unknown rather than
    attributing
    it to whatever came back.
    """

    def attr(pattern: re.Pattern[str], attrs: str) -> str:
        match = pattern.search(attrs)
        return match.group(1) if match else ""

    candidates: list[tuple[int, str, str, str]] = []
    for reference in _OBJECT_REFERENCE.finditer(xml):
        attrs = reference.group(1)
        if attr(_NAME_ATTR, attrs).upper() != name.upper():
            continue
        object_type = attr(_TYPE_ATTR, attrs)
        candidates.append(
            (
                _OBJECT_TYPE_RANK.get(object_type, _UNRANKED_TYPE),
                object_type,
                attr(_PACKAGE_ATTR, attrs),
                attr(_DESCRIPTION_ATTR, attrs),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0])
    _rank, object_type, package, description = candidates[0]
    return object_type, package, description


class AdtError(Exception):
    """An ADT fetch failed. The message names the profile and path only - never host or secrets."""


class AdtResponse:
    """A fetched ADT payload: status code and body text."""

    __slots__ = ("status", "text")

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text


@runtime_checkable
class AdtFetcher(Protocol):
    """Minimal read-only ADT transport. ``GET`` is the only operation it can express.

    ``accept`` is negotiable because ADT serves *source* as ``text/plain`` and *metadata* as
    ``application/vnd.sap.adt.*+xml``, and a single hardcoded header cannot ask for both. That was
    not
    a theoretical limitation: with ``text/plain`` fixed, every object-metadata endpoint answered 404
    or
    406, reading exactly like "this ADT exposes no object metadata" and wrong. Defaulted,
    so every existing source read is unchanged.
    """

    def get_text(
        self, path: str, params: Mapping[str, str], accept: str = "text/plain"
    ) -> AdtResponse: ...


def _legacy_cipher_context(validate_certificate: bool) -> ssl.SSLContext:
    """TLS context that also admits the cipher suites older SAP ICM releases still offer.

    **The problem this solves.** OpenSSL 3.x, at its default security level, refuses static-RSA
    key exchange. An ICM that advertises no ECDHE suite is therefore unreachable with ``httpx``'s
    default settings: the server rejects the ClientHello and OpenSSL surfaces the confusingly
    generic ``SSLV3_ALERT_HANDSHAKE_FAILURE``. This is easy to misread as the host being down,
    especially since ``curl`` on Windows uses Schannel, not OpenSSL, and connects to the very same
    port without complaint. Dropping to security level 1 re-admits those suites.

    **What this does not do.** It does not disable encryption, and it does not disable certificate
    checking - validation still follows ``ssl_validate_certificate``, and the negotiated bulk
    cipher is still AES-GCM. The single concession is forward secrecy: static RSA has none, so a
    recorded session becomes readable to anyone who later obtains the server's private key. That is
    a real cost, which is why the profile flag is opt-in rather than a silent fallback.

    The durable fix belongs on the SAP side - give the ICM an ECDHE-capable cipher suite list
    (``ssl/ciphersuites``, with a current CommonCryptoLib) - after which the flag can be removed.
    """
    context = ssl.create_default_context()
    if not validate_certificate:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.set_ciphers("DEFAULT@SECLEVEL=1")
    return context


class HttpxAdtFetcher:
    """``httpx``-backed ADT transport. GET-only; no CSRF token; stateless ADT session.

    ``httpx`` is an optional dependency (extra ``ecc``) and is imported lazily so a BW-only install
    never needs it.
    """

    def __init__(self, profile: EccProfile) -> None:
        self._profile = profile
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            # Lazily imported: httpx is an optional extra, so a BW-only install must not need it.
            import httpx  # noqa: PLC0415
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by install shape
            raise AdtError(
                "reading source-system ABAP over ADT needs the 'ecc' extra: "
                "pip install 'mcp-server-sapbw[ecc]'"
            ) from exc
        verify: bool | str | ssl.SSLContext = self._profile.ssl_validate_certificate
        if self._profile.use_tls and self._profile.allow_legacy_tls_ciphers:
            verify = _legacy_cipher_context(self._profile.ssl_validate_certificate)
        self._client = httpx.Client(
            base_url=self._profile.base_url,
            auth=(self._profile.user, self._profile.password.get_secret_value()),
            timeout=self._profile.timeout_seconds,
            verify=verify,
            follow_redirects=False,
            headers={
                # Stateless: ADT takes no enqueue lock on objects read this way.
                "X-sap-adt-sessiontype": "stateless",
            },
        )
        return self._client

    def get_text(
        self, path: str, params: Mapping[str, str], accept: str = "text/plain"
    ) -> AdtResponse:
        """Issue the one and only request this connector can make: a GET.

        ``Accept`` moved from the client to the call. ADT renders source as ``text/plain`` and
        object
        metadata as its own XML content types, and a client-level ``text/plain`` made every metadata
        endpoint answer 404 or 406 - indistinguishable from the endpoint not existing.
        """
        client = self._ensure_client()
        try:
            response = client.get(path, params=dict(params), headers={"Accept": accept})
        except Exception as exc:
            # Broad by design: httpx raises a family of transport errors and every one of them
            # embeds the full URL. The type is reported; the message is not (mission Rule 5).
            raise AdtError(
                f"ADT request to {path} on profile '{self._profile.name}' failed "
                f"({type(exc).__name__}); connection details withheld"
            ) from None
        return AdtResponse(response.status_code, response.text)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class EccConnector:
    """Reads extractor-exit ABAP from a source system over ADT.

    Unconfigured by default: with no ``ecc_systems`` profile the connector reports its own absence
    and every dependent analyzer emits an ``unpopulated_reason`` instead of inventing data.
    """

    kind: Literal["ecc"] = "ecc"

    def __init__(
        self,
        profile: EccProfile | None = None,
        fetcher: AdtFetcher | None = None,
    ) -> None:
        self._profile = profile
        # Injectable so the whole connector is testable offline, with no HTTP stack involved.
        self._fetcher = fetcher if fetcher is not None else self._default_fetcher(profile)

    @staticmethod
    def _default_fetcher(profile: EccProfile | None) -> AdtFetcher | None:
        return HttpxAdtFetcher(profile) if profile is not None else None

    @property
    def profile_name(self) -> str | None:
        return self._profile.name if self._profile is not None else None

    @property
    def client(self) -> str | None:
        return self._profile.client if self._profile is not None else None

    def is_configured(self) -> bool:
        return self._profile is not None and self._fetcher is not None

    def status(self) -> ConnectorStatus:
        if self._profile is None:
            return ConnectorStatus(
                kind=self.kind,
                configured=False,
                detail=(
                    "no ABAP source system configured; add an 'ecc_systems' entry to profiles.yaml "
                    "to read extractor-exit ABAP over ADT"
                ),
            )
        # Names the profile and client, never the host (mission Rule 5).
        transport = "https" if self._profile.use_tls else "http (plain, explicitly opted in)"
        return ConnectorStatus(
            kind=self.kind,
            configured=True,
            detail=(
                f"ADT over {transport} as profile '{self._profile.name}', "
                f"client {self._profile.client}, read-only (GET only)"
            ),
        )

    # --- source retrieval ------------------------------------------------------------------

    def fetch_source(
        self, object_name: str, *, order: tuple[str, ...] = ("includes", "programs")
    ) -> tuple[AdtResponse, str]:
        """Fetch one ABAP object's source, returning the response and the path that served it.

        Tries the include path first, then the program path: a customer exit include is registered
        as an include on most systems but as a program on some, and guessing wrong looks identical
        to "the enhancement does not exist".

        ``order`` reverses that preference for objects known to be standalone programs - a satellite
        exit program, for instance. The wrong order still finds the object, but spends an extra 404
        round trip per object, which matters when hundreds are probed.
        """
        if self._profile is None or self._fetcher is None:
            raise AdtError("ECC connector is not configured")
        params = {"sap-client": self._profile.client}
        root = self._profile.adt_root.rstrip("/")
        attempted: list[str] = []
        response: AdtResponse | None = None
        for segment in order:
            path = f"{root}/programs/{segment}/{object_name.lower()}/source/main"
            attempted.append(path)
            response = self._fetcher.get_text(path, params)
            if response.status != _NOT_FOUND:
                return response, path
        # Every candidate 404: report the first, which is the conventional location for this kind.
        return (response or AdtResponse(_NOT_FOUND, "")), attempted[0]

    @property
    def satellite_program_prefixes(self) -> list[str]:
        """Configured satellite-program prefixes; empty when the profile declares none."""
        return list(self._profile.satellite_program_prefixes) if self._profile else []

    @property
    def max_satellite_fetches(self) -> int:
        """Ceiling on satellite probe requests per inventory call."""
        return self._profile.max_satellite_fetches if self._profile else 0

    # --- object ownership (D67) --------------------------------------------------------------

    def resolve_owners(self, names: Sequence[str], *, limit: int = 0) -> dict[str, TableOwner]:
        """Package and description per object name, from ADT's information system.

        **The read that lets scenario 9.6 flag rather than ask.** Mission §9.6 wants an enhancement
        reading another team's data flagged; without an ownership signal the analyzer could only
        list
        the tables and hand the judgement back to the reader, which is defect D67 entire.

        One GET per name against ``repository/informationsystem/search``, which returns package
        name,
        description and object type together. Measured at ~77 ms per table on the reference system,
        49 of 49 resolved, so the cost is real but small - and bounded by ``limit`` (defaulting
        to the profile's satellite budget) because these are requests against a production system.

        **Ranked, not first-match.** The search returns several objects for one name: ``VBRP``
        yields
        its maintenance object ``SOBJ/MO`` in package ``VFW`` *before* the table ``TABL/DT`` in
        ``VF``.
        Taking the first hit therefore attributes the table to the wrong package, which is worse
        than
        no answer - it is a confident wrong owner in a finding about ownership.

        Unresolvable names are simply absent from the result. The caller reports them as unknown
        rather than defaulting them to any package.
        """
        if self._profile is None or self._fetcher is None:
            raise AdtError("ECC connector is not configured")
        budget = limit or self._profile.max_satellite_fetches
        params_base = {"sap-client": self._profile.client}
        root = self._profile.adt_root.rstrip("/")
        path = f"{root}/repository/informationsystem/search"
        owners: dict[str, TableOwner] = {}
        # De-duplicated on the NORMALISED name, not the raw string. The ABAP parser yields table
        # names lower-cased while other paths yield them upper-cased, so de-duplicating on the raw
        # value read the same table twice - one wasted GET against a production system per table
        # appearing in both forms. Found by a test, not by reading the code.
        unique = list(dict.fromkeys(n.strip().upper() for n in names if n and n.strip()))
        for query in unique[:budget]:
            try:
                response = self._fetcher.get_text(
                    path,
                    {
                        **params_base,
                        "operation": "quickSearch",
                        "query": query,
                        "maxResults": str(_OWNER_SEARCH_RESULTS),
                    },
                    accept="application/xml",
                )
            except AdtError:
                continue
            if response.status != _OK:
                continue
            picked = _best_object_reference(response.text, query)
            if picked is None:
                continue
            object_type, package, description = picked
            owners[query.upper()] = TableOwner(
                table=query.upper(),
                package=package or None,
                table_description=unescape(description) if description else None,
                object_type=object_type or None,
                provenance=AdtProvenance(
                    profile=self._profile.name,
                    client=self._profile.client,
                    adt_path=path,
                    object_name=query.upper(),
                    object_kind="include",
                    fetched_at=datetime.now(UTC),
                ),
            )
        return owners

    def resolve_package_texts(self, packages: Sequence[str], *, limit: int = 0) -> dict[str, str]:
        """Short text per development package, so ``VF`` reads as what it is.

        A second, much smaller read: there are far fewer packages than tables (30 across 49 on the
        reference system). ``/packages/<name>`` answers 404 here; the workbench object route does
        answer, which is why that path is used rather than the more obvious one.
        """
        if self._profile is None or self._fetcher is None:
            raise AdtError("ECC connector is not configured")
        budget = limit or self._profile.max_satellite_fetches
        params = {"sap-client": self._profile.client}
        root = self._profile.adt_root.rstrip("/")
        texts: dict[str, str] = {}
        for package in list(dict.fromkeys(p for p in packages if p and p.strip()))[:budget]:
            path = f"{root}/vit/wb/object_type/devck/object_name/{package.strip().lower()}"
            try:
                response = self._fetcher.get_text(path, params, accept="*/*")
            except AdtError:
                continue
            if response.status != _OK:
                continue
            match = _DESCRIPTION_ATTR.search(response.text)
            if match and match.group(1).strip():
                # Unescaped: ADT returns XML, so a description containing quotes arrives as
                # `Financial Accounting &quot;Basis&quot;`. Putting that in a finding would show the
                # entity to a reader.
                texts[package.strip().upper()] = unescape(match.group(1).strip())
        return texts

    @staticmethod
    def classify_status(status: int) -> ExitUnavailableReason | None:
        """Map a non-200 ADT status onto an unavailability reason, or ``None`` for success."""
        if status == _OK:
            return None
        if status == _NOT_FOUND:
            return "absent"
        if status == _UNAUTHORIZED:
            return "unauthorized"
        if status == _FORBIDDEN:
            return "forbidden"
        return "fetch_failed"

    def close(self) -> None:
        closer = getattr(self._fetcher, "close", None)
        if callable(closer):
            closer()
