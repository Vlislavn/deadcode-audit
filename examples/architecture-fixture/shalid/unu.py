"""Case (x2): h1..h8 are imported but never used: NO-FIRE."""

def h1(x: int) -> int:
    return x * 1 + 1


def h2(x: int) -> int:
    return x * 2 + 2


def h3(x: int) -> int:
    return x * 3 + 3


def h4(x: int) -> int:
    return x * 4 + 4


def h5(x: int) -> int:
    return x * 5 + 5


def h6(x: int) -> int:
    return x * 6 + 6


def h7(x: int) -> int:
    return x * 7 + 7


def h8(x: int) -> int:
    return x * 8 + 8
