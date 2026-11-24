"""Consumer of kpi."""
from kpi.m1 import p1, p2, p3, p4, p5
from kpi.m2 import q1, q2, q3, q4, q5


def run_kpi(x: int) -> list[object]:
    return [p1(x), p2(x), p3(x), p4(x), p5(x), q1(x), q2(x), q3(x), q4(x), q5(x)]
