"""Tests for the thin-wrapper (verbatim pass-through) AI-slop detector.

Each rule gets >=3 positive cases (varied identifiers/modules, proving the rule generalises) and
>=3 adversarial negative cases (legitimate near-misses that must stay silent), plus an explicit
generalisation negative that a naive snippet matcher would wrongly flag.
"""

from __future__ import annotations

from pathlib import Path

from deadcode_audit.detectors.thin_wrapper import THIN_WRAPPER, detect
from deadcode_audit.framework import build_file_context

RULE = THIN_WRAPPER.rule


def _fire_lines(source: str) -> list[int]:
    ctx = build_file_context(Path("src/x.py"), source)
    return sorted(d.line for d in detect(ctx) if d.rule == RULE)


def _fires(source: str) -> bool:
    return bool(_fire_lines(source))


# --------------------------------------------------------------------------------------------------
# POSITIVE cases — genuine verbatim pass-through wrappers that must fire.
# --------------------------------------------------------------------------------------------------


def test_positive_simple_positional_forward() -> None:
    src = "def wrap(a, b):\n    return compute(a, b)\n"
    assert _fires(src)


def test_positive_single_arg_other_module() -> None:
    src = "def fetch(query):\n    return backend.run(query)\n"
    assert _fires(src)


def test_positive_async_await_forward() -> None:
    src = "async def proxy(payload, headers):\n    return await client.post(payload, headers)\n"
    assert _fires(src)


def test_positive_star_args_and_kwargs_forward() -> None:
    src = "def shim(*args, **kwargs):\n    return delegate(*args, **kwargs)\n"
    assert _fires(src)


def test_positive_positional_plus_varargs() -> None:
    src = "def relay(first, *rest):\n    return sink(first, *rest)\n"
    assert _fires(src)


def test_positive_method_forwards_to_self_method() -> None:
    src = "class C:\n    def do(self, x, y):\n        return self.impl(x, y)\n"
    # 'self' is itself a parameter forwarded as the first positional — a verbatim self.impl forward.
    assert _fires(src)


def test_positive_leading_docstring_then_return() -> None:
    src = 'def thin(value):\n    """Forward to the real thing."""\n    return real(value)\n'
    assert _fires(src)


def test_positive_receiver_is_first_param_not_named_self() -> None:
    # The same structural pattern as ``self.method`` but the receiver is a plain parameter: the
    # first param is consumed as the receiver and the rest are forwarded verbatim.
    src = "def query(conn, sql):\n    return conn.execute(sql)\n"
    assert _fires(src)


def test_positive_nested_wrapper_is_walked() -> None:
    # The finding is reported on the inner ``def`` line (line 2), proving ast.walk reaches nested defs.
    src = "def outer():\n    def inner(a, b):\n        return service.call(a, b)\n    return inner\n"
    assert _fire_lines(src) == [2]


# --------------------------------------------------------------------------------------------------
# ADVERSARIAL NEGATIVE cases — legitimate near-misses that must NOT fire.
# --------------------------------------------------------------------------------------------------


def test_negative_argument_transformed() -> None:
    src = "def w(x, y):\n    return f(x + 1, y)\n"
    assert not _fires(src)


def test_negative_argument_wrapped_in_call() -> None:
    src = "def w(x):\n    return f(g(x))\n"
    assert not _fires(src)


def test_negative_attribute_access_on_arg() -> None:
    src = "def w(obj):\n    return f(obj.attr)\n"
    assert not _fires(src)


def test_negative_extra_literal_keyword_added() -> None:
    src = "def w(x):\n    return f(x, mode=1)\n"
    assert not _fires(src)


def test_negative_extra_positional_literal_added() -> None:
    src = "def w(a, b):\n    return f(a, b, 0)\n"
    assert not _fires(src)


def test_negative_reordered_args() -> None:
    src = "def w(a, b):\n    return f(b, a)\n"
    assert not _fires(src)


def test_negative_renamed_via_keyword() -> None:
    src = "def w(a, b):\n    return f(a=a, b=b)\n"
    assert not _fires(src)


def test_negative_body_more_than_single_return() -> None:
    src = "def w(x):\n    log(x)\n    return f(x)\n"
    assert not _fires(src)


def test_negative_recursion_is_not_a_wrapper() -> None:
    src = "def fact(n):\n    return fact(n)\n"
    assert not _fires(src)


def test_negative_drops_a_parameter() -> None:
    src = "def w(a, b):\n    return f(a)\n"
    assert not _fires(src)


def test_negative_adds_a_parameter_not_owned() -> None:
    src = "def w(a):\n    return f(a, b)\n"  # 'b' is a free variable, not a parameter
    assert not _fires(src)


def test_negative_kwargs_forwarded_under_different_name() -> None:
    src = "def w(*args, **kwargs):\n    return f(*args, **other)\n"
    assert not _fires(src)


def test_positive_param_with_default_still_forwarded_verbatim() -> None:
    # `timeout` has a default but is itself a parameter forwarded positionally and unchanged, so
    # the call is still a pure pass-through and SHOULD fire (no value is injected by the wrapper).
    src = "def w(x, timeout=30):\n    return f(x, timeout)\n"
    assert _fires(src)


def test_negative_returns_non_call() -> None:
    src = "def w(x):\n    return x\n"
    assert not _fires(src)


def test_negative_bare_return() -> None:
    src = "def w(x):\n    return\n"
    assert not _fires(src)


def test_negative_keyword_only_param_not_verified() -> None:
    # kw-only params can't be forwarded positionally in a verifiable role -> stay silent.
    src = "def w(a, *, b):\n    return f(a, b)\n"
    assert not _fires(src)


def test_negative_varargs_declared_but_not_forwarded() -> None:
    src = "def w(a, *rest):\n    return f(a)\n"
    assert not _fires(src)


def test_negative_double_await_or_nested_call_on_result() -> None:
    src = "async def w(x):\n    return await wrap(await f(x))\n"
    assert not _fires(src)


def test_negative_deep_attribute_chain_receiver() -> None:
    # ``self.a.b`` is not a verbatim single-hop receiver; with ``self`` still expected as the first
    # positional, the strict forward check must not match -> stay silent.
    src = "class C:\n    def do(self, x):\n        return self.client.send(x)\n"
    assert not _fires(src)


# --------------------------------------------------------------------------------------------------
# Explicit GENERALISATION negative: a naive "return f(args)" snippet match would trip here, but the
# arguments are NOT a verbatim forward of the function's own parameters, so we must stay silent.
# --------------------------------------------------------------------------------------------------


def test_generalisation_snippet_lookalike_does_not_fire() -> None:
    # Looks exactly like the positive shape `return <name>(<name>, <name>)`, but `b` is forwarded
    # before `a` (reordered) and there is a transform — a naive matcher would flag it.
    src = "def handler(a, b):\n    return dispatch(b, normalize(a))\n"
    assert not _fires(src)


# --- thin-wrapper: legitimate indirections spared, pointless forwards still flagged (tuned) ---


def test_thin_wrapper_decorated_surface_is_spared() -> None:
    assert not _fires("import server\n\n@server.tool()\ndef ping(target):\n    return do_ping(target)\n")


def test_thin_wrapper_factory_constructor_target_is_spared() -> None:
    assert not _fires("def make_widget(cfg):\n    return WidgetNode(cfg)\n")


def test_thin_wrapper_private_module_state_accessor_is_spared() -> None:
    assert not _fires("def current_run():\n    return _RUN_REGISTRY.get()\n")


def test_thin_wrapper_reexport_alias_of_import_is_spared() -> None:
    assert not _fires("from sec import guard_path\n\ndef _guard(payload):\n    return guard_path(payload)\n")


def test_thin_wrapper_pointless_private_forward_still_fires() -> None:
    # bare lowercase target, not imported, undecorated -> genuine no-op indirection.
    assert _fires("def relay(alpha, beta):\n    return shuttle(alpha, beta)\n")
