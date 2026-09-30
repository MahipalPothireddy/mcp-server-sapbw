"""Repository base: the only layer that turns capability + dialect + connection into records.

Every repository method checks the capability record before building SQL (returning a structured
``UnsupportedResult`` when a table is absent), executes through the read-only connection, and stamps
each record with provenance. Repositories hold no tool registration and no cross-domain logic.

Expensive per-object extracts go through :meth:`Repository.cached_model` /
:meth:`Repository.cached_model_list`, which read and write the per-profile SQLite cache. Structural
metadata changes slowly, so it carries a long TTL; runtime statistics use the ``"runtime"`` tier,
hard-capped at one hour (mission Section 3). The ``object_type`` passed in doubles as the
``bw_refresh_cache`` scope for that family of objects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from ..core.cache import CacheTier, SqliteCache
from ..core.capabilities import SupportsSelect, unsupported_result
from ..core.dialect import SelectQuery, SqlDialect
from ..models.capability import CapabilityRecord
from ..models.provenance import Provenance, UnsupportedResult

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ResultT = TypeVar("_ResultT")  # a builder's full return union (model | not-found | unsupported)


class Repository:
    """Base for domain repositories."""

    def __init__(
        self,
        connection: SupportsSelect,
        capability: CapabilityRecord,
        cache: SqliteCache | None = None,
    ) -> None:
        self._connection = connection
        self._capability = capability
        self._dialect = SqlDialect(capability)
        self._cache = cache

    @property
    def capability(self) -> CapabilityRecord:
        return self._capability

    @property
    def dialect(self) -> SqlDialect:
        return self._dialect

    def require(
        self, *logical_names: str, alternative: str | None = None
    ) -> UnsupportedResult | None:
        """Return an ``UnsupportedResult`` if any logical table is absent, else ``None``."""
        absent = [name for name in logical_names if not self._capability.is_available(name)]
        if not absent:
            return None
        return unsupported_result(
            self._capability,
            [self.physical(name) for name in absent],
            alternative=alternative,
        )

    def require_columns(
        self, logical_name: str, *columns: str, alternative: str | None = None
    ) -> UnsupportedResult | None:
        """Return an ``UnsupportedResult`` if this release lacks any of ``columns``, else ``None``.

        The pre-flight companion to :meth:`require` (D45). ``require`` answers "does the table
        exist"; this answers "does it carry the columns this answer needs", which is a separate
        question and was never asked. A reader that skips it still fails safely - the dialect will
        not build the statement - but it fails as an exception, and a caller asking a legitimate
        question about a release that structures a table differently deserves a structured result.

        Silent when the column list was never measured, so a release whose ``DD03L`` could not be
        read behaves exactly as it did before this existed.
        """
        status = self._capability.table(logical_name)
        if status is None or not status.columns_known:
            return None
        absent = [column for column in columns if not status.has_column(column)]
        if not absent:
            return None
        table = self.physical(logical_name)
        return unsupported_result(
            self._capability,
            [f"{table}.{column}" for column in absent],
            alternative=alternative,
            detail=(
                f"{table} exists on {self._capability.bw_release} but does not carry "
                f"{', '.join(absent)}. The table was found and this column was not, so this is a "
                "release difference rather than a missing grant or an absent object."
            ),
        )

    def physical(self, logical_name: str) -> str:
        """Resolved physical table name for a logical name (falls back to the logical name)."""
        status = self._capability.table(logical_name)
        if status is not None and status.resolved_name is not None:
            return status.resolved_name
        return logical_name

    def select(self, query: SelectQuery) -> list[tuple[Any, ...]]:
        """Execute a read-only SELECT through the connection (statement guard applies)."""
        return self._connection.execute_select(query.sql, query.parameters)

    def provenance(self, logical_name: str, key: Mapping[str, Any]) -> Provenance:
        """Build a Provenance citing the resolved physical table and a stringified key."""
        return Provenance(
            source_table=self.physical(logical_name),
            source_key={k: str(v) for k, v in key.items()},
        )

    # --- cache passthrough (no-ops when no cache is configured) ---

    def cache_get(
        self, object_type: str, object_id: str, *, tier: CacheTier = "structural"
    ) -> str | None:
        return self._cache.get(object_type, object_id, tier=tier) if self._cache else None

    def cache_put(
        self, object_type: str, object_id: str, value: str, *, tier: CacheTier = "structural"
    ) -> None:
        if self._cache is not None:
            self._cache.put(object_type, object_id, value, tier=tier)

    # --- cached extraction ---
    #
    # These are the only two entry points repositories use to cache. Both cache *successes* only:
    # an UnsupportedResult is capability state (already keyed by the cache fingerprint, and cheap to
    # recompute) and a None is a not-found, which may become found once an object is transported.
    # Caching either would freeze a transient answer into a long TTL.
    #
    # A stored value that no longer validates is treated as a miss and rebuilt, so a model change
    # shipped in a new server version can never surface as a validation error to the caller.

    def cached_model(
        self,
        object_type: str,
        object_id: str,
        *,
        model: type[_ModelT],
        build: Callable[[], _ResultT],
        cache_when: Callable[[_ModelT], bool] | None = None,
        tier: CacheTier = "structural",
    ) -> _ResultT:
        """Serve one model from the cache, else build it and store the result.

        Only an instance of ``model`` is stored, so the other members of a builder's return union
        (``UnsupportedResult``, ``ObjectNotFound``, ``None``) pass through uncached by construction.

        ``cache_when`` guards the case where "nothing found" is expressed *as* the model rather than
        as a separate type — an absent object yields an empty shell, and caching that would answer
        "does not exist" for the whole TTL after it was transported. Repositories whose builder can
        return such a shell pass a predicate that recognises a real extract.
        """
        if self._cache is None:
            return build()
        raw = self.cache_get(object_type, object_id, tier=tier)
        if raw is not None:
            try:
                return cast("_ResultT", model.model_validate_json(raw))
            except ValidationError:
                pass  # shape changed since it was stored -> rebuild
        built = build()
        if isinstance(built, model) and (cache_when is None or cache_when(built)):
            self.cache_put(object_type, object_id, built.model_dump_json(), tier=tier)
        return built

    def cached_model_list(
        self,
        object_type: str,
        object_id: str,
        *,
        model: type[_ModelT],
        build: Callable[[], list[_ModelT] | UnsupportedResult],
        tier: CacheTier = "structural",
    ) -> list[_ModelT] | UnsupportedResult:
        """Serve a list of models from the cache, else build it and store the result."""
        if self._cache is None:
            return build()
        raw = self.cache_get(object_type, object_id, tier=tier)
        if raw is not None:
            try:
                return [model.model_validate(item) for item in json.loads(raw)]
            except (ValidationError, ValueError, TypeError):
                pass  # shape changed or value corrupt -> rebuild
        built = build()
        if isinstance(built, list):
            payload = json.dumps([item.model_dump(mode="json") for item in built])
            self.cache_put(object_type, object_id, payload, tier=tier)
        return built
