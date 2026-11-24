"""Case (h2): package of two modules; FIRE at the package, neither module fires."""
from multi.a import a1, a2, a3, a4, a5
from multi.b import b1, b2, b3, b4, b5

__all__ = ["a1", "a2", "a3", "a4", "a5", "b1", "b2", "b3", "b4", "b5"]
