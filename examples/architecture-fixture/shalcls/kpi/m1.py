class K:
    pass


def p1(x: int) -> K:
    return K() if x > 1 else K()


def p2(x: int) -> K:
    return K() if x > 2 else K()


def p3(x: int) -> K:
    return K() if x > 3 else K()


def p4(x: int) -> K:
    return K() if x > 4 else K()


def p5(x: int) -> K:
    return K() if x > 5 else K()
