"""Case (g1): soft cycle - the b -> a edge is a function-local import."""
from soft.b import b_value


def a_value() -> int:
    return b_value() + 1
