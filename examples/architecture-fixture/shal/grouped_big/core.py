"""Case (l2): 9 shown functions, all referencing the in-unit class Account -> 1 list of 9."""


class Account:
    def __init__(self) -> None:
        self.balance = 0

    def deposit(self, amount: int) -> int:
        self.balance += amount
        return self.balance


def acct_op1(x: int) -> Account:
    account = Account()
    account.deposit(x * 1)
    return account


def acct_op2(x: int) -> Account:
    account = Account()
    account.deposit(x * 2)
    return account


def acct_op3(x: int) -> Account:
    account = Account()
    account.deposit(x * 3)
    return account


def acct_op4(x: int) -> Account:
    account = Account()
    account.deposit(x * 4)
    return account


def acct_op5(x: int) -> Account:
    account = Account()
    account.deposit(x * 5)
    return account


def acct_op6(x: int) -> Account:
    account = Account()
    account.deposit(x * 6)
    return account


def acct_op7(x: int) -> Account:
    account = Account()
    account.deposit(x * 7)
    return account


def acct_op8(x: int) -> Account:
    account = Account()
    account.deposit(x * 8)
    return account


def acct_op9(x: int) -> Account:
    account = Account()
    account.deposit(x * 9)
    return account


def _aux1(x: int) -> int:
    return x * 91 + 91


def _aux2(x: int) -> int:
    return x * 92 + 92


def _aux3(x: int) -> int:
    return x * 93 + 93
