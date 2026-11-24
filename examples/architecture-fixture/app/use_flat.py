"""Outside runtime consumer of `flat` (case flat)."""
from flat import q01, q02, q03, q04, q05, q06, q07, q08, q09


def run_flat(x: int) -> list[object]:
    return [q01(x), q02(x), q03(x), q04(x), q05(x), q06(x), q07(x), q08(x), q09(x)]
