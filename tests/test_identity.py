"""Storage identity: the guarantee that two customers' data cannot land in one file.

The old rule mapped every awkward character to an underscore, which is lossy, and lossy is fatal for
an identity. Two measured consequences:

* ``prd/eu``, ``prd_eu``, ``prd.eu``, ``prd eu`` and ``prd:eu`` all produced ``prd_eu.sqlite``, so
  two profiles shared one cache and one snapshot store with nothing failing;
* the runtime built its own cache path and skipped the sanitiser entirely, so ``../../escaped``
  wrote customer metadata outside the cache root.

These tests pin the two properties that fix it - **injective** and **contained** - plus the tenant
that separates two customers who both, reasonably, call their production system ``prd``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server_sapbw.core.identity import StorageIdentity, storage_key
from mcp_server_sapbw.core.paths import cache_file
from mcp_server_sapbw.core.snapshots import snapshot_file

#: Aliases that the old sanitiser collapsed onto one name. The regression this module exists for.
_COLLIDED = ["prd/eu", "prd_eu", "prd.eu", "prd eu", "prd:eu", "prd-eu", "PRD_EU", "prd\\eu"]


# --- injective: distinct identities never share a stem ------------------------------------


def test_aliases_the_old_rule_collapsed_are_now_distinct() -> None:
    keys = [storage_key(alias) for alias in _COLLIDED]
    assert len(set(keys)) == len(_COLLIDED), f"still colliding: {sorted(keys)}"


def test_a_tenant_separates_two_customers_using_the_same_alias() -> None:
    """The isolation case. Without a tenant these are one identity, and nothing says so."""
    assert storage_key("prd", "acme") != storage_key("prd", "globex")
    assert storage_key("prd", "acme") != storage_key("prd")


def test_the_same_identity_always_yields_the_same_key() -> None:
    """It names a file that must still be found after a restart, so it cannot drift."""
    assert storage_key("prd", "acme") == storage_key("prd", "acme")


def test_a_tenant_and_alias_pair_is_not_confusable_by_concatenation() -> None:
    """`acme` + `prd` must not equal `acmeprd`, or two tenants could still meet.

    The separator inside the hashed value is what prevents it; without one, ('ac','meprd') and
    ('acme','prd') hash identically.
    """
    assert storage_key("prd", "acme") != storage_key("meprd", "ac")
    assert storage_key("prd", "acme") != storage_key("acmeprd")


# --- contained: an operator-supplied alias cannot traverse --------------------------------


@pytest.mark.parametrize(
    "alias",
    ["../../escaped", "..\\..\\escaped", "/etc/passwd", "a/b/c", "..", ".", "con:", "  "],
)
def test_a_key_is_always_a_single_safe_path_component(alias: str) -> None:
    key = storage_key(alias)
    assert "/" not in key
    assert "\\" not in key
    assert ".." not in key
    assert key.strip() == key
    assert key, "an empty stem would collide with every other empty one"


def test_an_empty_alias_still_yields_a_usable_key() -> None:
    assert storage_key("   ").startswith("profile-")
    assert storage_key("///").startswith("profile-")


def test_two_different_unusable_aliases_still_differ() -> None:
    """Both fall back to the same readable prefix, so only the digest separates them."""
    assert storage_key("///") != storage_key("   ")


# --- readable: an operator has to be able to tell whose file it is ------------------------


def test_the_key_leads_with_something_a_person_can_read() -> None:
    """A pure hash would be unauditable: nobody can tell whose metadata a file holds."""
    assert storage_key("prd", "acme").startswith("acme-prd-")
    assert storage_key("qa").startswith("qa-")


def test_a_very_long_alias_is_truncated_but_stays_distinct() -> None:
    """Path length is finite; distinctness is not negotiable, so the digest carries it."""
    long_one, long_two = "x" * 300, "x" * 301
    assert len(storage_key(long_one)) < 60
    assert storage_key(long_one) != storage_key(long_two)


# --- the declared facts -------------------------------------------------------------------


def test_identity_labels_itself_unambiguously_across_tenants() -> None:
    assert StorageIdentity(system="prd", tenant="acme", environment="prod").label == (
        "acme/prd (prod)"
    )
    assert StorageIdentity(system="qa").label == "qa"
    assert StorageIdentity(system="qa", tenant="acme").label == "acme/qa"


def test_environment_is_declared_and_never_inferred_from_the_alias() -> None:
    """The failure this avoids: `prd_copy` read as production, `production_2` not.

    Being wrong here means someone reads production figures believing they are QA, so the server
    takes the operator's word and offers no guess.
    """
    assert StorageIdentity(system="prd_copy").environment == "unknown"
    assert StorageIdentity(system="prd_copy").is_production is False
    assert StorageIdentity(system="anything", environment="prod").is_production is True


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("prod", True), ("preprod", True), ("qa", False), ("unknown", False)],
)
def test_is_production_covers_preprod_too(environment: str, expected: bool) -> None:
    """Pre-production usually holds a copy of production data, so it deserves the same care."""
    identity = StorageIdentity(system="s", environment=environment)  # type: ignore[arg-type]
    assert identity.is_production is expected


def test_isolation_by_tenant_is_reported_not_enforced() -> None:
    """False is correct for a single-customer install, which is the normal case."""
    assert StorageIdentity(system="qa").isolated_by_tenant is False
    assert StorageIdentity(system="qa", tenant="   ").isolated_by_tenant is False
    assert StorageIdentity(system="qa", tenant="acme").isolated_by_tenant is True


def test_the_identity_is_frozen() -> None:
    """It names a file being written to; letting it change underneath would be a defect."""
    identity = StorageIdentity(system="qa", tenant="acme")
    with pytest.raises(AttributeError, match="cannot assign"):
        identity.system = "other"  # type: ignore[misc]


# --- both stores agree ---------------------------------------------------------------------


def test_the_cache_and_the_snapshot_store_share_one_identity_rule() -> None:
    """They had two copies of the sanitising rule, and both copies had the same defect."""
    root = Path("/c")
    for alias in _COLLIDED:
        cache = cache_file(alias, directory=root).name.removesuffix(".sqlite")
        snapshot = snapshot_file(alias, root).name.removesuffix(".snapshots.sqlite")
        assert cache == snapshot == storage_key(alias)
