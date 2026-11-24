"""Case (a): see m_a."""
from cheap.m_a import a_one, a_three, a_two


def b_one() -> int:
    return a_one() + a_two() + a_three()
