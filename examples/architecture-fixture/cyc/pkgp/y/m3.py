"""Case (d): y.m3 -> x.m4 (no edge back from m4)."""
from pkgp.x.m4 import four


def three() -> int:
    return four() + 3
