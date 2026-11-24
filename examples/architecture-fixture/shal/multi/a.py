"""Case (h2) module a: 5 shown + 2 private functions; alone it is not shallow."""


def a1(x: int) -> int:
    return x * 1 + 1


def a2(x: int) -> int:
    return x * 2 + 2


def a3(x: int) -> int:
    return x * 3 + 3


def a4(x: int) -> int:
    return x * 4 + 4


def a5(x: int) -> int:
    return x * 5 + 5


def _pa1(x: int) -> int:
    return x * 201 + 201


def _pa2(x: int) -> int:
    return x * 202 + 202
