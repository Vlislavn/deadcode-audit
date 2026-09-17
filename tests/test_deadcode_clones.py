"""Tests for the clones module's token normalization and similarity."""

import ast

from deadcode_audit.clones import clone_similarity, normalize_function


def _tokens(src: str) -> list[str]:
    return normalize_function(ast.parse(src).body[0])  # type: ignore[arg-type]


def test_rename_of_except_as_and_nested_def_names_still_matches() -> None:
    # ``except E as err`` and nested ``def``/``class`` bindings are plain str attributes on their
    # AST nodes (no Store-context Name node), so without collecting them a clone that differs
    # only in one of those names scored below 1.0 (a missed Type-2 clone).
    a = _tokens("def f(x):\n    try:\n        g(x)\n    except ValueError as err1:\n        h(err1)\n")
    b = _tokens("def f(x):\n    try:\n        g(x)\n    except ValueError as err2:\n        h(err2)\n")
    assert clone_similarity(a, b) == 1.0

    c = _tokens("def outer(x):\n    def helper1(y):\n        return y + 1\n    return helper1(x)\n")
    d = _tokens("def outer(x):\n    def helper2(y):\n        return y + 1\n    return helper2(x)\n")
    assert clone_similarity(c, d) == 1.0


def test_free_api_call_names_are_still_preserved() -> None:
    # Abstraction must stay scoped to locally bound names: two functions calling different APIs
    # are not clones even when structurally identical.
    a = _tokens("def f(x):\n    return alpha(x)\n")
    b = _tokens("def f(x):\n    return beta(x)\n")
    assert clone_similarity(a, b) < 1.0


def test_self_recursive_name_stays_a_free_name() -> None:
    # The walked root's own name is a str attribute too; it must not enter the local set, or a
    # self-recursive call would stop being part of the semantic signature.
    a = _tokens("def recurse(x):\n    return recurse(x - 1)\n")
    assert any(t == "#recurse" for t in a)
