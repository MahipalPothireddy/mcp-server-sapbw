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

# Synthetic test values only (not real credentials); the allowlist pragmas mark them so the
# detect-secrets CI scan does not flag the password-keyword pattern.
_ENV = {
    "BW_QA_HOST": "qa.example.invalid",
    "BW_QA_USER": "qa_ro",
    "BW_QA_PASSWORD": "s3cr3t-qa",  # pragma: allowlist secret
    "BW_PRD_USER": "prd_ro",
    "BW_PRD_PASSWORD": "s3cr3t-prd",  # pragma: allowlist secret
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
    secret = "s3cr3t-qa"  # pragma: allowlist secret
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


def test_loads_local_dotenv_when_no_env_is_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BW_PROFILES_PATH=./profiles.yaml\n"
        "BW_QA_HOST=qa.example.invalid\n"
        "BW_QA_USER=qa_ro\n"
        "BW_QA_PASSWORD=s3cr3t-qa\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles.yaml").write_text(
        """
systems:
  qa:
    host: ${BW_QA_HOST}
    port: 30015
    user: ${BW_QA_USER}
    password: ${BW_QA_PASSWORD}
    abap_schema: auto
    encrypt: true
    read_only_user: true
""",
        encoding="utf-8",
    )
    for key in ["BW_PROFILES_PATH", "BW_QA_HOST", "BW_QA_USER", "BW_QA_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)

    mgr = ProfileManager()

    assert mgr.get("qa").host == "qa.example.invalid"
    assert mgr.get("qa").user == "qa_ro"
    assert mgr.get("qa").password.get_secret_value() == "s3cr3t-qa"


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


# --- optional ABAP source systems (ecc_systems) -----------------------------------------------

_ECC_YAML = """
systems:
  qa:
    host: ${BW_QA_HOST}
    port: 30015
    user: ${BW_QA_USER}
    password: ${BW_QA_PASSWORD}
ecc_systems:
  src:
    host: ${ECC_HOST}
    port: 44300
    client: "300"
    user: ${ECC_USER}
    password: ${ECC_PASSWORD}
"""

_ECC_ENV = {
    **_ENV,
    "ECC_HOST": "src.example.invalid",
    "ECC_USER": "src_ro",
    "ECC_PASSWORD": "s3cr3t-src",  # pragma: allowlist secret
}


def test_ecc_systems_is_optional(tmp_path: Path) -> None:
    """A profiles file with no ecc_systems block is valid and simply has no source systems."""
    mgr = ProfileManager(_write(tmp_path, _GOOD_YAML), env=_ENV)
    assert mgr.ecc_names() == []


def test_ecc_profile_is_loaded_and_interpolated(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _ECC_YAML), env=_ECC_ENV)
    assert mgr.ecc_names() == ["src"]
    src = mgr.get_ecc("src")
    assert src.host == "src.example.invalid"
    assert src.client == "300"
    assert src.password.get_secret_value() == "s3cr3t-src"
    assert src.use_tls is True  # secure by default
    assert src.base_url == "https://src.example.invalid:44300"


def test_unknown_ecc_profile_lists_the_configured_ones(tmp_path: Path) -> None:
    mgr = ProfileManager(_write(tmp_path, _ECC_YAML), env=_ECC_ENV)
    with pytest.raises(ProfileNotFoundError) as excinfo:
        mgr.get_ecc("nope")
    assert "src" in str(excinfo.value)


def test_ecc_inline_password_is_rejected(tmp_path: Path) -> None:
    bad = _ECC_YAML.replace("password: ${ECC_PASSWORD}", "password: literal-not-allowed")
    with pytest.raises(ProfileConfigError, match="environment-variable reference"):
        ProfileManager(_write(tmp_path, bad), env=_ECC_ENV)


def test_ecc_plain_http_without_opt_in_is_rejected(tmp_path: Path) -> None:
    bad = _ECC_YAML + "    use_tls: false\n"
    with pytest.raises(ProfileConfigError, match="allow_plain_http"):
        ProfileManager(_write(tmp_path, bad), env=_ECC_ENV)


def test_ecc_plain_http_with_opt_in_is_accepted(tmp_path: Path) -> None:
    ok = _ECC_YAML + "    use_tls: false\n    allow_plain_http: true\n"
    mgr = ProfileManager(_write(tmp_path, ok), env=_ECC_ENV)
    assert mgr.get_ecc("src").base_url.startswith("http://")


def test_ecc_client_must_be_three_digits(tmp_path: Path) -> None:
    bad = _ECC_YAML.replace('client: "300"', 'client: "3000"')
    with pytest.raises(ProfileConfigError, match="invalid"):
        ProfileManager(_write(tmp_path, bad), env=_ECC_ENV)


def test_ecc_error_never_echoes_the_password(tmp_path: Path) -> None:
    bad = _ECC_YAML.replace('client: "300"', 'client: "bad"')
    with pytest.raises(ProfileConfigError) as excinfo:
        ProfileManager(_write(tmp_path, bad), env=_ECC_ENV)
    assert "s3cr3t-src" not in str(excinfo.value)


# --- ECC profile -> BW system declaration ------------------------------------------------
#
# A landscape with several ECC profiles used to leave the connector-gated scenarios permanently
# unpopulated: the resolver refused to guess which source feeds which BW system, which was right,
# but the consequence was "connector not configured" on a landscape where a connector worked.


# Distinct name: _ECC_ENV is already defined above for the shared ECC fixtures, and redefining it
# here would shadow it for every test in the file.
_SERVES_ENV = {"PW": "secret", "EPW": "esecret", "SPW": "ssecret"}


def _write_serves_profiles(tmp_path: Path, ecc_block: str) -> Path:
    """Minimal valid profiles file. Passwords are ${VAR} refs because inline ones are refused."""
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        "systems:\n"
        "  prd:\n"
        "    host: h\n"
        "    port: 30015\n"
        "    user: u\n"
        "    password: ${PW}\n"
        "ecc_systems:\n" + ecc_block,
        encoding="utf-8",
    )
    return profiles


def test_ecc_profile_serves_defaults_to_empty(tmp_path: Path) -> None:
    """Empty is the safe default: a sandbox must not be picked up as a real source."""
    profiles = _write_serves_profiles(
        tmp_path,
        "  ecc_sandbox:\n"
        "    host: sh\n    port: 8001\n    client: '300'\n    user: su\n    password: ${SPW}\n",
    )
    manager = ProfileManager(profiles, env=_SERVES_ENV)
    assert manager.get_ecc("ecc_sandbox").serves == []


def test_ecc_profile_declares_which_bw_systems_it_serves(tmp_path: Path) -> None:
    profiles = _write_serves_profiles(
        tmp_path,
        "  ecc_dev:\n"
        "    host: eh\n    port: 8443\n    client: '300'\n    user: eu\n    password: ${EPW}\n"
        "    serves: [prd]\n"
        "  ecc_sandbox:\n"
        "    host: sh\n    port: 8001\n    client: '300'\n    user: su\n    password: ${SPW}\n",
    )
    manager = ProfileManager(profiles, env=_SERVES_ENV)
    assert manager.get_ecc("ecc_dev").serves == ["prd"]
    # The sandbox declares nothing, so it can never be resolved as the source for a BW system.
    assert manager.get_ecc("ecc_sandbox").serves == []
