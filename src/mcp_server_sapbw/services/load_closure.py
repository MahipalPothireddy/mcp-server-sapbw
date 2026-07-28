"""Chain <-> provider load closure.

Answers the two questions BW makes surprisingly hard:

* **What does this chain load?** A chain's own step list is not the answer — most of its loads sit
  inside nested sub-chains, so the step graph has to be walked recursively (cycle-guarded).
* **What loads this provider, and how often?** The reverse index, joined to observed cadence, which
  is what makes "is this object's data current?" answerable at all.

The path is ``RSPCCHAIN`` (chain steps) -> ``VARIANTE`` of a ``DTP_LOAD`` step is a DTP id ->
``RSBKDTP`` gives that DTP's target provider and update mode. Nested chains appear as steps of type
``CHAIN`` whose ``VARIANTE`` is the child chain id.

An earlier build recorded this mapping as *not derivable* after probing ``RSPCVARIANT`` — the
variant **parameter** table, which indeed holds no DTP linkage. ``RSPCCHAIN`` is the step table and
does: verified live, 99% of ``DTP_LOAD`` steps join to ``RSBKDTP``.

Step categories are derived from process **type codes**, never from chain names, so a housekeeping
chain is identified by what it does rather than by a naming convention that may not hold.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..models.chains import ChainCadence, LoadClosure, LoadedProvider
from ..models.provenance import UnsupportedResult
from ..repositories.base import Repository
from ..repositories.chains import ChainsRepository

# RSPCCHAIN.TYPE codes grouped by structural role. Codes seen live on 7.50; anything unrecognised
# is counted as "other" rather than being force-fitted into a category.
_STEP_CATEGORIES: dict[str, str] = {
    # data movement into a provider
    "DTP_LOAD": "data_load",
    "LOADING": "data_load",
    # making loaded data available / rebuilding derived structures
    "ADSOACT": "activation",
    "ODSACTIVAT": "activation",
    "ATTRIBCHAN": "activation",
    "HIERARCHY": "activation",
    "INDEX": "activation",
    "DBSTAT": "activation",
    "COMPRESS": "activation",
    # deleting or trimming data
    "PSADELETE": "housekeeping",
    "CHGLOGDEL": "housekeeping",
    "DROPCUBE": "housekeeping",
    "DELETE_ADSO": "housekeeping",
    "ARCHIVE": "housekeeping",
    # orchestration only
    "CHAIN": "orchestration",
    "AND": "orchestration",
    "OR": "orchestration",
    "XOR": "orchestration",
    "TRIGGER": "orchestration",
    "DECISION": "orchestration",
    "INTERRUPT": "orchestration",
    # custom logic
    "ABAP": "custom_code",
    "COMMAND": "custom_code",
}
_DTP_STEP_TYPES = ("DTP_LOAD", "LOADING")
_SUBCHAIN_STEP_TYPE = "CHAIN"

_MAX_SUBCHAIN_DEPTH = 8
_MAX_STEPS = 5000
_MAX_LOADING_CHAINS = 100


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class LoadClosureService(Repository):
    """Resolves what a chain loads, and which chains load a provider."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._chains = ChainsRepository(connection, capability, cache)

    # --- chain -> providers ----------------------------------------------------------------

    def chain_to_providers(self, chain_id: str) -> LoadClosure | UnsupportedResult:
        unsupported = self.require("chain_edges", "dtp")
        if unsupported is not None:
            return unsupported

        providers: list[LoadedProvider] = []
        categories: dict[str, int] = defaultdict(int)
        walked: list[str] = []
        seen_chains: set[str] = {chain_id}
        truncated = False
        queue: deque[tuple[str, int, str | None]] = deque([(chain_id, 0, None)])
        dtp_steps: list[tuple[str, str | None]] = []  # (dtp id, via_subchain)

        while queue:
            current, depth, via = queue.popleft()
            if depth > _MAX_SUBCHAIN_DEPTH:
                truncated = True
                continue
            for step_type, variant in self._steps_of(current):
                categories[_STEP_CATEGORIES.get(step_type, "other")] += 1
                if step_type == _SUBCHAIN_STEP_TYPE and variant:
                    if variant in seen_chains:
                        continue  # cycle guard: a chain reachable twice is walked once
                    seen_chains.add(variant)
                    walked.append(variant)
                    queue.append((variant, depth + 1, variant))
                elif step_type in _DTP_STEP_TYPES and variant:
                    dtp_steps.append((variant, via))

        targets = self._dtp_targets([dtp for dtp, _ in dtp_steps])
        seen_targets: set[str] = set()
        for dtp, via in dtp_steps:
            target = targets.get(dtp)
            if target is None:
                continue
            name, type_code, update_mode = target
            key = f"{name}|{via or ''}"
            if key in seen_targets:
                continue
            seen_targets.add(key)
            providers.append(
                LoadedProvider(
                    name=name,
                    type_code=type_code,
                    dtp_id=dtp,
                    via_subchain=via,
                    update_mode=update_mode,
                    provenance=self.provenance("dtp", {"DTP": dtp, "TGT": name}),
                )
            )
        providers.sort(key=lambda item: item.name)

        caveats: list[str] = []
        unresolved = len(dtp_steps) - len([d for d, _ in dtp_steps if d in targets])
        if unresolved:
            caveats.append(
                f"{unresolved} load step(s) referenced a DTP that could not be resolved in RSBKDTP "
                "(deleted or inactive DTP); those targets are not listed"
            )
        if truncated:
            caveats.append(f"sub-chain recursion stopped at depth {_MAX_SUBCHAIN_DEPTH}")
        return LoadClosure(
            direction="chain_to_providers",
            chain_id=chain_id,
            providers_loaded=providers,
            subchains_walked=walked,
            step_categories=dict(categories),
            truncated_recursion=truncated,
            caveats=caveats,
        )

    def _steps_of(self, chain_id: str) -> list[tuple[str, str | None]]:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["TYPE", "VARIANTE"],
                    from_logical="chain_edges",
                    where=["CHAIN_ID = ?"],
                    params=[chain_id],
                ),
                limit=_MAX_STEPS,
            )
        )
        out: list[tuple[str, str | None]] = []
        for step_type, variant in rows:
            code = _clean(step_type)
            if code:
                out.append((code.upper(), _clean(variant)))
        return out

    def _dtp_targets(self, dtp_ids: list[str]) -> dict[str, tuple[str, str | None, str | None]]:
        """Resolve DTP ids to ``(target, target_type_code, update_mode)``, batched."""
        unique = sorted({d for d in dtp_ids if d})
        if not unique:
            return {}
        out: dict[str, tuple[str, str | None, str | None]] = {}
        for start in range(0, len(unique), _DTP_BATCH):
            chunk = unique[start : start + _DTP_BATCH]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.select(
                self.dialect.build_select(
                    columns=["DTP", "TGT", "TGTTLOGO", "UPDMODE"],
                    from_logical="dtp",
                    where=[f"DTP IN ({placeholders})", "OBJVERS = 'A'"],
                    params=list(chunk),
                )
            )
            for dtp, target, type_code, update_mode in rows:
                key = _clean(dtp)
                name = _clean(target)
                if key and name:
                    out[key] = (
                        name,
                        _clean(type_code),
                        _UPDMODE_LABEL.get((_clean(update_mode) or "").upper()),
                    )
        return out

    # --- provider -> chains ----------------------------------------------------------------

    def provider_to_chains(self, provider: str) -> LoadClosure | UnsupportedResult:
        unsupported = self.require("chain_edges", "dtp")
        if unsupported is not None:
            return unsupported

        dtps = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DTP"],
                    from_logical="dtp",
                    where=["TGT = ?", "OBJVERS = 'A'"],
                    params=[provider],
                ),
                limit=_MAX_LOADING_CHAINS,
            )
        )
        dtp_ids = [str(r[0]).strip() for r in dtps if _clean(r[0])]
        if not dtp_ids:
            return LoadClosure(
                direction="provider_to_chains",
                provider=provider,
                caveats=[
                    "no DTP targets this provider, so no loading chain could be resolved; it may "
                    "be loaded by an InfoPackage, filled by a routine, or be virtual"
                ],
            )

        placeholders = ", ".join("?" for _ in dtp_ids)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT CHAIN_ID"],
                    from_logical="chain_edges",
                    where=[f"VARIANTE IN ({placeholders})"],
                    params=list(dtp_ids),
                ),
                limit=_MAX_LOADING_CHAINS,
            )
        )
        direct = sorted({str(r[0]).strip() for r in rows if _clean(r[0])})
        # A chain that only *contains* the loading chain also governs when the load happens.
        parents = self._parents_of(direct)
        all_chains = sorted(set(direct) | parents)
        cadence = self._chains.get_cadence(all_chains) if all_chains else {}
        ordered = [cadence[c] for c in all_chains if c in cadence]

        caveats: list[str] = []
        missing = [c for c in all_chains if c not in cadence]
        if missing:
            caveats.append(
                f"{len(missing)} chain(s) load this provider but have no run history, so their "
                "cadence is unknown (never executed, or history rotated out)"
            )
        if parents:
            caveats.append(
                "includes parent chains that trigger the loading chain as a sub-chain; the "
                "parent's cadence is what actually governs the load"
            )
        return LoadClosure(
            direction="provider_to_chains",
            provider=provider,
            loading_chains=ordered,
            caveats=caveats,
        )

    def _parents_of(self, chain_ids: list[str]) -> set[str]:
        """Chains that call any of ``chain_ids`` as a sub-chain (one level up)."""
        if not chain_ids:
            return set()
        placeholders = ", ".join("?" for _ in chain_ids)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT CHAIN_ID"],
                    from_logical="chain_edges",
                    where=["TYPE = ?", f"VARIANTE IN ({placeholders})"],
                    params=[_SUBCHAIN_STEP_TYPE, *chain_ids],
                ),
                limit=_MAX_LOADING_CHAINS,
            )
        )
        return {str(r[0]).strip() for r in rows if _clean(r[0])}


_DTP_BATCH = 300
# RSBKDTP.UPDMODE -> readable update mode.
_UPDMODE_LABEL: dict[str, str] = {"F": "full", "D": "delta", "I": "init"}


def cadence_of(chains: list[ChainCadence]) -> ChainCadence | None:
    """The governing cadence among several loading chains: the most frequent one.

    When two chains load the same provider, currency is set by whichever runs most often.
    """
    if not chains:
        return None
    return max(chains, key=lambda c: (c.runs_per_day or 0.0, c.run_count))
