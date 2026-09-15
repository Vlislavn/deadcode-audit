"""Tests for the control-flow AI-slop detectors (constant condition + empty function).

Each rule is pinned with >=3 positive cases (varied identifiers/literals to prove the rule is a
structural principle, not a snippet match) and >=3 adversarial negatives (legitimate near-misses
that must stay silent), plus an explicit generalisation negative that a naive text match would
trip on.
"""

from pathlib import Path

from deadcode_audit.detectors.control_flow import detect
from deadcode_audit.framework import build_file_context

CONSTANT = "ai-slop/constant-condition"
EMPTY = "ai-slop/empty-function"


def _rules_fired(source: str) -> list[str]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.rule for d in detect(ctx)]


def _lines_for(source: str, rule: str) -> list[int]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.line for d in detect(ctx) if d.rule == rule]


# --- ai-slop/constant-condition : POSITIVES -------------------------------------------------


def test_constant_condition_if_true() -> None:
    source = "def handler():\n    if True:\n        do_thing()\n"
    assert CONSTANT in _rules_fired(source)


def test_constant_condition_if_zero_and_elif_none() -> None:
    source = (
        "def route(value):\n"
        "    if 0:\n"
        "        first()\n"
        "    elif None:\n"
        "        second()\n"
        "    else:\n"
        "        third()\n"
    )
    # Both the `if 0` and the `elif None` are constant branches.
    assert _lines_for(source, CONSTANT) == [2, 4]


def test_constant_condition_string_literals() -> None:
    source = 'def gate():\n    if "":\n        a()\n    if "ready":\n        b()\n'
    # Empty AND non-empty string literals are both constants.
    assert _lines_for(source, CONSTANT) == [2, 4]


def test_constant_condition_false_and_one() -> None:
    source = "def selector():\n    if False:\n        x()\n    if 1:\n        y()\n"
    assert _lines_for(source, CONSTANT) == [2, 4]


# --- ai-slop/constant-condition : ADVERSARIAL NEGATIVES -------------------------------------


def test_constant_condition_ignores_while_true() -> None:
    source = "def pump():\n    while True:\n        step()\n        if should_stop():\n            break\n"
    assert CONSTANT not in _rules_fired(source)


def test_constant_condition_ignores_type_checking_name() -> None:
    source = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from foo import Bar\n"
    assert CONSTANT not in _rules_fired(source)


def test_constant_condition_ignores_named_flag() -> None:
    source = "DEBUG = compute_debug()\n\n\ndef log(msg):\n    if DEBUG:\n        emit(msg)\n"
    assert CONSTANT not in _rules_fired(source)


def test_constant_condition_ignores_real_comparison() -> None:
    source = "def check(n):\n    if n == 1:\n        single()\n    if len(items) > 0:\n        many()\n"
    assert CONSTANT not in _rules_fired(source)


def test_constant_condition_ignores_ternary_expression() -> None:
    # IfExp (ternary) is out of scope and must never fire, even with a literal branch.
    source = "def pick(flag):\n    return 1 if flag else 0\n"
    assert CONSTANT not in _rules_fired(source)


def test_constant_condition_generalisation_literal_inside_string() -> None:
    # A naive scan for `if True:` in text would match this docstring/comment; the AST must not.
    source = (
        "def documented():\n"
        '    """Example in docs: write `if True:` only as a placeholder."""\n'
        "    # never do `if 0:` either\n"
        "    if ready():\n"
        "        go()\n"
    )
    assert CONSTANT not in _rules_fired(source)


# --- ai-slop/empty-function : POSITIVES -----------------------------------------------------


def test_empty_function_pass_body() -> None:
    source = "def loader():\n    pass\n"
    assert EMPTY in _rules_fired(source)


def test_empty_function_ellipsis_body() -> None:
    source = "def fetcher():\n    ...\n"
    assert EMPTY in _rules_fired(source)


def test_empty_function_docstring_then_pass() -> None:
    source = 'def initialise():\n    """Set up the thing."""\n    pass\n'
    assert EMPTY in _rules_fired(source)


def test_empty_function_async_and_plain_method() -> None:
    source = (
        "class Widget:\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "    async def refresh(self):\n"
        "        ...\n"
    )
    # Plain class (not Protocol/ABC), no stub decorators -> both methods are placeholders.
    assert _lines_for(source, EMPTY) == [2, 5]


# --- ai-slop/empty-function : ADVERSARIAL NEGATIVES -----------------------------------------


def test_empty_function_ignores_abstractmethod() -> None:
    source = (
        "import abc\n"
        "\n"
        "\n"
        "class Base(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def compute(self):\n"
        "        ...\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_ignores_overload() -> None:
    source = (
        "from typing import overload\n"
        "\n"
        "\n"
        "@overload\n"
        "def parse(x: int) -> int: ...\n"
        "@overload\n"
        "def parse(x: str) -> str: ...\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_ignores_protocol_members() -> None:
    source = (
        "from typing import Protocol\n"
        "\n"
        "\n"
        "class Reader(Protocol):\n"
        "    def read(self) -> bytes: ...\n"
        "    def close(self) -> None: ...\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_ignores_custom_protocol_suffix() -> None:
    # Any base ending in 'Protocol' is treated as a stub context.
    source = "class Sink(WriterProtocol):\n    def write(self, data) -> None: ...\n"
    assert EMPTY not in _rules_fired(source)


def test_empty_function_ignores_real_body() -> None:
    source = (
        "def handler():\n"
        '    """Do real work."""\n'
        "    raise NotImplementedError('later')\n"
        "\n"
        "\n"
        "def other():\n"
        "    return 42\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_ignores_docstring_only_body() -> None:
    # A docstring-only body is documentation, not a placeholder -> not flagged.
    source = 'def described():\n    """This documents an interface contract elsewhere."""\n'
    assert EMPTY not in _rules_fired(source)


def test_empty_function_generalisation_pass_inside_real_branch() -> None:
    # A `pass` that is one statement among others (a deliberate no-op branch) is a real body;
    # a naive "body contains pass" match would wrongly fire.
    source = (
        "def dispatch(event):\n"
        "    if event.kind == 'noop':\n"
        "        pass\n"
        "    else:\n"
        "        process(event)\n"
    )
    assert EMPTY not in _rules_fired(source)


# --- empty-function: TYPE_CHECKING stubs are type-only declarations, never placeholders ---


def test_empty_function_under_type_checking_is_spared() -> None:
    # A `...`-body method inside `if TYPE_CHECKING:` is a type-only interface stub (the _service_members
    # shadow-class pattern), not an unfinished placeholder.
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    class Members:\n"
        "        def do_thing(self) -> int: ...\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_under_qualified_typing_type_checking_is_spared() -> None:
    source = (
        "import typing\n" "if typing.TYPE_CHECKING:\n" "    class Shadow:\n" "        def fetch(self) -> str: ...\n"
    )
    assert EMPTY not in _rules_fired(source)


def test_empty_function_outside_type_checking_still_fires() -> None:
    # The SAME stub at runtime scope IS an unfinished placeholder — proves the guard is the TYPE_CHECKING
    # context, not the `...` shape (a different name than the spared cases above).
    source = "class Members:\n    def do_thing(self) -> int: ...\n"
    assert EMPTY in _rules_fired(source)
