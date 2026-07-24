"""Tests for the per-profile SQLite metadata cache (B1)."""

from __future__ import annotations

from pathlib import Path

from mcp_server_sapbw.core.cache import SqliteCache


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _cache(
    tmp_path: Path, clock: Clock, *, fingerprint: str = "cap-1", **kw: object
) -> SqliteCache:
    return SqliteCache(
        tmp_path / "cache.sqlite",
        system="qa",
        fingerprint=fingerprint,
        clock=clock,
        **kw,  # type: ignore[arg-type]
    )


def test_put_then_get_hit(tmp_path: Path) -> None:
    cache = _cache(tmp_path, Clock())
    cache.put("transformation", "T1", '{"x": 1}')
    assert cache.get("transformation", "T1") == '{"x": 1}'


def test_get_miss_for_unknown(tmp_path: Path) -> None:
    cache = _cache(tmp_path, Clock())
    assert cache.get("transformation", "nope") is None


def test_structural_ttl_expiry(tmp_path: Path) -> None:
    clock = Clock()
    cache = _cache(tmp_path, clock, structural_ttl=1000)
    cache.put("provider", "P1", "v")
    clock.t = 1001
    assert cache.get("provider", "P1") is None


def test_runtime_tier_hard_capped_at_one_hour(tmp_path: Path) -> None:
    clock = Clock()
    # Even with a large configured runtime_ttl, runtime entries expire after 3600s.
    cache = _cache(tmp_path, clock, runtime_ttl=999_999)
    cache.put("chain_runtime", "C1", "stats", tier="runtime")
    clock.t = 3599
    assert cache.get("chain_runtime", "C1", tier="runtime") == "stats"
    clock.t = 3601
    assert cache.get("chain_runtime", "C1", tier="runtime") is None


def test_fingerprint_change_invalidates(tmp_path: Path) -> None:
    clock = Clock()
    first = _cache(tmp_path, clock, fingerprint="cap-A")
    first.put("provider", "P1", "v")
    first.close()

    second = _cache(tmp_path, clock, fingerprint="cap-B")
    assert second.get("provider", "P1") is None


def test_refresh_all(tmp_path: Path) -> None:
    cache = _cache(tmp_path, Clock())
    cache.put("provider", "P1", "v")
    cache.put("chain", "C1", "v")
    removed = cache.refresh("all")
    assert removed == 2
    assert cache.get("provider", "P1") is None


def test_refresh_by_object_type(tmp_path: Path) -> None:
    cache = _cache(tmp_path, Clock())
    cache.put("provider", "P1", "v")
    cache.put("chain", "C1", "v")
    removed = cache.refresh("provider")
    assert removed == 1
    assert cache.get("provider", "P1") is None
    assert cache.get("chain", "C1") == "v"


def test_refresh_by_object_id(tmp_path: Path) -> None:
    cache = _cache(tmp_path, Clock())
    cache.put("provider", "P1", "v")
    cache.put("chain", "P1", "v")  # same id, different type
    removed = cache.refresh("P1")
    assert removed == 2
