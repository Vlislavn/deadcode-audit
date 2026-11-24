"""Outside runtime consumer of `padded` (case padded)."""
from padded import w1, w2, w3, w4, w5, w6, w7, w8, real


def run_padded(x: int) -> list[object]:
    return [w1(x, x), w2(x, x), w3(x, x), w4(x, x), w5(x, x), w6(x, x), w7(x, x), w8(x, x), real(x)]
