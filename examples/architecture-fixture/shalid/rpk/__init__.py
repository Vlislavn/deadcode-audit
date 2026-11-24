"""A package __init__ that really calls rin (not a facade)."""
from rin import s1, s2, s3, s4, s5, s6, s7, s8


def run_rpk(x: int) -> list[object]:
    return [s1(x), s2(x), s3(x), s4(x), s5(x), s6(x), s7(x), s8(x)]
