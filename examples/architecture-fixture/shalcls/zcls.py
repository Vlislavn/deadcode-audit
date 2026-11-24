"""Case (y1): g1..g7 use Z, g8 uses A and Z: Z's list has 8: FIRE."""


class A:
    pass


class Z:
    pass


def g1(x: int) -> Z:
    return Z() if x > 1 else Z()


def g2(x: int) -> Z:
    return Z() if x > 2 else Z()


def g3(x: int) -> Z:
    return Z() if x > 3 else Z()


def g4(x: int) -> Z:
    return Z() if x > 4 else Z()


def g5(x: int) -> Z:
    return Z() if x > 5 else Z()


def g6(x: int) -> Z:
    return Z() if x > 6 else Z()


def g7(x: int) -> Z:
    return Z() if x > 7 else Z()


def g8(x: int) -> tuple[A, Z]:
    return A(), Z()
