"""Consumer of zcls."""
from zcls import g1, g2, g3, g4, g5, g6, g7, g8


def run_zcls(x: int) -> list[object]:
    return [g1(x), g2(x), g3(x), g4(x), g5(x), g6(x), g7(x), g8(x)]
