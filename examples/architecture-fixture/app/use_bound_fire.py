"""Outside runtime consumer of `bound_fire` (case bound_fire)."""
from bound_fire import b1, b2, b3, b4, b5, b6, b7, b8


def run_bound_fire(x: int) -> list[object]:
    return [b1(x), b2(x), b3(x), b4(x), b5(x), b6(x), b7(x), b8(x)]
