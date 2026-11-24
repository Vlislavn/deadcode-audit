"""Case (b): a <-> b is the live cycle; c only depends on a."""
from shrink.b import b_value


def a_value() -> int:
    return b_value() + 1
