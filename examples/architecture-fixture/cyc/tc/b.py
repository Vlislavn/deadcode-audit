from tc.a import describe


class Thing:
    label = "thing"


def render(thing: Thing) -> str:
    return describe(thing)
