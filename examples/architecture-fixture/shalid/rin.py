"""Case (x3): s1..s8 are called by the __init__ of another package: FIRE."""

def s1(x: int) -> int:
    return x * 1 + 1


def s2(x: int) -> int:
    return x * 2 + 2


def s3(x: int) -> int:
    return x * 3 + 3


def s4(x: int) -> int:
    return x * 4 + 4


def s5(x: int) -> int:
    return x * 5 + 5


def s6(x: int) -> int:
    return x * 6 + 6


def s7(x: int) -> int:
    return x * 7 + 7


def s8(x: int) -> int:
    return x * 8 + 8
