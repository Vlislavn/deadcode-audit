"""Case (k): 8 shown + 9 underscore helpers used only inside; hidden 9 > shown 8."""

def p1(x: int) -> int:
    return _helper1(x) + 1


def p2(x: int) -> int:
    return _helper2(x) + 2


def p3(x: int) -> int:
    return _helper3(x) + 3


def p4(x: int) -> int:
    return _helper4(x) + 4


def p5(x: int) -> int:
    return _helper5(x) + 5


def p6(x: int) -> int:
    return _helper6(x) + 6


def p7(x: int) -> int:
    return _helper7(x) + 7


def p8(x: int) -> int:
    return _helper8(x) + 8


def _helper1(x: int) -> int:
    return x * 51 + 51


def _helper2(x: int) -> int:
    return x * 52 + 52


def _helper3(x: int) -> int:
    return x * 53 + 53


def _helper4(x: int) -> int:
    return x * 54 + 54


def _helper5(x: int) -> int:
    return x * 55 + 55


def _helper6(x: int) -> int:
    return x * 56 + 56


def _helper7(x: int) -> int:
    return x * 57 + 57


def _helper8(x: int) -> int:
    return x * 58 + 58


def _helper9(x: int) -> int:
    return x * 59 + 59
