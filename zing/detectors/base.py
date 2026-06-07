"""Detector base class, registry, and suite selection.

Every detector subclasses :class:`Detector`, declares its identity and the suite
tier it first appears in, implements ``run``, and registers itself with
``@register``. The runner discovers detectors through the registry, so adding a
new check is a single self-contained file.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import ClassVar

from zing.context import AuditContext
from zing.models import DetectorResult, Dimension, Status

# Suite tiers, cheapest to most thorough. A detector with ``min_suite="standard"``
# runs in standard, deep, and full but not smoke.
SUITE_ORDER: tuple[str, ...] = ("smoke", "standard", "deep", "full")

REGISTRY: dict[str, type[Detector]] = {}


def register(cls: type[Detector]) -> type[Detector]:
    if cls.id in REGISTRY:
        raise ValueError(f"Duplicate detector id: {cls.id}")
    REGISTRY[cls.id] = cls
    return cls


class Detector(ABC):
    """Base class for all checks."""

    id: ClassVar[str]
    name: ClassVar[str]
    dimension: ClassVar[Dimension]
    min_suite: ClassVar[str] = "standard"
    requires_judge: ClassVar[bool] = False
    requires_baseline: ClassVar[bool] = False
    # Approximate number of API calls this detector issues — used by `--dry-run` so
    # an agent can budget token/cost before committing. A rough upper bound is fine.
    cost_hint: ClassVar[int] = 2

    @abstractmethod
    async def run(self, ctx: AuditContext) -> DetectorResult:
        """Execute the check and return a populated result."""
        raise NotImplementedError

    # -- helpers for subclasses -------------------------------------------- #
    def new_result(self, **kwargs) -> DetectorResult:
        return DetectorResult(
            id=self.id, name=self.name, dimension=self.dimension, **kwargs
        )


def _tier(suite: str) -> int:
    try:
        return SUITE_ORDER.index(suite)
    except ValueError:
        return SUITE_ORDER.index("standard")


def select_detectors(
    suite: str,
    *,
    has_judge: bool,
    has_baseline: bool,
    enabled,
) -> list[Detector]:
    """Instantiate the detectors that should run for this configuration.

    ``enabled`` is a callable ``(detector_id) -> bool`` from :class:`AuditOptions`.
    Detectors requiring a judge/baseline are silently dropped when unavailable.
    """
    suite_tier = _tier(suite)
    chosen: list[Detector] = []
    for det_id, cls in sorted(REGISTRY.items()):
        if _tier(cls.min_suite) > suite_tier:
            continue
        if not enabled(det_id):
            continue
        if cls.requires_judge and not has_judge:
            continue
        if cls.requires_baseline and not has_baseline:
            continue
        chosen.append(cls())
    return chosen


async def run_detector(detector: Detector, ctx: AuditContext) -> DetectorResult:
    """Run a detector, capturing timing and turning crashes into an error result."""
    started = time.perf_counter()
    try:
        result = await detector.run(ctx)
    except Exception as exc:  # a misbehaving relay must not crash the audit
        return DetectorResult(
            id=detector.id,
            name=detector.name,
            dimension=detector.dimension,
            status=Status.ERROR,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if result.duration_ms is None:
        result.duration_ms = (time.perf_counter() - started) * 1000
    return result
