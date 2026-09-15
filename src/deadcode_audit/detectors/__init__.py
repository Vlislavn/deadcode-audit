"""Per-rule AI-slop / dead-code detector modules + the assembled registry.

Each submodule exposes ``rules: tuple[RuleSpec, ...]`` and ``detect(ctx: FileContext) -> list[Diagnostic]``
per the :mod:`deadcode_audit.framework` detector protocol — a module satisfies the protocol by
duck typing, so :data:`ALL_DETECTORS` is just the tuple of detector modules the scan runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deadcode_audit.detectors import (
    comments,
    complexity,
    control_flow,
    exceptions,
    hardcoded,
    naming,
    python_idioms,
    security,
    thin_wrapper,
)

if TYPE_CHECKING:
    from deadcode_audit.framework import Detector

ALL_DETECTORS: tuple[Detector, ...] = (
    exceptions,
    thin_wrapper,
    naming,
    comments,
    control_flow,
    hardcoded,
    python_idioms,
    complexity,
    security,
)

__all__ = ["ALL_DETECTORS"]
