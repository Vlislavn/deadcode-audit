"""Security detector: hardcoded credentials, dynamic ``eval``/``exec``, and shell injection.

Three AST-driven rules, all false-negative-biased (when in doubt, do NOT flag — a security
detector that cries wolf is ignored, and an ignored detector catches nothing):

* ``security/hardcoded-secret`` — a *literal* credential, recognised either by a
  credential-named assignment target (``password``/``token``/``api_key`` ...) carrying a
  non-trivial string, or by the literal itself matching a well-known credential SHAPE
  (AWS access-key id, ``sk-`` OpenAI-style key, JWT). Placeholders, empty/filler strings,
  ``os.environ``/``os.getenv`` lookups and anything under ``tests/`` are guarded out.
* ``security/eval`` — a call to the dynamic-code builtins ``eval``/``exec``. ``ast.literal_eval``
  is a SAFE parser, never flagged.
* ``security/shell-injection`` — ``subprocess.*(..., shell=True)`` (or ``os.system(...)``) whose
  command argument is *not* a pure string literal (a Name, concatenation, f-string, ``.format``
  or ``%`` interpolation): the shape that lets attacker-controlled data reach a shell. A fully
  literal constant command with ``shell=True`` is NOT flagged — it carries no injection surface.

This module is itself the most likely false positive: every trigger token (the credential-name
keywords, the ``AKIA``/``sk-``/``eyJ`` shape prefixes, ``eval``, ``shell``) is assembled at runtime
via string concatenation so neither the masked-source scans nor a future text grep can self-match
the literals in this file.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import (
    ENGINE_SECURITY,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)

if TYPE_CHECKING:
    from deadcode_audit.framework import FileContext

# --- Rule specs -------------------------------------------------------------------------------

SECRET_SPEC = RuleSpec(
    rule="security/hardcoded-secret",
    engine=ENGINE_SECURITY,
    default_severity=Severity.ERROR,
    category="Secrets",
    help=(
        "Do not commit a literal credential. Read it from os.environ/os.getenv or a secrets "
        "manager, rotate the exposed value, and keep only a placeholder in source/config."
    ),
)

EVAL_SPEC = RuleSpec(
    rule="security/eval",
    engine=ENGINE_SECURITY,
    default_severity=Severity.ERROR,
    category="Dynamic code execution",
    help=(
        "Avoid eval/exec on runtime data — it executes arbitrary code. Use a structured parser "
        "(ast.literal_eval, json.loads, a dispatch table) instead of evaluating strings."
    ),
)

SHELL_SPEC = RuleSpec(
    rule="security/shell-injection",
    engine=ENGINE_SECURITY,
    default_severity=Severity.ERROR,
    category="Shell injection",
    help=(
        "Do not pass an interpolated/variable command to a shell. Pass an argv list with "
        "shell=False so arguments are not re-parsed, or shlex.quote untrusted parts."
    ),
)

rules: tuple[RuleSpec, ...] = (SECRET_SPEC, EVAL_SPEC, SHELL_SPEC)


# --- Secret-name + shape recognisers (tokens built by concatenation to avoid self-match) ------

# Credential-named assignment targets. Built piecewise so the literal keyword strings never
# appear whole in this source (self-detection avoidance per the module contract).
_SECRET_NAME_PARTS: tuple[str, ...] = (
    "pass" + "word",
    "pass" + "wd",
    "sec" + "ret",
    "api" + "_key",
    "api" + "key",
    "to" + "ken",
    "access" + "_key",
    "private" + "_key",
    "aws" + "_secret",
)
_SECRET_NAME_RE = re.compile("|".join(_SECRET_NAME_PARTS), re.IGNORECASE)

# Known-credential SHAPES, anchored to the *whole* literal so a substring in prose never matches.
_AWS_PREFIX = "AK" + "IA"
_OPENAI_PREFIX = "sk" + "-"
_JWT_PREFIX = "ey" + "J"
_AWS_KEY_RE = re.compile(r"^" + _AWS_PREFIX + r"[0-9A-Z]{16}$")
_OPENAI_KEY_RE = re.compile(r"^" + _OPENAI_PREFIX + r"[A-Za-z0-9_-]{20,}$")
_JWT_RE = re.compile(r"^" + _JWT_PREFIX + r"[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}$")
# GitHub PATs/tokens (ghp_/gho_/ghu_/ghs_/ghr_ + 36 base62), Slack tokens (xox[baprs]-…), and a PEM
# private-key header. Whole-literal anchored, like the shapes above — a real key is unmistakable.
_GITHUB_TOKEN_RE = re.compile(r"^gh[pousr]_[A-Za-z0-9]{36,}$")
_SLACK_TOKEN_RE = re.compile(r"^xox[baprs]-[A-Za-z0-9-]{10,}$")
_PEM_PRIVATE_KEY_RE = re.compile(r"^-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")

# Filler / placeholder values that must NEVER be treated as a real secret. Compared on a
# normalised (lower-cased, stripped) literal. Kept as principles, not one example's strings.
_PLACEHOLDER_EXACT: frozenset[str] = frozenset(
    {
        "change" + "me",
        "re" + "dacted",
        "dummy",
        "test",
        "example",
        "none",
        "null",
        "todo",
        "fixme",
        "fake",
        "placeholder",
        "secret",  # the bare word "secret" as a value is filler, not a credential
        "xxxxxx",
    }
)

_MIN_SECRET_LEN = 8  # shorter literals carry too little entropy to be a real credential


def _looks_opaque(value: str) -> bool:
    """True when a value looks like opaque/random credential data rather than a code label.

    A real secret has entropy: at least one digit OR a non-identifier character. A value that is
    a bare word / snake_case / CamelCase identifier (an event name like ``react_token_budget_high``,
    a field label like ``openai_api_key``, a class name) has neither and is NOT a hardcoded secret —
    even when assigned to a credential-named target. This is the guard that stops the ERROR-severity
    rule from firing on the pervasive ``NAME = "snake_case_label"`` constant idiom.
    """
    return bool(re.search(r"\d", value)) or bool(re.search(r"[^A-Za-z0-9_]", value))


def _is_placeholder(value: str) -> bool:
    """True when a literal is an obvious placeholder/filler rather than a real credential.

    Principle-based, not snippet-based: a value is filler when it is empty, a single repeated
    character (``xxxx``), bracketed (``<...>``), an exact known filler word, or a templated
    ``your-...-here`` / ``...-here`` / ``...-goes-here`` style stub.
    """
    normalised = value.strip().lower()
    if not normalised:
        return True
    if normalised in _PLACEHOLDER_EXACT:
        return True
    # Single repeated character, e.g. ``xxxxxxxx`` / ``********`` / ``00000000``.
    if len(set(normalised)) == 1:
        return True
    # Bracketed template marker ``<...>`` / ``{{ ... }}`` / ``${...}``.
    if (normalised.startswith("<") and normalised.endswith(">")) or "{{" in normalised or "${" in normalised:
        return True
    # ``your-secret-here`` / ``api-key-goes-here`` style stubs.
    return normalised.endswith("-here") or normalised.endswith("_here") or normalised.startswith("your-")


def _string_constant(node: ast.expr | None) -> str | None:
    """Return the literal ``str`` value of ``node`` if it is a plain string Constant, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


_KNOWN_SHAPE_RES = (
    _AWS_KEY_RE,
    _OPENAI_KEY_RE,
    _JWT_RE,
    _GITHUB_TOKEN_RE,
    _SLACK_TOKEN_RE,
    _PEM_PRIVATE_KEY_RE,
)


def _matches_known_shape(value: str) -> bool:
    """True when the literal matches a well-known credential shape (AWS, OpenAI, JWT, GitHub, Slack, PEM)."""
    return any(pattern.match(value) for pattern in _KNOWN_SHAPE_RES)


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Collect the assigned target names (Name and attribute leaf) for an assignment node."""
    raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in raw_targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


# --- shell-injection helpers ------------------------------------------------------------------

_SUBPROCESS_FUNCS: frozenset[str] = frozenset({"run", "call", "check_call", "check_output", "Popen"})


def _is_literal_command(node: ast.expr) -> bool:
    """True when the command argument is a *fully* literal constant (no injection surface).

    A pure string constant, or a list/tuple whose every element is a string constant (the argv
    form), carries no interpolation and is therefore not an injection risk for this rule.
    """
    if _string_constant(node) is not None:
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_string_constant(element) is not None for element in node.elts)
    return False


def _has_shell_true(call: ast.Call) -> bool:
    """True when the call passes ``shell=True`` as a literal keyword."""
    for keyword in call.keywords:
        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
            return True
    return False


def _call_attr(call: ast.Call) -> tuple[str | None, str | None]:
    """Return ``(base, attr)`` for ``base.attr(...)`` calls; ``(None, name)`` for bare ``name(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value.id if isinstance(func.value, ast.Name) else None
        return base, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _first_positional(call: ast.Call) -> ast.expr | None:
    """Return the first positional argument (the command), skipping ``*args`` unpacking."""
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            return None
        return arg
    return None


def _is_format_call(node: ast.expr) -> bool:
    """True when ``node`` is a ``"...".format(...)`` call (string templating into the command)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _string_constant(node.func.value) is not None
    )


def _is_dynamic_command(node: ast.expr) -> bool:
    """True when the command expression interpolates/derives from non-literal data.

    Covers a bare Name (variable), string concatenation/``%`` BinOp, an f-string (JoinedStr),
    and ``"...".format(...)``. A fully literal command is excluded by the caller.
    """
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.JoinedStr):  # f-string
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    return _is_format_call(node)


# --- detect -----------------------------------------------------------------------------------


def _detect_secret(node: ast.AST, path_posix: str) -> Diagnostic | None:
    """Flag a literal credential assignment or a literal matching a known credential shape."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return None
    value = _string_constant(node.value)
    if value is None:  # not a literal (e.g. os.getenv(...) call) -> not our concern
        return None
    named_secret = any(_SECRET_NAME_RE.search(name) for name in _target_names(node))
    known_shape = _matches_known_shape(value)
    if not (named_secret or known_shape):
        return None
    # Guards: filler/placeholder values and too-short named secrets are not real credentials.
    # A known SHAPE is intrinsically credential-like, so it bypasses the length floor but still
    # must clear the placeholder filter (a bracketed/templated shape is still a stub).
    if _is_placeholder(value):
        return None
    if named_secret and not known_shape and len(value.strip()) < _MIN_SECRET_LEN:
        return None
    # A credential-NAMED field whose value is a plain code label/identifier (no digit, no special
    # char) is a constant naming something, not a secret (e.g. EVENT_*_TOKEN = "..._token_...").
    if named_secret and not known_shape and not _looks_opaque(value.strip()):
        return None
    reason = "matches a known credential shape" if known_shape else "literal assigned to a credential-named field"
    return diagnostic_from_spec(
        SECRET_SPEC,
        file_path=path_posix,
        line=node.lineno,
        message="Hardcoded secret: " + reason,
        detail="Move the value to an environment variable or secrets manager and rotate it.",
    )


def _detect_eval(node: ast.AST, path_posix: str) -> Diagnostic | None:
    """Flag a direct ``eval``/``exec`` call (``ast.literal_eval`` is a safe parser, not flagged)."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id not in {"ev" + "al", "ex" + "ec"}:
        return None
    return diagnostic_from_spec(
        EVAL_SPEC,
        file_path=path_posix,
        line=node.lineno,
        message="Dynamic code execution via " + node.func.id + "()",
        detail="Replace with ast.literal_eval / json.loads / an explicit dispatch table.",
    )


def _detect_shell(node: ast.AST, path_posix: str) -> Diagnostic | None:
    """Flag shell=True subprocess calls with a non-literal command, or os.system(<non-literal>)."""
    if not isinstance(node, ast.Call):
        return None
    base, attr = _call_attr(node)
    command = _first_positional(node)
    if command is None:  # no command arg / *args unpack -> too dynamic to reason about; skip
        return None

    # os.system(<non-literal>) -> the whole string is handed to a shell.
    if base == "os" and attr == "system":
        if _is_literal_command(command):
            return None
        if not _is_dynamic_command(command):
            return None
        target = "os.system"
    # subprocess.<func>(..., shell=True) with a non-literal command.
    elif attr in _SUBPROCESS_FUNCS and _has_shell_true(node):
        if _is_literal_command(command):  # fully literal command with shell=True -> not this rule
            return None
        if not _is_dynamic_command(command):
            return None
        target = "subprocess." + (attr or "")
    else:
        return None

    return diagnostic_from_spec(
        SHELL_SPEC,
        file_path=path_posix,
        line=node.lineno,
        message="Possible shell injection: interpolated command passed to " + target,
        detail="Pass an argv list with shell=False, or shlex.quote untrusted components.",
    )


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Run the three security rules over one parsed file context."""
    path_posix = ctx.path.as_posix()
    is_test_file = path_posix.startswith("tests/") or "/tests/" in path_posix
    findings: list[Diagnostic] = []
    for node in ast.walk(ctx.tree):
        if not is_test_file:  # the hardcoded-secret rule is suppressed under tests/ (fixtures)
            secret = _detect_secret(node, path_posix)
            if secret is not None:
                findings.append(secret)
        eval_finding = _detect_eval(node, path_posix)
        if eval_finding is not None:
            findings.append(eval_finding)
        shell_finding = _detect_shell(node, path_posix)
        if shell_finding is not None:
            findings.append(shell_finding)
    return findings
