"""Outside runtime consumer of `grouped_big` (case grouped_big)."""
from grouped_big import acct_op1, acct_op2, acct_op3, acct_op4, acct_op5, acct_op6, acct_op7, acct_op8, acct_op9


def run_grouped_big(x: int) -> list[object]:
    return [acct_op1(x), acct_op2(x), acct_op3(x), acct_op4(x), acct_op5(x), acct_op6(x), acct_op7(x), acct_op8(x), acct_op9(x)]
