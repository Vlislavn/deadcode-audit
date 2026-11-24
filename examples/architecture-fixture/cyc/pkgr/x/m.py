"""Case (f): file cycle pkgr.x.m <-> pkgr.y.n; also yields package cycle pkgr.x <-> pkgr.y."""
from pkgr.y.n import n_value


def m_value() -> int:
    return n_value() + 1
