"""Connection profile loading and ``${VAR}`` interpolation.

Profiles live in a git-ignored ``profiles.yaml`` (path from ``BW_PROFILES_PATH``). Secrets are
never inline: the ``password`` field must be an environment-variable reference (``${VAR}``), and
any field may use ``${VAR}`` interpolation. ``abap_schema: auto`` is preserved as a sentinel to be
resolved later by the capability resolver — never hardcoded (mission Section 3, Rules 1 and 5).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    read_only_user: bool = True

    @property
    def resolve_schema_at_connect(self) -> bool:
        """True when the ABAP schema must be discovered at connect time."""
        return self.abap_schema == ABAP_SCHEMA_AUTO


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


def _build_profile(name: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> Profile:
    if not isinstance(raw, Mapping):
        raise ProfileConfigError(f"profile '{name}' must be a mapping")

    # Password: must be a ${VAR} reference. Never echo the value on error.
    raw_pw = raw.get("password")
    if not isinstance(raw_pw, str) or _VAR_RE.match(raw_pw) is None:
        raise ProfileConfigError(
            f"password for profile '{name}' must be an environment-variable reference "
            "like ${VAR}; inline secrets are not allowed"
        )
    password = _resolve_field(raw_pw, env, field="password", profile=name)

    def resolve_str(field: str, *, required: bool) -> str | None:
        val = raw.get(field)
        if val is None:
            if required:
                raise ProfileConfigError(f"profile '{name}' is missing required field '{field}'")
            return None
        if not isinstance(val, str):
            return str(val)
        return _resolve_field(val, env, field=field, profile=name)

    host = resolve_str("host", required=True)
    user = resolve_str("user", required=True)
    abap_schema = resolve_str("abap_schema", required=False) or ABAP_SCHEMA_AUTO

    # Port may be an int or a ${VAR} reference resolving to an int-like string.
    raw_port = raw.get("port")
    if isinstance(raw_port, str):
        raw_port = _resolve_field(raw_port, env, field="port", profile=name)
    try:
        port = int(raw_port)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProfileConfigError(f"profile '{name}' has an invalid or missing 'port'") from exc

    return Profile(
        name=name,
        host=host,  # type: ignore[arg-type]
        port=port,
        user=user,  # type: ignore[arg-type]
        password=SecretStr(password),
        abap_schema=abap_schema,
        encrypt=bool(raw.get("encrypt", True)),
        read_only_user=bool(raw.get("read_only_user", True)),
    )


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
        self._env: Mapping[str, str] = env if env is not None else os.environ
        resolved = profiles_path or self._env.get("BW_PROFILES_PATH")
        if not resolved:
            raise ProfileConfigError("no profiles path given and BW_PROFILES_PATH is not set")
        self._path = Path(resolved)
        self._profiles: dict[str, Profile] = self._load()

    def _load(self) -> dict[str, Profile]:
        if not self._path.is_file():
            raise ProfileConfigError(f"profiles file not found: {self._path}")
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ProfileConfigError(f"could not parse profiles file: {self._path}") from exc
        if not isinstance(data, Mapping) or "systems" not in data:
            raise ProfileConfigError("profiles file must contain a top-level 'systems' mapping")
        systems = data["systems"]
        if not isinstance(systems, Mapping) or not systems:
            raise ProfileConfigError("'systems' must be a non-empty mapping")
        return {
            str(name): _build_profile(str(name), raw, self._env) for name, raw in systems.items()
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
