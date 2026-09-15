"""AI-slop comment detectors (ports of the aislop ``comments.ts`` / ``narrative-comments.ts`` rules).

Three rules, all comment-only, driven off ``tokenize`` COMMENT tokens (never raw-source regex —
so an example pattern living inside a string/docstring can never self-trigger, and code on the
same line is read structurally, not by string matching):

* ``ai-slop/todo-stub`` — a TODO/FIXME/HACK/XXX marker with no tracker reference (issue ``#123``,
  a ``JIRA-123`` style key, or an http(s) URL). A tracked TODO is a real backlog item; an
  untracked one is the orphaned stub an agent leaves behind.
* ``ai-slop/trivial-comment`` — a comment that merely restates the adjacent code (``# import os``
  above ``import os``; ``# return x`` on a ``return x`` line). Fires ONLY when the comment's
  content words are a subset of the identifiers/keywords on the adjacent code line AND the comment
  adds no "why" — so any comment carrying information not in the code is spared.
* ``ai-slop/narrative-comment`` — decorative separators (``# =========``) and section/phase
  headers (``# Step 1:`` / ``# Phase 2`` / ``# === Section ===``). Real prose is spared.

Each rule encodes a *principle*, not a snippet, and is false-negative-biased: when a comment
could plausibly carry information the code does not, we do not flag it.
"""

from __future__ import annotations

import ast
import io
import re
import token as token_mod
import tokenize
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

# --- Rule specs --------------------------------------------------------------------------------

TODO_STUB = RuleSpec(
    rule="ai-slop/todo-stub",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="AI Slop",
    help="Either resolve the TODO/FIXME now, or link a tracker reference (#123, KEY-123, or a URL) "
    "so it is a tracked item rather than an orphaned stub.",
)

TRIVIAL_COMMENT = RuleSpec(
    rule="ai-slop/trivial-comment",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="AI Slop",
    help="Remove the comment: it restates the adjacent code without adding any information "
    "(the code already says this). Keep comments that explain *why*.",
    style=True,
)

NARRATIVE_COMMENT = RuleSpec(
    rule="ai-slop/narrative-comment",
    engine=ENGINE_AI_SLOP,
    default_severity=Severity.INFO,
    category="AI Slop",
    help="Drop the decorative separator / section header; use blank lines and real declarations "
    "to structure code instead of ASCII art or 'Step N' narration.",
    style=True,
)

rules: tuple[RuleSpec, ...] = (TODO_STUB, TRIVIAL_COMMENT, NARRATIVE_COMMENT)


# --- Shared comment model ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Comment:
    """One ``#`` comment: its body (marker stripped), 1-based line, and whether code precedes it."""

    line: int  # 1-based physical line of the comment token
    col: int  # 0-based start column of the '#'
    body: str  # text after the leading '#', stripped
    inline: bool  # True when source code precedes the comment on the same physical line


def _collect_comments(source: str) -> list[_Comment]:
    """Return every ``#`` comment via :mod:`tokenize` (positions are authoritative, not regex).

    ``inline`` is True when the comment is preceded by a non-NL/non-INDENT token on its own line —
    i.e. there is code to its left. A standalone comment has only whitespace before the ``#``.
    """
    comments: list[_Comment] = []
    code_token_lines: set[int] = set()
    readline = io.StringIO(source).readline
    tokens = list(tokenize.generate_tokens(readline))
    for tok in tokens:
        # A "code" token on a line is anything that is not trivia (NL/NEWLINE/INDENT/DEDENT/
        # COMMENT/ENCODING/ENDMARKER); used to decide inline-ness.
        if tok.type not in _TRIVIA_TYPES and tok.start[0] == tok.end[0]:
            code_token_lines.add(tok.start[0])
        elif tok.type not in _TRIVIA_TYPES:
            for ln in range(tok.start[0], tok.end[0] + 1):
                code_token_lines.add(ln)
    for tok in tokens:
        if tok.type != token_mod.COMMENT:
            continue
        line = tok.start[0]
        # Inline iff a code token starts strictly before the comment on the same line. We detect
        # this by checking whether any non-trivia token has start row == line and col < comment col.
        inline = any(t.type not in _TRIVIA_TYPES and t.start[0] == line and t.start[1] < tok.start[1] for t in tokens)
        comments.append(
            _Comment(
                line=line,
                col=tok.start[1],
                body=tok.string.lstrip("#").strip(),
                inline=inline,
            )
        )
    return comments


_TRIVIA_TYPES = frozenset(
    {
        token_mod.NL,
        token_mod.NEWLINE,
        token_mod.INDENT,
        token_mod.DEDENT,
        token_mod.COMMENT,
        token_mod.ENCODING,
        token_mod.ENDMARKER,
    }
)


# --- todo-stub ---------------------------------------------------------------------------------

# A TODO *marker* leads its clause: the comment body starts with TODO/FIXME/HACK/XXX (optionally
# after a list bullet ``- ``/``1. ``/``a) ``). This is the universal TODO convention — and it is the
# guard that distinguishes the directive from the SAME word used as a noun mid-sentence ("the sticky
# TODO widget", "core (.../Todo/...)"), which is prose, not an orphaned stub. Case-insensitive.
_TODO_MARKER = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+|[A-Za-z][.)]\s+)?(?:TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)

# A tracker reference that spares the comment: an issue ``#123``, a JIRA-style ``KEY-123`` (>=2
# uppercase letters, a hyphen, digits), or an http(s) URL. Any one of these means it is tracked.
_ISSUE_REF = re.compile(r"#\d+")
_JIRA_REF = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_URL_REF = re.compile(r"https?://\S+")


def _has_tracker_reference(body: str) -> bool:
    """True when the comment body carries an issue/tracker reference (so the TODO is tracked)."""
    return bool(_ISSUE_REF.search(body) or _JIRA_REF.search(body) or _URL_REF.search(body))


def _standalone_comment_lines(comments: list[_Comment]) -> frozenset[int]:
    """Lines of standalone (non-inline) comments — used to detect consecutive comment runs."""
    return frozenset(c.line for c in comments if not c.inline)


def _todo_stub_findings(ctx: FileContext, comments: list[_Comment]) -> list[Diagnostic]:
    """Flag TODO/FIXME/HACK/XXX comments (marker-led, untracked, single-line) as orphaned stubs.

    Three guards spare legitimate uses: the marker must LEAD the comment (so the same word used as a
    noun in prose is ignored); a tracker reference (#id / KEY-123 / URL) means it is a tracked item;
    and a TODO inside a multi-line comment RUN is a documented decision block, not a one-line stub.
    """
    standalone = _standalone_comment_lines(comments)
    findings: list[Diagnostic] = []
    for comment in comments:
        if not _TODO_MARKER.match(comment.body):
            continue
        if _has_tracker_reference(comment.body):
            continue
        # A standalone comment flanked by another comment line is part of an explanatory run, not an
        # orphaned one-liner (mirrors the aislop multi-line-comment-run guard) -> spare it.
        if not comment.inline and ((comment.line - 1) in standalone or (comment.line + 1) in standalone):
            continue
        findings.append(
            diagnostic_from_spec(
                TODO_STUB,
                file_path=ctx.path.as_posix(),
                line=comment.line,
                message="TODO/FIXME without a tracker reference (link an issue #id, KEY-123, or URL, or resolve it)",
                detail=comment.body or None,
            )
        )
    return findings


# --- trivial-comment ---------------------------------------------------------------------------

# Markers that hand the comment to another rule or mark it as machine-directive, never trivial.
_DIRECTIVE_PREFIXES = ("!", "-*-")  # shebang body (after '#'), coding cookie
_DIRECTIVE_KEYWORDS = re.compile(r"\b(?:type|noqa|pragma|pylint|mypy|ruff|flake8|isort)\b", re.IGNORECASE)
_TODO_OWNED = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)

# A comment adds a *reason*/condition (the "why") rather than restating the "what" when it carries
# one of these markers; such comments are spared. Mirrors the aislop EXPLANATORY keyword guard.
_WHY_MARKER = re.compile(
    r"\b(?:because|since|so\s+that|otherwise|workaround|caveat|note|warning|important|assume[sd]?|"
    r"if|when|unless|until|only|except|needs?|must|should|ensure|avoid|prevent|requires?|"
    r"why|reason|e\.g\.|i\.e\.|for\s+example|in\s+order\s+to|to\s+avoid|to\s+prevent|hack\s+for)\b",
    re.IGNORECASE,
)

# Tokenise comment prose into lowercase word stems; we compare SET overlap against the adjacent
# line's lexemes + the verbs its AST operation expresses, so paraphrase detection stays conservative.
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# English filler words that carry no code meaning; ignored when forming the comment's content set so
# "# now return the value" still reduces to {return, value}.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "to",
        "of",
        "for",
        "and",
        "or",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "into",
        "now",
        "then",
        "here",
        "value",
        "result",
        "all",
        "each",
        "every",
        "we",
        "is",
        "are",
        "be",
    }
)


def _word_stem(word: str) -> str:
    """Lowercase a word and strip a trailing verb/plural suffix (``returns`` -> ``return``).

    Used ONLY to map a comment's verb to the code keyword/identifier it restates; it never widens
    the match because the result must still appear verbatim in the adjacent code token set.
    """
    lower = word.lower()
    for suffix in ("ements", "ement", "ing", "es", "ed", "s"):
        if lower.endswith(suffix) and len(lower) - len(suffix) >= 3:
            return lower[: -len(suffix)]
    return lower


def _comment_content_words(body: str) -> set[str]:
    """Stemmed, stop-word-filtered content words of a comment body (its semantic surface)."""
    return {_word_stem(w) for w in _WORD_RE.findall(body)} - _STOPWORDS


def _code_token_words_by_line(source: str) -> dict[int, set[str]]:
    """Map each 1-based source line to the set of identifier/keyword token stems on it.

    Built from :mod:`tokenize` NAME tokens (Python keywords are NAME tokens at lexer level) so a
    comment is compared against the *real* code lexemes, not a regex over raw text. Each lexeme is
    added both verbatim and stemmed, so a comment verb ``returning`` can match the keyword ``return``.
    """
    by_line: dict[int, set[str]] = {}
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type != token_mod.NAME:
            continue
        bucket = by_line.setdefault(tok.start[0], set())
        bucket.add(tok.string.lower())
        bucket.add(_word_stem(tok.string))
    return by_line


# Verbs that a comment uses to *paraphrase* a code OPERATION, keyed by the AST node that operation
# is. This is a structural vocabulary (one entry per operation kind), not a per-snippet table: any
# ``ast.AugAssign`` with ``ast.Add`` is an increment regardless of identifiers, so ``# bump count``
# over ``count += 1`` and ``# increment idx`` over ``idx += step`` both reduce identically.
_AUGOP_VERBS: dict[type[ast.operator], frozenset[str]] = {
    ast.Add: frozenset({"increment", "increas", "add", "bump", "raise", "grow"}),
    ast.Sub: frozenset({"decrement", "decreas", "subtract", "reduc", "lower", "drop"}),
    ast.Mult: frozenset({"multipli", "scal", "multiply"}),
    ast.Div: frozenset({"divid", "divide"}),
}
_NODE_VERBS: list[tuple[type[ast.AST], frozenset[str]]] = [
    (ast.Return, frozenset({"return"})),
    (ast.Import, frozenset({"import"})),
    (ast.ImportFrom, frozenset({"import"})),
    (ast.Raise, frozenset({"raise", "throw"})),
    (ast.Assert, frozenset({"assert", "check"})),
    (ast.Delete, frozenset({"delete", "remov", "del"})),
    (ast.For, frozenset({"loop", "iterat"})),
    (ast.While, frozenset({"loop"})),
    (ast.Yield, frozenset({"yield"})),
]


def _operation_verbs_by_line(tree: ast.Module) -> dict[int, set[str]]:
    """Map each 1-based line to the stemmed verbs that paraphrase the AST operation starting there.

    Structural, not lexical: an ``a += b`` line yields ``{increment, add, ...}`` from its
    ``AugAssign``+``Add`` shape; a ``return`` line yields ``{return}``. This lets a comment verb that
    names *what the line does* (rather than the identifiers it touches) count as a restatement —
    without hardcoding any particular variable or snippet.
    """
    by_line: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        if isinstance(node, ast.AugAssign):
            verbs = _AUGOP_VERBS.get(type(node.op))
            if verbs is not None:
                by_line.setdefault(line, set()).update(_word_stem(v) for v in verbs)
            continue
        for node_type, verbs in _NODE_VERBS:
            if isinstance(node, node_type):
                by_line.setdefault(line, set()).update(_word_stem(v) for v in verbs)
                break
    return by_line


def _adjacent_code_line(comment: _Comment, code_words: dict[int, set[str]], total_lines: int) -> int | None:
    """Return the code line the comment describes: its own line if inline, else the next code line.

    For a standalone comment, the "next code line" is the next line that actually has code lexemes;
    intervening blank/comment lines are skipped. Returns None when no code line is adjacent.
    """
    if comment.inline:
        return comment.line if comment.line in code_words else None
    for candidate in range(comment.line + 1, total_lines + 2):
        if candidate in code_words:
            return candidate
    return None


# Compound (block-opening) statements: a comment above one of these LABELS the block it introduces,
# it does not restate a single operation — so trivial-comment must spare it (section/block label).
_BLOCK_STATEMENTS = (
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.Try,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _block_opening_lines(tree: ast.Module) -> set[int]:
    """Lines that open a compound statement (``if``/``for``/``def``/``class``/...).

    A standalone comment immediately above such a line is a block label, not a restatement.
    """
    return {node.lineno for node in ast.walk(tree) if isinstance(node, _BLOCK_STATEMENTS)}


def _separator_framed_lines(comments: list[_Comment]) -> set[int]:
    """Comment lines that sit directly next to a separator comment (framed section headers).

    ``# ===`` / ``# Main`` / ``# ===`` makes ``# Main`` a section heading owned by narrative-comment,
    so trivial-comment spares any comment whose immediate comment-neighbour is a decorative separator.
    """
    sep_lines = {c.line for c in comments if _is_separator(c.body)}
    framed: set[int] = set()
    for comment in comments:
        if (comment.line - 1) in sep_lines or (comment.line + 1) in sep_lines:
            framed.add(comment.line)
    return framed


def _is_directive_or_owned(body: str) -> bool:
    """True for comments owned by another rule or that are machine directives (never trivial)."""
    if body.startswith(_DIRECTIVE_PREFIXES):
        return True
    return bool(_DIRECTIVE_KEYWORDS.search(body) or _TODO_OWNED.search(body))


def _trivial_comment_findings(ctx: FileContext, comments: list[_Comment]) -> list[Diagnostic]:
    """Flag comments that are a near-paraphrase of the adjacent code's identifiers/keywords.

    Conservative: fires only when EVERY content word of the comment also appears (verbatim or
    stemmed) among the adjacent code line's lexemes AND the comment has >= 1 content word. Comments
    that add a "why", directives, TODO markers, separators, or that have no adjacent code are spared.
    """
    code_words = _code_token_words_by_line(ctx.source)
    op_verbs = _operation_verbs_by_line(ctx.tree)
    block_lines = _block_opening_lines(ctx.tree)
    framed_lines = _separator_framed_lines(comments)
    total_lines = len(ctx.lines)
    findings: list[Diagnostic] = []
    for comment in comments:
        body = comment.body
        if not body or _is_directive_or_owned(body) or _WHY_MARKER.search(body):
            continue
        if _is_separator(body) or _section_header_kind(body) is not None:
            continue  # owned by narrative-comment
        if comment.line in framed_lines:
            continue  # a separator-framed heading is a section label, not a restatement
        content = _comment_content_words(body)
        if not content:
            continue
        target_line = _adjacent_code_line(comment, code_words, total_lines)
        if target_line is None:
            continue
        # A standalone comment above a block-opening statement LABELS the block (e.g. ``# Dependencies``
        # above ``if ...:``); that is navigation, not a single-operation restatement -> spare it.
        if not comment.inline and target_line in block_lines:
            continue
        # The line's restatement vocabulary: its identifiers/keywords plus the verbs its AST
        # operation paraphrases (so ``# increment counter`` restates ``counter += 1``).
        line_words = code_words[target_line] | op_verbs.get(target_line, set())
        # Restatement == comment words are a (non-empty) subset of that vocabulary.
        if content <= line_words:
            findings.append(
                diagnostic_from_spec(
                    TRIVIAL_COMMENT,
                    file_path=ctx.path.as_posix(),
                    line=comment.line,
                    message="comment restates the adjacent code without adding information",
                    detail=body or None,
                )
            )
    return findings


# --- narrative-comment -------------------------------------------------------------------------

# A decorative separator: a run of >= 6 punctuation/box chars, optionally framing whitespace only.
_SEPARATOR = re.compile(r"^[\s]*[-=*#_~]{6,}[\s]*$")
# A separator with text framed by punctuation on BOTH sides: ``=== Section ===``.
_FRAMED_HEADER = re.compile(r"^[-=*#_~]{3,}\s*\S.*?\s*[-=*#_~]{3,}\s*$")
# Section / phase header shapes: a heading WORD + ordinal + delimiter (``Step 1:``, ``Phase 2 -``,
# ``Section 3.``). The number+delimiter is required for every word — so a sentence that merely begins
# with "Section ..." or "Part of ..." (prose) is NOT a header. The framed ``=== Section ===`` form is
# caught separately by ``_FRAMED_HEADER``.
_SECTION_HEADER = re.compile(r"^(?:Step|Phase|Part|Section)\s+\d+\s*[:.\-)]", re.IGNORECASE)


def _is_separator(body: str) -> bool:
    """True for a pure decorative separator (>= 6 repeated punctuation chars)."""
    return bool(_SEPARATOR.match(body))


def _section_header_kind(body: str) -> str | None:
    """Classify a section/phase header shape, else None.

    ``framed`` for ``=== X ===``; ``section`` for ``Step N:`` / ``Phase N -`` / ``Section N.``.
    """
    if _FRAMED_HEADER.match(body):
        return "framed"
    if _SECTION_HEADER.match(body):
        return "section"
    return None


def _label_comment_lines(comments: list[_Comment]) -> frozenset[int]:
    """Standalone comment lines that carry a real text LABEL (a section title), not decoration.

    A label is a non-inline comment with at least one alphabetic word that is itself neither a
    decorative separator nor a directive. ``# Routing & Intent`` is a label; ``# ======`` is not.
    """
    labels: set[int] = set()
    for comment in comments:
        body = comment.body
        if comment.inline or not body or _is_separator(body) or _is_directive_or_owned(body):
            continue
        if any(ch.isalpha() for ch in body):
            labels.add(comment.line)
    return frozenset(labels)


def _narrative_findings(ctx: FileContext, comments: list[_Comment]) -> list[Diagnostic]:
    """Flag ONLY a LONE, unlabeled decorative separator (a horizontal rule with no section title).

    A separator that frames a labeled section — the ``# ====`` / ``# Section name`` / ``# ====``
    block — is navigation, not comment art, so it is spared (this was the dominant false positive:
    section dividers inside large schemas/modules). Likewise a section/phase header that carries a
    real label is navigational and spared. Only an isolated separator adjacent to no label remains
    flagged, since that is pure decoration with nothing to structure.
    """
    label_lines = _label_comment_lines(comments)
    findings: list[Diagnostic] = []
    for comment in comments:
        body = comment.body
        if not body or _is_directive_or_owned(body):
            continue
        if not _is_separator(body):
            continue  # section/phase headers carry a label -> navigation, not flagged
        # A separator framing a section label (a label comment directly above or below) is a
        # navigational section divider -> spare it; only a label-less lone rule is decoration.
        if (comment.line - 1) in label_lines or (comment.line + 1) in label_lines:
            continue
        findings.append(
            diagnostic_from_spec(
                NARRATIVE_COMMENT,
                file_path=ctx.path.as_posix(),
                line=comment.line,
                message="lone decorative separator; structure code with blank lines and declarations, not comment art",
                detail=body or None,
            )
        )
    return findings


# --- entry point -------------------------------------------------------------------------------


def detect(ctx: FileContext) -> list[Diagnostic]:
    """Run all three comment rules over one file's comments (tokenised once)."""
    comments = _collect_comments(ctx.source)
    findings: list[Diagnostic] = []
    findings.extend(_todo_stub_findings(ctx, comments))
    findings.extend(_trivial_comment_findings(ctx, comments))
    findings.extend(_narrative_findings(ctx, comments))
    return findings
