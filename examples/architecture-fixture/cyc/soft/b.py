def b_value() -> int:
    from soft.a import a_value

    return a_value() + 1
