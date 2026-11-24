"""Outside runtime consumer of `multi` (case h2)."""
from multi import a1, a2, a3, a4, a5, b1, b2, b3, b4, b5


def run_multi(x: int) -> list[object]:
    return [a1(x), a2(x), a3(x), a4(x), a5(x), b1(x), b2(x), b3(x), b4(x), b5(x)]
