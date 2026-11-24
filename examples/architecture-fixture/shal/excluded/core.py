"""Case (u): 8 shown, 2 hidden functions, 2 hidden methods (classes are not working items).

16 further members are excluded kinds (dunders, property/setter, methods of an external-base class).
If they were counted, hidden would exceed shown and the rule would not fire.
"""
import threading

def e1(x: int) -> int:
    return x * 1 + 1


def e2(x: int) -> int:
    return x * 2 + 2


def e3(x: int) -> int:
    return x * 3 + 3


def e4(x: int) -> int:
    return x * 4 + 4


def e5(x: int) -> int:
    return x * 5 + 5


def e6(x: int) -> int:
    return x * 6 + 6


def e7(x: int) -> int:
    return x * 7 + 7


def e8(x: int) -> int:
    return x * 8 + 8


def _hid1(x: int) -> int:
    return x * 180 + 180


def _hid2(x: int) -> int:
    return x * 181 + 181


class Plain:
    def __init__(self) -> None:
        self._v = 0

    def __repr__(self) -> str:
        return f"Plain({self._v})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Plain) and other._v == self._v

    def __hash__(self) -> int:
        return hash(self._v)

    @property
    def value(self) -> int:
        return self._v

    @value.setter
    def value(self, v: int) -> None:
        self._v = v

    @property
    def doubled(self) -> int:
        return self._v * 2

    @property
    def tripled(self) -> int:
        return self._v * 3

    def bump(self) -> int:
        self._v += 1
        return self._v

    def drop(self) -> int:
        self._v -= 1
        return self._v


class Worker(threading.Thread):
    def step1(self) -> int:
        return 1 * 2

    def step2(self) -> int:
        return 2 * 2

    def step3(self) -> int:
        return 3 * 2

    def step4(self) -> int:
        return 4 * 2

    def step5(self) -> int:
        return 5 * 2

    def step6(self) -> int:
        return 6 * 2

    def step7(self) -> int:
        return 7 * 2

    def step8(self) -> int:
        return 8 * 2
