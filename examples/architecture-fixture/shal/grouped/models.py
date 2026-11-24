"""Case (l): in-unit classes (public, but not imported outside)."""


class Ledger:
    def __init__(self) -> None:
        self.total = 0

    def add(self, amount: int) -> int:
        self.total += amount
        return self.total

    def reset(self) -> None:
        self.total = 0


class Journal:
    def __init__(self) -> None:
        self.entries: list[int] = []

    def record(self, amount: int) -> int:
        self.entries.append(amount)
        return len(self.entries)

    def clear(self) -> None:
        self.entries.clear()
