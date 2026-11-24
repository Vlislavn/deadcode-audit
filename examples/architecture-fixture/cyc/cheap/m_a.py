"""Case (a): cheap tie m_a -> m_b (1 name) vs expensive tie m_b -> m_a (3 names)."""
from cheap.m_b import b_one


def a_one() -> int:
    return b_one() + 1


def a_two() -> int:
    return 2


def a_three() -> int:
    return 3
