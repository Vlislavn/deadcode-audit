"""Uses hom_a's functions through the package hom_p."""
from hom_p import f1, f2, f3, f4, f5, f6, f7, f8


def run_hom(x: int) -> list[object]:
    return [f1(x), f2(x), f3(x), f4(x), f5(x), f6(x), f7(x), f8(x)]
