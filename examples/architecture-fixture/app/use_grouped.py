"""Outside runtime consumer of `grouped` (case grouped)."""
from grouped import ledger_op1, ledger_op2, ledger_op3, ledger_op4, journal_op1, journal_op2, journal_op3, plain_op1, plain_op2


def run_grouped(x: int) -> list[object]:
    return [ledger_op1(x), ledger_op2(x), ledger_op3(x), ledger_op4(x), journal_op1(x), journal_op2(x), journal_op3(x), plain_op1(x), plain_op2(x)]
