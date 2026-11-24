"""Outside consumer of `methods` (case n): calls all 20 methods on an instance from the factory."""
from methods import describe, make_engine


def run_methods(seed: int) -> list[object]:
    engine = make_engine(seed)
    results: list[object] = [describe(engine)]
    results.append(engine.m01())
    results.append(engine.m02())
    results.append(engine.m03())
    results.append(engine.m04())
    results.append(engine.m05())
    results.append(engine.m06())
    results.append(engine.m07())
    results.append(engine.m08())
    results.append(engine.m09())
    results.append(engine.m10())
    results.append(engine.m11())
    results.append(engine.m12())
    results.append(engine.m13())
    results.append(engine.m14())
    results.append(engine.m15())
    results.append(engine.m16())
    results.append(engine.m17())
    results.append(engine.m18())
    results.append(engine.m19())
    results.append(engine.m20())
    return results
