"""Case (e): file cycle inside one package; no extra package cycle."""
from pkgq.b import b_value


def a_value() -> int:
    return b_value() + 1
