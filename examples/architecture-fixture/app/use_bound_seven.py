"""Outside runtime consumer of `bound_seven` (case bound_seven)."""
from bound_seven import s1, s2, s3, s4, s5, s6, s7


def run_bound_seven(x: int) -> list[object]:
    return [s1(x), s2(x), s3(x), s4(x), s5(x), s6(x), s7(x)]
