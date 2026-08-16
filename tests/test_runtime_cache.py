"""Tests that ServerRuntime actually hands a cache to the repositories it builds.

This is the plumbing half of the cache fix. The read paths can be wired perfectly and still cache
nothing if the runtime constructs every repository with ``cache=None`` — which is what it did: the
only ``SqliteCache`` in the process was the throwaway one built inside ``refresh_cache``, so
``bw_refresh_cache`` cleared a store nothing ever wrote to.

Offline: the pool and resolver are fakes, so no BW system is contacted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from mcp_server_sapbw.core.paths import cache_file
from mcp_server_sapbw.core.profiles import Profile
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.snapshot import Snapshot
from mcp_server_sapbw.server import ServerRuntime

SCHEMA = "TESTSCHEMA"


class _Conn:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        return []


class _Pool:
    def acquire(self, profile: Profile) -> _Conn:
        return _Conn()


class _Resolver:
    """Returns a capability record whose ``discovered_at`` is the cache fingerprint."""

    def __init__(self) -> None:
        self.calls = 0
        # "Now", so the record is not expired and capability() does not re-resolve on every call.
        self._base = datetime.now(UTC)

    def resolve(self, profile: Profile, connection: object) -> CapabilityRecord:
        self.calls += 1
        return CapabilityRecord(
            system=profile.name,
            bw_release="7.50",
            abap_schema=SCHEMA,
            # A distinct timestamp per resolve, so an explicit refresh changes the fingerprint.
            discovered_at=self._base + timedelta(microseconds=self.calls),
            tables={
                "transformation": TableStatus(
                    logical_name="transformation",
                    resolved_name="RSTRAN",
                    present=True,
                    schema_name=SCHEMA,
                )
            },
        )


class _Profiles:
    def __init__(
        self, *, cache_enabled: bool = True, tenant: str | None = None, name: str = "qa"
    ) -> None:
        self._cache_enabled = cache_enabled
        self._tenant = tenant
        self._name = name

    def get(self, name: str) -> Profile:
        return Profile(
            name=name,
            host="host.invalid",
            port=30015,
            user="TESTER",
            password=SecretStr("unused-in-this-test"),
            abap_schema=SCHEMA,
            read_only_user=False,
            cache_enabled=self._cache_enabled,
            tenant=self._tenant,
        )

    def names(self) -> list[str]:
        return [self._name]

    def ecc_names(self) -> list[str]:
        return []

    def bi_inventory_path(self) -> str | None:
        return None


def _runtime(
    tmp_path: Path, *, cache_enabled: bool = True, tenant: str | None = None
) -> tuple[ServerRuntime, _Resolver]:
    resolver = _Resolver()
    runtime = ServerRuntime(
        _Profiles(cache_enabled=cache_enabled, tenant=tenant),  # type: ignore[arg-type]
        _Pool(),  # type: ignore[arg-type]
        resolver,  # type: ignore[arg-type]
        cache_dir=tmp_path / "cache",
    )
    return runtime, resolver


def test_repositories_receive_a_cache(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    for repo in (
        runtime.transformations("qa"),
        runtime.queries("qa"),
        runtime.providers("qa"),
        runtime.chains("qa"),
        runtime.hana("qa"),
        runtime.search("qa"),
    ):
        assert repo._cache is not None, f"{type(repo).__name__} was built without a cache"


def test_services_receive_a_cache(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    for service in (
        runtime.lineage("qa"),
        runtime.analyzers("qa"),
        runtime.load_closure("qa"),
        runtime.routine_register("qa"),
        runtime.docgen("qa"),
    ):
        assert service._cache is not None, f"{type(service).__name__} was built without a cache"


def test_security_repository_never_receives_a_cache(tmp_path: Path) -> None:
    """Permission data must not be persisted to disk, at any tier.

    Deliberately the inverse of every other repository: RSECVAL holds which values a named user may
    see, so caching it would widen the blast radius of the cache file and could serve a stale answer
    to "who can see this" after someone changed role or left.
    """
    runtime, _ = _runtime(tmp_path)
    assert runtime.security("qa")._cache is None


def test_same_cache_instance_is_reused_across_repositories(tmp_path: Path) -> None:
    """One SQLite connection per system, so writes by one repository are visible to the next."""
    runtime, _ = _runtime(tmp_path)
    assert runtime.transformations("qa")._cache is runtime.queries("qa")._cache


def test_cache_file_is_created_under_the_cache_dir(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    runtime.transformations("qa")
    written = list((tmp_path / "cache").glob("qa-*.sqlite"))
    assert len(written) == 1, f"expected one cache file, found {written}"


def test_the_runtime_uses_the_same_path_helper_as_everything_else(tmp_path: Path) -> None:
    """The bug this guards: the runtime built `cache_dir / f"{system}.sqlite"` itself.

    That bypassed the sanitising in `cache_file`, so an alias containing `..` wrote customer
    metadata outside the cache root - and the helper that would have prevented it was never called.
    """
    runtime, _ = _runtime(tmp_path)
    runtime.transformations("qa")
    expected = cache_file("qa", directory=tmp_path / "cache")
    assert expected.exists()


def test_an_alias_that_would_traverse_stays_inside_the_cache_directory(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    resolver = _Resolver()
    runtime = ServerRuntime(
        _Profiles(name="../../escaped"),  # type: ignore[arg-type]
        _Pool(),  # type: ignore[arg-type]
        resolver,  # type: ignore[arg-type]
        cache_dir=root,
    )
    assert runtime.transformations("../../escaped")._cache is not None
    written = list(root.rglob("*.sqlite"))
    assert written, "no cache file was created"
    for path in written:
        assert root.resolve() in path.resolve().parents
    assert not list(tmp_path.glob("*.sqlite")), "a file escaped the cache directory"


def test_capability_refresh_retires_the_cache(tmp_path: Path) -> None:
    """A new release picture must not serve extracts taken under the old one."""
    runtime, _ = _runtime(tmp_path)
    first = runtime.transformations("qa")._cache
    runtime.refresh_capabilities("qa")
    second = runtime.transformations("qa")._cache
    assert first is not second
    # The retired instance must stay usable: a repository built before the refresh still holds it.
    assert first is not None
    first.put("routine_code", "T1", "[]")  # would raise if the connection had been closed


def test_refresh_cache_uses_the_live_store(tmp_path: Path) -> None:
    """bw_refresh_cache must clear the store the repositories write to, not a fresh one."""
    runtime, _ = _runtime(tmp_path)
    cache = runtime.transformations("qa")._cache
    assert cache is not None
    cache.put("routine_code", "T1", "[]")
    result = runtime.refresh_cache("qa", "routine_code")
    assert result.removed == 1
    assert cache.get("routine_code", "T1") is None


def test_unwritable_cache_dir_degrades_instead_of_failing(tmp_path: Path) -> None:
    """A cache that cannot be opened is never fatal: repositories just run uncached."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    resolver = _Resolver()
    runtime = ServerRuntime(
        _Profiles(),  # type: ignore[arg-type]
        _Pool(),  # type: ignore[arg-type]
        resolver,  # type: ignore[arg-type]
        cache_dir=blocker / "cache",  # parent is a file -> mkdir fails
    )
    assert runtime._cache("qa") is None
    assert runtime.transformations("qa")._cache is None  # still usable
    assert runtime.refresh_cache("qa", "all").removed == 0


# --- the snapshot store is the other thing at rest ----------------------------------------


def test_snapshot_store_lives_beside_the_cache(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    store = runtime.snapshot_store("qa")
    assert store is not None
    assert store.path.parent == tmp_path / "cache"
    assert store.path.name.startswith("qa-")
    assert store.path.name.endswith(".snapshots.sqlite")


def test_two_tenants_with_the_same_alias_do_not_share_stored_data(tmp_path: Path) -> None:
    """The isolation property that was absent: everybody's production system is called `prd`.

    Without a tenant these two resolved to one cache file and one snapshot store, so a partner
    install served one customer's extracted metadata under another's name - and nothing failed.
    """
    acme, _ = _runtime(tmp_path, tenant="acme")
    globex, _ = _runtime(tmp_path, tenant="globex")

    acme.transformations("prd")
    globex.transformations("prd")
    acme_store, globex_store = acme.snapshot_store("prd"), globex.snapshot_store("prd")
    assert acme_store is not None and globex_store is not None
    assert acme_store.path != globex_store.path

    caches = sorted(
        p.name for p in (tmp_path / "cache").glob("*.sqlite") if "snapshots" not in p.name
    )
    assert len(caches) == 2, f"the two tenants shared a cache file: {caches}"
    assert any(name.startswith("acme-prd-") for name in caches)
    assert any(name.startswith("globex-prd-") for name in caches)


def test_cache_status_reports_the_identity_so_isolation_is_verifiable(tmp_path: Path) -> None:
    """Asserting isolation is not the same as being able to check it."""
    runtime, _ = _runtime(tmp_path, tenant="acme")
    status = runtime.cache_status("prd")
    assert status.tenant == "acme"
    assert status.isolated_by_tenant is True
    assert status.storage_key.startswith("acme-prd-")


def test_the_identity_is_reported_even_when_nothing_is_stored(tmp_path: Path) -> None:
    """A profile keeping nothing at rest still has to say whose profile it is."""
    runtime, _ = _runtime(tmp_path, cache_enabled=False, tenant="acme")
    status = runtime.cache_status("prd")
    assert status.enabled is False
    assert status.tenant == "acme"
    assert status.storage_key.startswith("acme-prd-")


def test_one_snapshot_store_per_profile(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    assert runtime.snapshot_store("qa") is runtime.snapshot_store("qa")


def test_a_capability_refresh_does_not_retire_a_snapshot(tmp_path: Path) -> None:
    """The deliberate difference from the extract cache.

    A cached extract must not outlive the release picture it was read under, so a refresh retires
    it. A snapshot must: it is a record of how the system looked at a moment, and losing the
    baseline on a refresh would remove the only thing that makes "what changed" answerable.
    """
    runtime, _ = _runtime(tmp_path)
    before = runtime.snapshot_store("qa")
    runtime.refresh_capabilities("qa")
    assert runtime.snapshot_store("qa") is before


def test_no_snapshot_store_when_the_profile_keeps_nothing_at_rest(tmp_path: Path) -> None:
    """Same switch as the extract cache: a snapshot names every object in the system."""
    runtime, _ = _runtime(tmp_path, cache_enabled=False)
    assert runtime.snapshot_store("qa") is None
    assert not list((tmp_path / "cache").glob("*.snapshots.sqlite"))


def test_cache_status_does_not_create_the_snapshot_file(tmp_path: Path) -> None:
    """A status report must not become the cause of what it reports."""
    runtime, _ = _runtime(tmp_path)
    status = runtime.cache_status("qa")
    assert status.snapshots == 0
    assert status.snapshot_location is None
    assert not (tmp_path / "cache" / "qa.snapshots.sqlite").exists()


def test_cache_status_reports_stored_snapshots(tmp_path: Path) -> None:
    """The compliance answer has to cover both stores, not just the smaller one."""
    runtime, _ = _runtime(tmp_path)
    store = runtime.snapshot_store("qa")
    assert store is not None
    store.put(
        Snapshot(
            snapshot_id="qa-20260101T000000Z",
            system="qa",
            taken_at=datetime(2026, 1, 1, tzinfo=UTC),
            bw_release="7.50",
            abap_schema=SCHEMA,
            server_version="0",
        )
    )
    status = runtime.cache_status("qa")
    assert status.snapshots == 1
    assert status.snapshot_size_bytes > 0
    assert status.snapshot_location == str(tmp_path / "cache")


def test_an_unwritable_directory_yields_no_snapshot_store(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    runtime = ServerRuntime(
        _Profiles(),  # type: ignore[arg-type]
        _Pool(),  # type: ignore[arg-type]
        _Resolver(),  # type: ignore[arg-type]
        cache_dir=blocker / "cache",
    )
    assert runtime.snapshot_store("qa") is None
