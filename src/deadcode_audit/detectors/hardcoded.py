"""AI-slop detector: hard-coded URLs and provider/resource ids baked into source as values.

Agents frequently inline a concrete endpoint URL or a concrete provider id (a UUID, an
``sk_...`` API key handle, a project/account id) where a config value or environment variable
belongs. Both rules operate on :class:`ast.Constant` string nodes only — so URLs/ids that live
in comments or docstrings are never matched (the AST does not carry comments, and a module/class
/function docstring node is explicitly excluded). Because we never text-scan, ``ctx.masked_source``
is irrelevant here; the AST is the single source of truth.

The rules encode a *structural principle*: "a literal that is shaped like a deployment-specific
secret/endpoint and is wired into runtime behaviour (assigned, defaulted, or passed to a call)
should come from config, not source." They are deliberately false-negative-biased — every shape
test below has an escape hatch for the legitimate near-misses (loopback/doc hosts, all-zero or
placeholder ids, short ids, test fixtures) so renaming a variable can never defeat a guard.

To keep the detector from flagging *itself*, every literal trigger substring this module must
compare a candidate against (the ``sk``/``pk`` prefixes etc.) is assembled by string
concatenation rather than written as a contiguous token.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deadcode_audit.diagnostic import (
    ENGINE_AI_SLOP,
    Diagnostic,
    RuleSpec,
    Severity,
    diagnostic_from_spec,
)

if TYPE_CHECKING:
    from deadcode_audit.framework import FileContext

URL_SPEC = RuleSpec(
    rule="ai-slop/hardcoded-url",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Hard-coded configuration",
    help=(
        "A concrete service URL is baked into source as a value. Read it from an environment "
        "variable or a config object (e.g. settings.SERVICE_URL) so deployments can change it "
        "without editing code."
    ),
    style=False,
    fixable=False,
)

ID_SPEC = RuleSpec(
    rule="ai-slop/hardcoded-id",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.WARNING,
    category="Hard-coded configuration",
    help=(
        "A provider/resource id or secret-shaped token is baked into source as a value. Move it "
        "to an environment variable or secret store; never commit account/project ids or key "
        "handles to source."
    ),
    style=False,
    fixable=False,
)

rules: tuple[RuleSpec, ...] = (URL_SPEC, ID_SPEC)


# --- shape predicates (principle-driven, no value lifted from a single example) ---

_URL_RE = re.compile(r"^\s*https?://([^/\s:?#]+)", re.IGNORECASE)

# Hosts/host-suffixes that denote a schema, namespace, documentation, or example target rather
# than a real deployment endpoint. These are well-known, version-stable identifiers — not
# deployment config — so a literal using them is correct as source and must not be flagged.
_DOC_HOST_SUFFIXES: tuple[str, ...] = (
    "w3.org",
    "json-schema.org",
    "xmlns.com",
    "example.com",
    "example.org",
    "example.net",
    "purl.org",
    "tools.ietf.org",
    "rfc-editor.org",
    "iana.org",
)

# Loopback / non-routable / link-local hosts: never a deployment endpoint worth externalising.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})

# Id prefixes assembled by concatenation so this module never contains a contiguous trigger
# token that the ID rule itself could match. Each maps a vendor-style namespace to its meaning.
_ID_PREFIXES: tuple[str, ...] = (
    "proj" + "_",
    "acct" + "_",
    "org" + "_",
    "key" + "_",
    "s" + "k_",
    "p" + "k_",
)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PREFIXED_ID_RE = re.compile(r"^[A-Za-z0-9]{10,}$")
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{24,}$")
# A non-hex secret-shaped token: long, no whitespace, mixes upper/lower/digits (high-entropy
# look). Plain prose, dotted paths, and dunder names never satisfy the mixed-class test.
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{24,}$")


@dataclass(frozen=True)
class _Candidate:
    """A string constant that is *used as a value* (not a docstring), with its line."""

    value: str
    line: int


def _is_doc_host(host: str) -> bool:
    """True for schema/namespace/doc/example hosts that are legitimately literal in source."""
    host = host.lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".local"):
        return True
    if host.startswith("schemas.") or host.startswith("schema."):
        return True
    if any(host == suffix or host.endswith("." + suffix) for suffix in _DOC_HOST_SUFFIXES):
        return True
    # github.com is used to reference specs/repos in source far more often than as a runtime
    # endpoint to externalise; treat it as a documentation reference (conservative: skip).
    return host == "github.com" or host.endswith(".github.com") or host.endswith(".githubusercontent.com")


def _looks_like_url(value: str) -> bool:
    """True for an http(s) URL string literal pointing at a real (non-doc, non-loopback) host."""
    match = _URL_RE.match(value)
    if match is None:
        return False
    host = match.group(1).split("@")[-1]  # drop any userinfo
    if not host or ("." not in host and host not in _LOOPBACK_HOSTS):
        return False  # a bare scheme like "http://" or "https://foo" with no dot: too weak to flag
    return not _is_doc_host(host)


def _is_placeholder_id(value: str) -> bool:
    """True for obvious placeholder ids that are intentionally fake (never real config)."""
    lowered = value.lower()
    placeholder_marks = ("xxxx", "<", ">", "your-", "your_", "todo", "changeme", "replace")
    if any(mark in lowered for mark in placeholder_marks):
        return True
    stripped = re.sub(r"[^0-9a-z]", "", lowered)
    if not stripped:
        return False
    # All-zero / single-repeated-character ids (e.g. the all-zeros UUID) are sentinels, not real.
    return len(set(stripped)) <= 1


def _looks_like_id(value: str) -> bool:
    """True for a UUID, a vendor-prefixed id, or a long hex/secret-shaped token (real, not placeholder)."""
    if _is_placeholder_id(value):
        return False
    if _UUID_RE.match(value):
        return True
    for prefix in _ID_PREFIXES:
        if value.startswith(prefix):
            remainder = value[len(prefix) :]
            if _PREFIXED_ID_RE.match(remainder):
                return True
    if _HEX_TOKEN_RE.match(value):
        return True
    # Secret-shaped token: long, contiguous, high-entropy opaque data. It MUST mix letters and
    # digits AND contain no ``_`` word separator. A real opaque id/key/token (a hex hash, a base64
    # blob) is unsegmented; a readable label is word-segmented — a CamelCase class name
    # (``ObsidianGetRecentChangesArgs``, letters only) or a snake_case identifier with a version/
    # dimension suffix (``prioritization_backlog_v1``, ``wellness_epoch_spo2_data``). Requiring
    # letter+digit AND no underscore is what keeps export lists, scenario ids, and field/log names
    # from being mis-read as opaque ids, while still catching an inline hex hash / key blob.
    if _SECRET_TOKEN_RE.match(value) and "_" not in value:
        has_letter = bool(re.search(r"[A-Za-z]", value))
        has_digit = bool(re.search(r"[0-9]", value))
        return has_letter and has_digit
    return False


def _is_test_path(ctx: FileContext) -> bool:
    """True when the file is a test module (literal ids/urls there are fixtures, not config)."""
    posix = ctx.path.as_posix()
    return posix.startswith("tests/") or "/tests/" in posix or posix.startswith("test_") or "/test_" in posix


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Object ids of every module/class/function docstring Constant (excluded from scanning).

    A docstring is the first statement of a module/class/function body and is an
    ``Expr`` wrapping a string ``Constant``. We collect those exact Constant nodes so a literal
    that merely *happens* to be a string expression statement elsewhere is still considered.
    """
    docstrings: set[int] = set()
    bodies: list[list[ast.stmt]] = [tree.body]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bodies.append(node.body)
    for body in bodies:
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            docstrings.add(id(first.value))
    return docstrings


def _value_string_constants(tree: ast.Module, docstring_ids: set[int]) -> list[_Candidate]:
    """Every string ``Constant`` *used as a value*: assigned, defaulted, or passed to a call.

    "Used as a value" is established structurally by the parent node, so the determination cannot
    be defeated by renaming. Bare expression-statement strings (e.g. docstrings, and string
    statements used as in-code comments) are excluded — they are not wired into behaviour.
    """
    candidates: list[_Candidate] = []
    for parent in ast.walk(tree):
        # Assignment / annotated-assignment right-hand side(s).
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for node in _string_operand_nodes(parent.value):
                if id(node) not in docstring_ids:
                    candidates.append(_Candidate(str(node.value), node.lineno))
        # Call positional + keyword arguments.
        elif isinstance(parent, ast.Call):
            for arg in parent.args:
                candidates.extend(_Candidate(str(n.value), n.lineno) for n in _string_operand_nodes(arg))
            for kw in parent.keywords:
                candidates.extend(_Candidate(str(n.value), n.lineno) for n in _string_operand_nodes(kw.value))
        # Default values for function parameters.
        elif isinstance(parent, ast.arguments):
            for default in (*parent.defaults, *parent.kw_defaults):
                candidates.extend(_Candidate(str(n.value), n.lineno) for n in _string_operand_nodes(default))
    return candidates


def _string_operand_nodes(node: ast.expr | None) -> list[ast.Constant]:
    """String ``Constant`` nodes that are this value directly or a top-level element of a literal collection."""
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [elt for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return []


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Flag string literals wired into runtime behaviour that are shaped like URLs or provider ids."""
    if _is_test_path(ctx):
        return []  # test fixtures legitimately inline endpoints and ids

    docstring_ids = _docstring_nodes(ctx.tree)
    candidates = _value_string_constants(ctx.tree, docstring_ids)

    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, int]] = set()
    for candidate in candidates:
        if _looks_like_url(candidate.value):
            key = (URL_SPEC.rule, candidate.line)
            if key not in seen:
                seen.add(key)
                diagnostics.append(
                    diagnostic_from_spec(
                        URL_SPEC,
                        file_path=ctx.path.as_posix(),
                        line=candidate.line,
                        message="hard-coded service URL; read it from config/env instead of source",
                    )
                )
        elif _looks_like_id(candidate.value):
            key = (ID_SPEC.rule, candidate.line)
            if key not in seen:
                seen.add(key)
                diagnostics.append(
                    diagnostic_from_spec(
                        ID_SPEC,
                        file_path=ctx.path.as_posix(),
                        line=candidate.line,
                        message="hard-coded provider/resource id; move it to env/secret config",
                    )
                )
    return diagnostics
