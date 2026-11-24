"""Outside runtime consumer of `deep` (case deep)."""
from deep import d1, d2, d3


def run_deep(x: int) -> list[object]:
    return [d1(x), d2(x), d3(x)]
