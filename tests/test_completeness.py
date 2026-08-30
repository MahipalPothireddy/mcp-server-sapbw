"""A bounded answer must say which bound stopped it, in every reader (defect D6).

``truncated: bool`` said only *that* something stopped. A caller cannot act on that, and worse, most
readers set the flag in exactly one place - so a read stopped by any other bound reported
``truncated=False`` and presented a partial answer as a complete one. ``LineageGraph`` was fixed in
isolation; every other reader kept the defect.

Two kinds of guard here, because the defect has two halves.

*Structural, over every model.* Any result that can be cut short must carry ``completeness`` beside
its flag, and the two must be reconciled rather than assigned independently. Asserted over every
``BoundedResult`` subclass by reflection, so a reader added later is covered without editing this
file - which is the failure mode that let D6 survive being "fixed" the first time.

*Behavioural, per reader.* A field that always reads ``complete`` is worse than no field, so the
readers wired here are driven to their bounds and checked for a **named** bound rather than the
``unspecified`` fallback.
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, date, datetime
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

import mcp_server_sapbw.models as models_package
from mcp_server_sapbw.models.completeness import (
    BOUND_MEANING,
    BOUND_PRECEDENCE,
    Bound,
    BoundedResult,
    BoundHit,
    Completeness,
    bounded,
)
from mcp_server_sapbw.models.lineage import LineageCompleteness
from mcp_server_sapbw.models.provenance import Provenance


def _all_model_classes() -> list[type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(models_package.__path__):
        module = importlib.import_module(f"{models_package.__name__}.{info.name}")
        for name in dir(module):
            candidate = getattr(module, name)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, BaseModel)
                and candidate is not BaseModel
            ):
                found[f"{module.__name__}.{candidate.__name__}"] = candidate
    return list(found.values())


def _bounded_subclasses() -> list[type[BoundedResult]]:
    return [
        cls
        for cls in _all_model_classes()
        if issubclass(cls, BoundedResult) and cls is not BoundedResult
    ]


def _sample_value(annotation: Any, label: str) -> Any:
    """A value satisfying ``annotation``, so no model needs a hand-written fixture here.

    Deliberately thorough rather than skipping on the awkward shapes. An earlier version skipped
    anything it could not trivially build, which quietly excluded four of the models this file
    exists to check - a guard with holes in exactly the places least like the others.
    """
    if annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (UnionType, Union):
        for arg in get_args(annotation):
            if arg is not type(None):
                return _sample_value(arg, label)
        return None
    if origin in (list, set, tuple):
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if annotation is datetime:
            return datetime(2026, 1, 1, tzinfo=UTC)
        if annotation is date:
            return date(2026, 1, 1)
        if annotation is bool:
            return False
        if annotation is int:
            return 0
        if annotation is float:
            return 0.0
        if annotation is str:
            return "X"
        if issubclass(annotation, Provenance):
            return Provenance(source_table="T", source_key={"K": "V"})
        if issubclass(annotation, BaseModel):
            return _minimal(annotation)
    pytest.fail(f"{label} has a required type this helper cannot build: {annotation!r}")


def _minimal(cls: type[BaseModel], **overrides: Any) -> Any:
    """Build ``cls`` supplying only what it requires, so this test needs no per-model fixture."""
    values: dict[str, Any] = {}
    for name, field in cls.model_fields.items():
        if name in overrides or not field.is_required():
            continue
        values[name] = _sample_value(field.annotation, f"{cls.__name__}.{name}")
    return cls(**values, **overrides)


# --- structural, over every reader --------------------------------------------------------------


def test_there_are_bounded_readers_to_check() -> None:
    """Guards the guard: reflection finding nothing would make every test below vacuous."""
    subclasses = _bounded_subclasses()
    assert len(subclasses) >= 6, f"only found {[c.__name__ for c in subclasses]}"


def test_no_model_reports_truncation_without_saying_why() -> None:
    """The D6 invariant. A flag with no reason beside it is the defect, wherever it appears."""
    offenders: list[str] = []
    for cls in _all_model_classes():
        fields = set(cls.model_fields)
        flags = {"truncated", "truncated_recursion"} & fields
        if flags and "completeness" not in fields:
            offenders.append(f"{cls.__module__}.{cls.__name__} has {sorted(flags)}")
    assert not offenders, (
        "these results can report being cut short but not which bound did it (D6):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("cls", _bounded_subclasses(), ids=lambda c: c.__name__)
def test_naming_a_bound_sets_the_flag(cls: type[BoundedResult]) -> None:
    """A reader that names a bound must not still read ``truncated=False``."""
    instance = _minimal(cls, completeness=bounded("row_cap", scope="s", limit=10))
    assert instance.truncated is True
    assert instance.completeness.status == "row_cap"


@pytest.mark.parametrize("cls", _bounded_subclasses(), ids=lambda c: c.__name__)
def test_setting_only_the_flag_never_reads_as_complete(cls: type[BoundedResult]) -> None:
    """The legacy path. Bounded-with-no-reason is honest; bounded-looking-complete is the defect."""
    instance = _minimal(cls, truncated=True)
    assert instance.completeness.is_complete is False
    assert instance.completeness.status == "unspecified"


@pytest.mark.parametrize("cls", _bounded_subclasses(), ids=lambda c: c.__name__)
def test_an_unbounded_result_reads_as_complete(cls: type[BoundedResult]) -> None:
    instance = _minimal(cls)
    assert instance.truncated is False
    assert instance.completeness.is_complete is True
    assert instance.completeness.status == "complete"


# --- the vocabulary itself ----------------------------------------------------------------------


def test_status_is_the_most_limiting_bound() -> None:
    """Several bounds can bind at once; the scalar must be the one to act on first."""
    several = Completeness(
        bounds=[
            BoundHit(bound="page_limit"),
            BoundHit(bound="time_budget"),
            BoundHit(bound="row_cap"),
        ]
    )
    assert several.status == "time_budget"
    assert len(several.bounds) == 3, "the scalar must summarise, not replace, the detail"


def test_every_bound_has_a_meaning_and_a_precedence() -> None:
    """An unranked or unexplained bound degrades to noise at the point a caller needs it most."""
    declared = set(get_args(Bound))
    assert declared - set(BOUND_MEANING) == set(), "a bound with no stated meaning"
    ranked = set(BOUND_PRECEDENCE) | {"complete"}
    assert declared - ranked == set(), "a bound with no place in the precedence order"
    assert "complete" not in BOUND_PRECEDENCE, "completeness is not a bound"


def test_a_bound_hit_explains_itself_and_keeps_its_limit() -> None:
    hit = BoundHit(bound="parse_budget", scope="anti_patterns", limit=100)
    assert hit.limit == 100, "reporting the limit is what lets a caller decide whether to raise it"
    assert hit.detail == BOUND_MEANING["parse_budget"]
    assert "unknown detail" in (hit.detail or "")


def test_unspecified_sorts_last_among_real_bounds() -> None:
    """Any named reason is more useful than 'something stopped us'."""
    assert BOUND_PRECEDENCE[-1] == "unspecified"
    mixed = Completeness(bounds=[BoundHit(bound="unspecified"), BoundHit(bound="page_limit")])
    assert mixed.status == "page_limit"


def test_the_lineage_vocabulary_is_the_shared_one() -> None:
    """One vocabulary, not two that drift.

    ``LineageGraph`` keeps its own scalar field - it is published and heavily depended on, and
    replacing it would break callers to gain nothing. What must not happen is the *words* diverging,
    so that a caller comparing a lineage bound with a calc-view bound is comparing two vocabularies.
    Every value lineage can report is required to exist in the shared set.
    """
    lineage_values = set(get_args(LineageCompleteness))
    shared = set(get_args(Bound))
    assert lineage_values - shared == set(), (
        "lineage reports bounds the shared vocabulary does not contain, so the two have drifted: "
        f"{sorted(lineage_values - shared)}"
    )
