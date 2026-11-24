"""Case (l): 9 shown functions grouped by the in-unit class they reference: 4 + 3 + 1 + 1."""

from grouped.models import Journal, Ledger

def ledger_op1(x: int) -> Ledger:
    ledger = Ledger()
    ledger.add(x * 1)
    return ledger


def ledger_op2(x: int) -> Ledger:
    ledger = Ledger()
    ledger.add(x * 2)
    return ledger


def ledger_op3(x: int) -> Ledger:
    ledger = Ledger()
    ledger.add(x * 3)
    return ledger


def ledger_op4(x: int) -> Ledger:
    ledger = Ledger()
    ledger.add(x * 4)
    return ledger


def journal_op1(x: int) -> int:
    journal = Journal()
    return journal.record(x * 1)


def journal_op2(x: int) -> int:
    journal = Journal()
    return journal.record(x * 2)


def journal_op3(x: int) -> int:
    journal = Journal()
    return journal.record(x * 3)


def plain_op1(x: int) -> int:
    return x * 71 + 71


def plain_op2(x: int) -> int:
    return x * 72 + 72


def _unused1(x: int) -> int:
    return x * 81 + 81


def _unused2(x: int) -> int:
    return x * 82 + 82
