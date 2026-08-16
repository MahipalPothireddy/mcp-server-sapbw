"""Connection profile loading and ``${VAR}`` interpolation.

Profiles live in a git-ignored ``profiles.yaml`` (path from ``BW_PROFILES_PATH``). Secrets are
never inline: the ``password`` field must be an environment-variable reference (``${VAR}``), and
any field may use ``${VAR}`` interpolation. ``abap_schema: auto`` is preserved as a sentinel to be
resolved later by the capability resolver — never hardcoded (mission Section 3, Rules 1 and 5).

Two profile kinds live in the same file under separate keys:

``systems``
    BW-on-HANA SQL connections (:class:`Profile`) — the server's primary target.
``ecc_systems``
    ABAP source systems reachable over ADT/HTTP (:class:`EccProfile`), used only to read
    extractor-exit ABAP that BW itself does not hold. Optional; absent by default.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ..models.access import AccessMode
from .identity import Environment, StorageIdentity

_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# Sentinel meaning "resolve the ABAP schema at connect time" (never hardcode SAPABAP1).
ABAP_SCHEMA_AUTO = "auto"


class ProfileConfigError(Exception):
    """A profiles file is missing, malformed, or references an unset environment variable."""


class ProfileNotFoundError(ProfileConfigError):
    """A named profile does not exist. Lists the configured names (never secrets)."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        listed = ", ".join(sorted(available)) or "(none)"
        super().__init__(f"unknown profile '{name}'; configured profiles: {listed}")


class Profile(BaseModel):
    """One BW system's connection configuration. The password is held as a SecretStr."""

    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    port: int = Field(gt=0, lt=65536)
    user: str
    password: SecretStr = Field(repr=False)
    # Which customer or landscape this system belongs to. Optional, and unset is right for an
    # install serving one organisation.
    #
    # It exists because everybody calls their production system "prd". On a machine serving several
    # customers the aliases carry no distinguishing information, so without a tenant two customers'
    # extract caches and snapshot stores resolve to the same files - and nothing fails, the wrong
    # data is simply there. Setting it separates them at rest and labels them in every report.
    tenant: str | None = None
    # Which environment this points at, declared rather than guessed. The server never infers it
    # from an alias or a host name: `prd_copy` would read as production and `production_2` would
    # not, and being wrong here means someone reads production figures believing they are QA.
    environment: Environment = "unknown"
    abap_schema: str = ABAP_SCHEMA_AUTO
    encrypt: bool = True
    # TLS certificate handling (only relevant when encrypt is true). Validation defaults to ON;
    # relax it (with a trust store, or explicitly disabling) only for internal/self-signed hosts.
    ssl_validate_certificate: bool = True
    ssl_trust_store: str | None = None
    read_only_user: bool = True
    # How this user was provisioned, declared rather than inferred - the same rule as `environment`.
    # The server observes what it was actually allowed to read and compares the two, so a profile
    # claiming a full technical read whose dictionary probe is refused reports a provisioning fault
    # instead of quietly answering less. See docs/deployment-modes.md.
    access_mode: AccessMode = "unknown"
    # Ceiling on concurrent connections for this system. MCP tool functions run in a worker
    # threadpool, so simultaneous calls need separate connections — a driver connection cannot be
    # shared across threads. Growth is lazy: one analyst working sequentially only ever opens one.
    # Raise it for a shared install, lower it to 1 where the database limits sessions per user.
    pool_size: int = Field(default=4, ge=1, le=32)
    # Bound the connect attempt so an unreachable host fails fast rather than hanging a tool call.
    connect_timeout_seconds: float = Field(default=30.0, ge=0)
    # Driver-level inactivity bound, best-effort: it depends on driver and server behaviour, so the
    # per-call query budget remains the actual guarantee. 0 disables it.
    communication_timeout_seconds: float = Field(default=0.0, ge=0)
    # A statement slower than this is logged at WARNING regardless of level, because on a landscape
    # you cannot log into, the slow query is what you need to see without enabling debug output.
    slow_query_ms: float = Field(default=5_000.0, ge=0)
    # Whether extracted metadata may be cached on local disk. Structural extracts include ABAP
    # routine source and query definitions — customer intellectual property at rest — so an
    # organisation that will not accept that can turn it off per system and pay the re-read cost.
    cache_enabled: bool = True

    @property
    def resolve_schema_at_connect(self) -> bool:
        """True when the ABAP schema must be discovered at connect time."""
        return self.abap_schema == ABAP_SCHEMA_AUTO

    @property
    def identity(self) -> StorageIdentity:
        """Who this profile is, for isolating its stored data and for labelling its answers."""
        return StorageIdentity(system=self.name, tenant=self.tenant, environment=self.environment)


class EccProfile(BaseModel):
    """One ABAP source system reachable over ADT (SAP's HTTP metadata/source service).

    Used only to read extractor-exit ABAP, which exists in the source system and not in BW. The
    transport is HTTP, so two safety properties are configuration-level rather than driver-level:

    * ``use_tls`` defaults to true. Turning it off needs ``allow_plain_http: true`` as a separate,
      explicit opt-in, because Basic auth over plain HTTP puts the password on the wire.
    * ``client`` is the ABAP client (``sap-client``), sent as a request parameter. A wrong client
      silently reads a different client's code, so it is required rather than defaulted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    # Which BW profiles this source system feeds, by profile name (e.g. ["prd"]).
    #
    # Without this, a landscape with more than one ECC profile leaves the connector-gated scenarios
    # permanently unpopulated. The resolver correctly refused to guess between profiles, but the
    # consequence was scenario 9.6 reporting "connector not configured" on a landscape where a
    # working connector existed. Declaring the mapping makes the choice explicit and auditable
    # rather than either guessed or abandoned. Empty means "do not use for any BW system", which is
    # the right default for a sandbox that must never be mistaken for the real source.
    serves: list[str] = Field(default_factory=list)
    port: int = Field(gt=0, lt=65536)
    client: str = Field(pattern=r"^[0-9]{3}$")
    user: str
    password: SecretStr = Field(repr=False)
    use_tls: bool = True
    # Separate, deliberate opt-in: without it, use_tls=False is rejected outright.
    allow_plain_http: bool = False
    ssl_validate_certificate: bool = True
    # A second deliberate opt-in, and a much smaller concession than allow_plain_http. Older SAP
    # ICM releases offer only static-RSA cipher suites, which OpenSSL 3.x excludes at its default
    # security level, so the TLS handshake fails outright even though the certificate is valid and
    # the bulk cipher is AES-GCM. Setting this admits those suites: the channel stays encrypted and
    # the certificate is still checked, and what is given up is forward secrecy. Off by default so
    # the weaker handshake is always a recorded choice.
    allow_legacy_tls_ciphers: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # ADT service root. Configurable because some landscapes expose ICF nodes under a prefix.
    adt_root: str = "/sap/bc/adt"
    # Extra prefixes for per-DataSource "satellite" exit programs, in any customer-namespace form:
    # "Z...", "Y..." or "/PARTNER/...".
    #
    # A common site pattern is for ZXRSAU0n to hold no logic of its own but to build a program name
    # from the DataSource and dispatch into it (PERFORM ... IN PROGRAM (name)). The analyser derives
    # these prefixes from the exit source when it can, so this is a supplement rather than the only
    # route: it covers a site whose name is built in a way the parser cannot read. Empty is correct
    # for a landscape that does not use the pattern - satellite resolution then costs no requests.
    #
    # No convention is assumed or defaulted, because there is no common one. Sites differ, and some
    # use a different prefix per DataSource kind - one for transaction data, another for master
    # data. That is handled without configuration: a derived prefix is attributed to the exit slot
    # whose dispatch produced it, so the distinction is reported rather than flattened.
    satellite_program_prefixes: list[str] = Field(default_factory=list)
    # Ceiling on satellite ADT round trips per inventory call. Each candidate is one GET against a
    # production source system, and a large landscape can offer over a thousand, so an accidental
    # sweep is a real operational cost rather than just a slow answer. Raise it deliberately when
    # full coverage is wanted; when it binds, the shortfall is reported as a caveat, never as "no
    # satellite exists".
    max_satellite_fetches: int = Field(default=400, ge=0, le=20000)

    @model_validator(mode="after")
    def _require_plain_http_opt_in(self) -> EccProfile:
        if not self.use_tls and not self.allow_plain_http:
            raise ValueError(
                f"ECC profile '{self.name}' sets use_tls: false, which sends the password over an "
                "unencrypted connection. Set allow_plain_http: true to accept that explicitly, or "
                "leave use_tls: true"
            )
        return self

    @property
    def base_url(self) -> str:
        """Scheme/host/port root. Held internally only; never returned in a tool response."""
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{self.host}:{self.port}"


def _resolve_field(value: str, env: Mapping[str, str], *, field: str, profile: str) -> str:
    """Resolve a ``${VAR}`` reference from ``env``; return literals unchanged."""
    match = _VAR_RE.match(value)
    if match is None:
        return value
    var = match.group(1)
    if var not in env:
        raise ProfileConfigError(
            f"environment variable {var} referenced by profile '{profile}' "
            f"field '{field}' is not set"
        )
    return env[var]


def _resolve_str(
    raw: Mapping[str, Any],
    field: str,
    env: Mapping[str, str],
    *,
    profile: str,
    required: bool,
) -> str | None:
    """Read one string field with ``${VAR}`` interpolation, enforcing required-ness."""
    val = raw.get(field)
    if val is None:
        if required:
            raise ProfileConfigError(f"profile '{profile}' is missing required field '{field}'")
        return None
    if not isinstance(val, str):
        return str(val)
    return _resolve_field(val, env, field=field, profile=profile)


def _resolve_port(raw: Mapping[str, Any], env: Mapping[str, str], *, profile: str) -> int:
    """Port may be an int or a ``${VAR}`` reference resolving to an int-like string."""
    raw_port = raw.get("port")
    if isinstance(raw_port, str):
        raw_port = _resolve_field(raw_port, env, field="port", profile=profile)
    try:
        return int(raw_port)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProfileConfigError(f"profile '{profile}' has an invalid or missing 'port'") from exc


def _resolve_password(raw: Mapping[str, Any], env: Mapping[str, str], *, profile: str) -> str:
    """Passwords must be ``${VAR}`` references. The value is never echoed on error."""
    raw_pw = raw.get("password")
    if not isinstance(raw_pw, str) or _VAR_RE.match(raw_pw) is None:
        raise ProfileConfigError(
            f"password for profile '{profile}' must be an environment-variable reference "
            "like ${VAR}; inline secrets are not allowed"
        )
    return _resolve_field(raw_pw, env, field="password", profile=profile)


#: Fields this loader resolves itself, because each needs more than a copy: secret handling,
#: ``${VAR}`` interpolation into a non-string type, or a sentinel default.
_EXPLICIT_PROFILE_FIELDS: frozenset[str] = frozenset(
    {"name", "host", "port", "user", "password", "abap_schema"}
)


def _build_profile(name: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> Profile:
    """Build one :class:`Profile` from its YAML block.

    Everything not in :data:`_EXPLICIT_PROFILE_FIELDS` is passed through by name and validated by
    pydantic, rather than enumerated here. That is deliberate and was a bug fix: the previous
    hand-written list silently dropped eight documented options, ``cache_enabled: false`` among
    them - so a customer who had turned the on-disk cache off still had ABAP routine source
    written to disk, and nothing reported it. A field added to the model now reaches the loader
    automatically, and an unknown key is an error instead of silence.
    """
    if not isinstance(raw, Mapping):
        raise ProfileConfigError(f"profile '{name}' must be a mapping")

    unknown = sorted(set(map(str, raw)) - set(Profile.model_fields))
    if unknown:
        raise ProfileConfigError(
            f"profile '{name}' has unrecognised setting(s): {', '.join(unknown)}. "
            "A misspelled option would otherwise be ignored without warning."
        )

    password = _resolve_password(raw, env, profile=name)
    host = _resolve_str(raw, "host", env, profile=name, required=True)
    user = _resolve_str(raw, "user", env, profile=name, required=True)
    abap_schema = _resolve_str(raw, "abap_schema", env, profile=name, required=False)

    passthrough: dict[str, Any] = {}
    for field, value in raw.items():
        key = str(field)
        if key in _EXPLICIT_PROFILE_FIELDS:
            continue
        # A string value may interpolate an environment variable, so `tenant: ${CUSTOMER}` works
        # the same way every other string field does.
        passthrough[key] = (
            _resolve_field(value, env, field=key, profile=name) if isinstance(value, str) else value
        )

    try:
        return Profile(
            name=name,
            host=host,  # type: ignore[arg-type]
            port=_resolve_port(raw, env, profile=name),
            user=user,  # type: ignore[arg-type]
            password=SecretStr(password),
            abap_schema=abap_schema or ABAP_SCHEMA_AUTO,
            **passthrough,
        )
    except ValueError as exc:
        # Pydantic names the field and the rejected value. Safe: the password is already a
        # SecretStr by this point, so it renders masked rather than as its value.
        raise ProfileConfigError(f"profile '{name}' is invalid: {exc}") from exc


def _build_ecc_profile(name: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> EccProfile:
    if not isinstance(raw, Mapping):
        raise ProfileConfigError(f"ECC profile '{name}' must be a mapping")

    unknown = sorted(set(map(str, raw)) - set(EccProfile.model_fields))
    if unknown:
        raise ProfileConfigError(
            f"ECC profile '{name}' has unrecognised setting(s): {', '.join(unknown)}. "
            "A misspelled option would otherwise be ignored without warning."
        )

    password = _resolve_password(raw, env, profile=name)
    host = _resolve_str(raw, "host", env, profile=name, required=True)
    user = _resolve_str(raw, "user", env, profile=name, required=True)
    client = _resolve_str(raw, "client", env, profile=name, required=True)
    adt_root = _resolve_str(raw, "adt_root", env, profile=name, required=False)

    try:
        return EccProfile(
            name=name,
            host=host,  # type: ignore[arg-type]
            serves=[str(s) for s in (raw.get("serves") or [])],
            port=_resolve_port(raw, env, profile=name),
            client=client,  # type: ignore[arg-type]
            user=user,  # type: ignore[arg-type]
            password=SecretStr(password),
            use_tls=bool(raw.get("use_tls", True)),
            allow_plain_http=bool(raw.get("allow_plain_http", False)),
            ssl_validate_certificate=bool(raw.get("ssl_validate_certificate", True)),
            allow_legacy_tls_ciphers=bool(raw.get("allow_legacy_tls_ciphers", False)),
            timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
            adt_root=adt_root or "/sap/bc/adt",
            satellite_program_prefixes=[
                str(p).strip().upper()
                for p in (raw.get("satellite_program_prefixes") or [])
                if str(p).strip()
            ],
            max_satellite_fetches=int(raw.get("max_satellite_fetches", 400)),
        )
    except ValueError as exc:
        # Pydantic validation text is safe here: it names fields, never the password value.
        raise ProfileConfigError(f"ECC profile '{name}' is invalid: {exc}") from exc


class ProfileManager:
    """Loads and resolves connection profiles from a YAML file.

    ``env`` is injectable for testing; it defaults to ``os.environ``. Profiles are parsed and
    interpolated eagerly at construction so configuration errors surface immediately.
    """

    def __init__(
        self,
        profiles_path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if env is None:
            env = os.environ
            self._load_local_dotenv(env)
        self._env: Mapping[str, str] = env
        resolved = profiles_path or self._env.get("BW_PROFILES_PATH")
        if not resolved:
            raise ProfileConfigError("no profiles path given and BW_PROFILES_PATH is not set")
        self._path = Path(resolved)
        raw = self._read()
        self._profiles: dict[str, Profile] = self._load_systems(raw)
        self._ecc_profiles: dict[str, EccProfile] = self._load_ecc_systems(raw)
        self._bi_inventory: str | None = self._load_bi_systems(raw)

    @staticmethod
    def _load_local_dotenv(env: Mapping[str, str]) -> None:
        """Populate missing environment variables from the first local .env file that exists.

        This mirrors the server bootstrap behavior so ProfileManager can resolve profiles when the
        process only has the workspace config files, not a pre-populated shell environment.
        """
        target: MutableMapping[str, str] | None = None
        if isinstance(env, MutableMapping):
            target = env

        candidates: list[Path] = []
        explicit = os.environ.get("BW_DOTENV_PATH")
        if explicit:
            candidates.append(Path(explicit).expanduser())
        profiles = os.environ.get("BW_PROFILES_PATH")
        if profiles:
            candidates.append(Path(profiles).expanduser().resolve().parent / ".env")
        candidates.append(Path.cwd() / ".env")

        for path in candidates:
            try:
                if not path.is_file():
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                resolved = value.strip().strip('"').strip("'")
                if target is not None:
                    target.setdefault(key.strip(), resolved)
                else:
                    os.environ.setdefault(key.strip(), resolved)
            return

    def _read(self) -> Mapping[str, Any]:
        if not self._path.is_file():
            raise ProfileConfigError(f"profiles file not found: {self._path}")
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ProfileConfigError(f"could not parse profiles file: {self._path}") from exc
        if not isinstance(data, Mapping) or "systems" not in data:
            raise ProfileConfigError("profiles file must contain a top-level 'systems' mapping")
        return data

    def _load_systems(self, data: Mapping[str, Any]) -> dict[str, Profile]:
        systems = data["systems"]
        if not isinstance(systems, Mapping) or not systems:
            raise ProfileConfigError("'systems' must be a non-empty mapping")
        return {
            str(name): _build_profile(str(name), raw, self._env) for name, raw in systems.items()
        }

    def _load_ecc_systems(self, data: Mapping[str, Any]) -> dict[str, EccProfile]:
        """Parse the optional ``ecc_systems`` block. Absent means "no ABAP source system"."""
        systems = data.get("ecc_systems")
        if systems is None:
            return {}
        if not isinstance(systems, Mapping):
            raise ProfileConfigError("'ecc_systems' must be a mapping when present")
        return {
            str(name): _build_ecc_profile(str(name), raw, self._env)
            for name, raw in systems.items()
        }

    def _load_bi_systems(self, data: Mapping[str, Any]) -> str | None:
        """Parse the optional ``bi_systems`` block: an inventory file path, nothing more.

        Only a path is accepted on purpose. Report schedules and dashboard sources are the same
        shape whichever platform produced them, and taking an exported file rather than platform
        credentials keeps this server's trust boundary at "read-only, BW only".
        """
        systems = data.get("bi_systems")
        if systems is None:
            return None
        if not isinstance(systems, Mapping):
            raise ProfileConfigError("'bi_systems' must be a mapping when present")
        raw_path = systems.get("inventory_path") or systems.get("path")
        if raw_path is None:
            raise ProfileConfigError("'bi_systems' needs an 'inventory_path' entry")
        return _resolve_field(
            str(raw_path), self._env, field="inventory_path", profile="bi_systems"
        )

    def names(self) -> list[str]:
        """Configured profile names."""
        return sorted(self._profiles)

    def get(self, name: str) -> Profile:
        """Return a profile by name, or raise :class:`ProfileNotFoundError`."""
        try:
            return self._profiles[name]
        except KeyError:
            raise ProfileNotFoundError(name, self.names()) from None

    def ecc_names(self) -> list[str]:
        """Configured ABAP source-system profile names (may be empty)."""
        return sorted(self._ecc_profiles)

    def get_ecc(self, name: str) -> EccProfile:
        """Return an ABAP source-system profile, or raise :class:`ProfileNotFoundError`."""
        try:
            return self._ecc_profiles[name]
        except KeyError:
            raise ProfileNotFoundError(name, self.ecc_names()) from None

    def bi_inventory_path(self) -> str | None:
        """Path to the BI reporting inventory, when a ``bi_systems`` block configures one.

        Vendor-neutral by design: the file is an export from whatever BI platform the organisation
        runs, so scenarios 9.7/9.8 work without this server holding BI credentials.
        """
        return self._bi_inventory
