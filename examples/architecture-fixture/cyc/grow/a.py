"""Case (c): a -> b -> c -> a. The allowlist only covers a <-> b."""
from grow.b import b_value


def a_value() -> int:
    return b_value() + 1
