"""Tests for the connection profile manager (B1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server_sapbw.core.profiles import (
    ProfileConfigError,
    ProfileManager,
    ProfileNotFoundError,
)

_GOOD_YAML = """
systems:
  qa:
    host: ${BW_QA_HOST}
    port: 30015
    user: ${BW_QA_USER}
    password: ${BW_QA_PASSWORD}
    abap_schema: auto
    encrypt: true
    read_only_user: true
  prd:
    host: prd.example.invalid
    port: 30015
    user: ${BW_PRD_USER}
    password: ${BW_PRD_PASSWORD}
    abap_schema: SAPHANADB
    read_only_user: true
"""

_ENV = {
    "BW_QA_HOST": "qa.example.invalid",
    "BW_QA_USER": "qa_ro",
    "BW_QA_PASSWORD": "s3cr3t-qa",
    "BW_PRD_USER": "prd_ro",
    "BW_PRD_PASSWORD": "s3cr3t-prd",
}


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_and_interpolates(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    qa = mgr.get("qa")
    assert qa.host == "qa.example.invalid"
    assert qa.user == "qa_ro"
    assert qa.password.get_secret_value() == "s3cr3t-qa"
    assert qa.port == 30015
    assert qa.read_only_user is True


def test_abap_schema_auto_sentinel(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    assert mgr.get("qa").resolve_schema_at_connect is True
    assert mgr.get("prd").resolve_schema_at_connect is False


def test_password_never_exposed_in_representations(tmp_path: Path) -> None:
    """The password value must not appear in repr/str/model_dump output (mission Rule 5)."""
    qa = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV).get("qa")
    secret = "s3cr3t-qa"
    assert secret not in repr(qa)
    assert secret not in str(qa)
    assert secret not in str(qa.model_dump())
    assert secret not in qa.model_dump_json()
    # The value is still retrievable through the explicit accessor (used only by the driver).
    assert qa.password.get_secret_value() == secret


def test_literal_host_allowed(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    assert mgr.get("prd").host == "prd.example.invalid"


def test_names_sorted(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    assert mgr.names() == ["prd", "qa"]


def test_unknown_profile_lists_available(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    with pytest.raises(ProfileNotFoundError) as excinfo:
        mgr.get("dev")
    message = str(excinfo.value)
    assert "dev" in message
    assert "prd" in message and "qa" in message


def test_password_not_leaked_in_repr(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    qa = mgr.get("qa")
    assert "s3cr3t-qa" not in repr(qa)
    assert "s3cr3t-qa" not in str(qa)


def test_inline_literal_password_rejected(tmp_path: Path) -> None:
    bad = """
systems:
  qa:
    host: qa.example.invalid
    port: 30015
    user: qa_ro
    password: hunter2
"""
    with pytest.raises(ProfileConfigError) as excinfo:
        ProfileManager(_write(tmp_path, bad), env=_ENV)
    # The offending literal must not be echoed in the error.
    assert "hunter2" not in str(excinfo.value)
    assert "password" in str(excinfo.value)


def test_missing_env_var_names_only_the_var(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError) as excinfo:
        ProfileManager(_write(tmp_path, _GOOD_YAML), env={"BW_QA_HOST": "h"})
    assert "BW_QA_USER" in str(excinfo.value) or "BW_QA_PASSWORD" in str(excinfo.value)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError):
        ProfileManager(tmp_path / "nope.yaml", env=_ENV)


def test_no_path_and_no_env() -> None:
    with pytest.raises(ProfileConfigError):
        ProfileManager(env={})


def test_missing_systems_key(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigError):
        ProfileManager(_write(tmp_path, "foo: bar\n"), env=_ENV)


def test_path_from_env(tmp_path: Path) -> None:
    path = _write(tmp_path, _GOOD_YAML)
    env = {**_ENV, "BW_PROFILES_PATH": str(path)}
    mgr = ProfileManager(env=env)
    assert mgr.get("qa").host == "qa.example.invalid"


def test_ssl_defaults_to_validation_on(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    qa = mgr.get("qa")
    assert qa.ssl_validate_certificate is True
    assert qa.ssl_trust_store is None


def test_ssl_options_parsed(tmp_path: Path) -> None:
    yaml_text = """
systems:
  qa:
    host: ${BW_QA_HOST}
    port: 30015
    user: ${BW_QA_USER}
    password: ${BW_QA_PASSWORD}
    encrypt: true
    ssl_validate_certificate: false
    ssl_trust_store: /etc/ssl/internal-ca.pem
"""
    qa = ProfileManager(_write(tmp_path, yaml_text), env=_ENV).get("qa")
    assert qa.ssl_validate_certificate is False
    assert qa.ssl_trust_store == "/etc/ssl/internal-ca.pem"
