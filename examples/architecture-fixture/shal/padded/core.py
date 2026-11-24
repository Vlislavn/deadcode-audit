"""Case (j): 9 imported functions, 8 of them thin wrappers -> effectively 1 shown."""

def w1(a: int, b: int) -> int:
    return _impl1(a, b)


def w2(a: int, b: int) -> int:
    return _impl2(a, b)


def w3(a: int, b: int) -> int:
    return _impl3(a, b)


def w4(a: int, b: int) -> int:
    return _impl4(a, b)


def w5(a: int, b: int) -> int:
    return _impl5(a, b)


def w6(a: int, b: int) -> int:
    return _impl6(a, b)


def w7(a: int, b: int) -> int:
    return _impl7(a, b)


def w8(a: int, b: int) -> int:
    return _impl8(a, b)


def _impl1(a: int, b: int) -> int:
    return a * 1 + b


def _impl2(a: int, b: int) -> int:
    return a * 2 + b


def _impl3(a: int, b: int) -> int:
    return a * 3 + b


def _impl4(a: int, b: int) -> int:
    return a * 4 + b


def _impl5(a: int, b: int) -> int:
    return a * 5 + b


def _impl6(a: int, b: int) -> int:
    return a * 6 + b


def _impl7(a: int, b: int) -> int:
    return a * 7 + b


def _impl8(a: int, b: int) -> int:
    return a * 8 + b


def real(x: int) -> int:
    return x * 7 + 7
