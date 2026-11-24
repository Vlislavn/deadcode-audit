"""Case (n): a class with 20 methods, every one called from outside; only 2 functions are shown."""


class Engine:
    def __init__(self) -> None:
        self.calls = 0

    def m01(self) -> int:
        self.calls += 1
        return self.calls

    def m02(self) -> int:
        self.calls += 2
        return self.calls

    def m03(self) -> int:
        self.calls += 3
        return self.calls

    def m04(self) -> int:
        self.calls += 4
        return self.calls

    def m05(self) -> int:
        self.calls += 5
        return self.calls

    def m06(self) -> int:
        self.calls += 6
        return self.calls

    def m07(self) -> int:
        self.calls += 7
        return self.calls

    def m08(self) -> int:
        self.calls += 8
        return self.calls

    def m09(self) -> int:
        self.calls += 9
        return self.calls

    def m10(self) -> int:
        self.calls += 10
        return self.calls

    def m11(self) -> int:
        self.calls += 11
        return self.calls

    def m12(self) -> int:
        self.calls += 12
        return self.calls

    def m13(self) -> int:
        self.calls += 13
        return self.calls

    def m14(self) -> int:
        self.calls += 14
        return self.calls

    def m15(self) -> int:
        self.calls += 15
        return self.calls

    def m16(self) -> int:
        self.calls += 16
        return self.calls

    def m17(self) -> int:
        self.calls += 17
        return self.calls

    def m18(self) -> int:
        self.calls += 18
        return self.calls

    def m19(self) -> int:
        self.calls += 19
        return self.calls

    def m20(self) -> int:
        self.calls += 20
        return self.calls


def make_engine(seed: int) -> Engine:
    engine = Engine()
    engine.calls = seed
    return engine


def describe(engine: Engine) -> str:
    return f"engine:{engine.m01()}"
