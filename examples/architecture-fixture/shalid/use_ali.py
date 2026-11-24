"""Uses ali through a module alias."""
import ali as x


def run_ali(n: int) -> list[object]:
    return [x.a1(n), x.a2(n), x.a3(n), x.a4(n), x.a5(n), x.a6(n), x.a7(n), x.a8(n)]
