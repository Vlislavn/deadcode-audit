"""Case (g2): the a -> b edge exists only under TYPE_CHECKING, so there is no cycle."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tc.b import Thing


def describe(thing: Thing) -> str:
    return thing.label
