"""Case (d): x.m1 -> y.m2 (no edge back from m2)."""
from pkgp.y.m2 import two


def one() -> int:
    return two() + 1
