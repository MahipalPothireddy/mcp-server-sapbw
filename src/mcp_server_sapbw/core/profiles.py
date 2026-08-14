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
    abap_schema: str = ABAP_SCHEMA_AUTO
    encrypt: bool = True
    # TLS certificate handling (only relevant when encrypt is true). Validation defaults to ON;
    # relax it (with a trust store, or explicitly disabling) only for internal/self-signed hosts.
    ssl_validate_certificate: bool = True
    ssl_trust_store: str | None = None
    read_only_user: bool = True

    @property
    def resolve_schema_at_connect(self) -> bool:
        """True when the ABAP schema must be discovered at connect time."""
        return self.abap_schema == ABAP_SCHEMA_AUTO


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


def _build_profile(name: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> Profile:
    if not isinstance(raw, Mapping):
        raise ProfileConfigError(f"profile '{name}' must be a mapping")

    password = _resolve_password(raw, env, profile=name)
    host = _resolve_str(raw, "host", env, profile=name, required=True)
    user = _resolve_str(raw, "user", env, profile=name, required=True)
    abap_schema = _resolve_str(raw, "abap_schema", env, profile=name, required=False)

    return Profile(
        name=name,
        host=host,  # type: ignore[arg-type]
        port=_resolve_port(raw, env, profile=name),
        user=user,  # type: ignore[arg-type]
        password=SecretStr(password),
        abap_schema=abap_schema or ABAP_SCHEMA_AUTO,
        encrypt=bool(raw.get("encrypt", True)),
        ssl_validate_certificate=bool(raw.get("ssl_validate_certificate", True)),
        ssl_trust_store=_resolve_str(raw, "ssl_trust_store", env, profile=name, required=False),
        read_only_user=bool(raw.get("read_only_user", True)),
    )


def _build_ecc_profile(name: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> EccProfile:
    if not isinstance(raw, Mapping):
        raise ProfileConfigError(f"ECC profile '{name}' must be a mapping")

    password = _resolve_password(raw, env, profile=name)
    host = _resolve_str(raw, "host", env, profile=name, required=True)
    user = _resolve_str(raw, "user", env, profile=name, required=True)
    client = _resolve_str(raw, "client", env, profile=name, required=True)
    adt_root = _resolve_str(raw, "adt_root", env, profile=name, required=False)

    try:
        return EccProfile(
            name=name,
            host=host,  # type: ignore[arg-type]
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
