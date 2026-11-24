"""Outside runtime consumer of `excluded` (case excluded)."""
from excluded import e1, e2, e3, e4, e5, e6, e7, e8


def run_excluded(x: int) -> list[object]:
    return [e1(x), e2(x), e3(x), e4(x), e5(x), e6(x), e7(x), e8(x)]
