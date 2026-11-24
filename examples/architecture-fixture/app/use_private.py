"""Outside runtime consumer of `private` (case private)."""
from private import p1, p2, p3, p4, p5, p6, p7, p8


def run_private(x: int) -> list[object]:
    return [p1(x), p2(x), p3(x), p4(x), p5(x), p6(x), p7(x), p8(x)]
