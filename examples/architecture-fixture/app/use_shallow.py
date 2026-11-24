"""Outside runtime consumer of `shallow` (case shallow)."""
from shallow import f01, f02, f03, f04, f05, f06, f07, f08, f09


def run_shallow(x: int) -> list[object]:
    return [f01(x), f02(x), f03(x), f04(x), f05(x), f06(x), f07(x), f08(x), f09(x)]
