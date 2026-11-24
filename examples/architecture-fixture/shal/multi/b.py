"""Case (h2) module b: 5 shown + 2 private functions; alone it is not shallow."""


def b1(x: int) -> int:
    return x * 11 + 11


def b2(x: int) -> int:
    return x * 12 + 12


def b3(x: int) -> int:
    return x * 13 + 13


def b4(x: int) -> int:
    return x * 14 + 14


def b5(x: int) -> int:
    return x * 15 + 15


def _pb1(x: int) -> int:
    return x * 211 + 211


def _pb2(x: int) -> int:
    return x * 212 + 212
