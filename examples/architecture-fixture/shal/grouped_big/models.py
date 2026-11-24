"""Case (l2): one in-unit class, not imported outside."""


class Account:
    def __init__(self) -> None:
        self.balance = 0

    def deposit(self, amount: int) -> int:
        self.balance += amount
        return self.balance
