"""Single source of truth for cross-tier configuration constants.

These values used to be duplicated across the codebase:

* the Vulture ``--ignore-names`` list appeared verbatim in the gate AND in
  ``.pre-commit-config.yaml`` (the pre-commit literal is asserted against
  :data:`VULTURE_IGNORE_NAMES` by a drift test rather than re-derived here);
* the mutation "self target" path was hardcoded four times inside the gate.

Centralising them here means a change lands in one place and a drift test can
prove the external copies stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from deadcode_audit.diagnostic import Diagnostic, Severity

# --- Vulture reachability gate (Tier 2) ---

VULTURE_SCAN_ROOT = Path("src")
VULTURE_WHITELIST = Path("scripts/vulture_whitelist.py")
VULTURE_BLOCK_CONFIDENCE = 80
VULTURE_ADVISORY_CONFIDENCE = 60

# Scope-independent name ignores passed to Vulture: ``__exit__`` params (exc_type/tb)
# and typing-only protocol imports referenced solely from string ``cast("...")``
# annotations (DataclassInstance), which Vulture cannot see inside string
# literals. The whitelist file cannot suppress these (it only marks globals).
VULTURE_IGNORE_NAMES: tuple[str, ...] = (
    "exc_type",
    "tb",
    "DataclassInstance",
)


def vulture_ignore_names_arg() -> str:
    """Render :data:`VULTURE_IGNORE_NAMES` as the comma-joined ``--ignore-names`` value."""
    return ",".join(VULTURE_IGNORE_NAMES)


# Decorators that REGISTER a function/class with a framework, which then invokes it — the
# invocation is invisible to static analysis, so Vulture would false-positively report the
# decorated symbol as unused. This is the SOTA, configurable replacement for a blunt
# "skip all decorated symbols" rule (cf. Vulture's own ``--ignore-decorators``). Only genuine
# *registration* decorators belong here; plain WRAPPERS (``@dataclass``, ``@lru_cache``,
# ``@property``, ``@requires_garth_session``, ...) must NOT be listed — the wrapped symbol is
# still referenced normally and real dead code among them must stay visible.
VULTURE_IGNORE_DECORATORS: tuple[str, ...] = (
    "@server.tool",  # MCP server tool registration (modules/integrations/research/server.py)
    "@tool",  # LangChain @tool registration
    "@router.*",  # FastAPI/MCP route registration
    "@app.*",  # FastAPI/MCP app route + lifecycle registration
    "@cl.*",  # Chainlit message / lifecycle / action callbacks
)


def vulture_ignore_decorators_arg() -> str:
    """Render :data:`VULTURE_IGNORE_DECORATORS` as the comma-joined ``--ignore-decorators`` value."""
    return ",".join(VULTURE_IGNORE_DECORATORS)


# --- Mutation gate (Tier 5) self-targets ---

# The dead-code module mutation-tests ITSELF when its source changes; any file under this
# directory is a valid mutation target (generalised from the former single-file hardcode so
# the whole package is self-protected, not just one module).
SELF_TARGET_DIR = Path("scripts/deadcode")

# Non-``src`` runtime scripts that are mutation targets in addition to the package itself.
EXTRA_MUTATION_TARGETS: frozenset[Path] = frozenset({Path("scripts/evals/monorepo_eval.py")})


def is_self_target(path: Path) -> bool:
    """True for a file inside the dead-code module (mutation self-target)."""
    return path.as_posix().startswith(f"{SELF_TARGET_DIR.as_posix()}/")


def is_mutation_target(path: Path) -> bool:
    """True for changed files the mutation gate should mutate (runtime code only)."""
    from deadcode_audit import diffscope
    roots = diffscope.project_paths("mutation_roots", diffscope.source_roots())
    return any(path == root or path.is_relative_to(root) for root in roots) and not diffscope.is_test_path(path)



# --- `.deadcode.yml` scan configuration (per-rule severity, weights, thresholds, gate) ---

DEADCODE_CONFIG_PATH = Path(".deadcode.yml")
_VALID_SEVERITIES = {"off", "error", "warning", "info"}
_VALID_TOP_KEYS = {"rules", "scoring", "ci", "exclude", "extends", "allowed_cycles", "project"}
_MAX_EXTENDS_DEPTH = 5


@dataclass(frozen=True)
class DeadcodeConfig:
    """Parsed ``.deadcode.yml`` (all fields optional; absent file -> all defaults)."""

    project: dict[str, object] = field(default_factory=dict)
    rule_severity: dict[str, str] = field(default_factory=dict)  # rule id -> off|error|warning|info
    weights: dict[str, float] = field(default_factory=dict)
    smoothing: int | None = None
    fail_below: int | None = None
    good_threshold: int | None = None
    ok_threshold: int | None = None
    exclude: tuple[str, ...] = ()
    allowed_cycles: tuple[str, ...] = ()  # import cycles deliberately accepted, by "<a> <-> <b>" key


def _require(condition: bool, message: str) -> None:
    """Fail-closed validation: raise ``ValueError`` rather than silently defaulting on bad config."""
    if not condition:
        raise ValueError(f"invalid .deadcode.yml: {message}")


def _parse_config(raw: object, base_dir: Path, depth: int) -> DeadcodeConfig:
    _require(isinstance(raw, dict), "top level must be a mapping")
    assert isinstance(raw, dict)
    unknown = set(raw) - _VALID_TOP_KEYS
    _require(not unknown, f"unknown top-level keys {sorted(unknown)}")

    parent = DeadcodeConfig()
    extends = raw.get("extends")
    if extends is not None:
        _require(depth < _MAX_EXTENDS_DEPTH, "extends nesting too deep (cycle?)")
        targets = [extends] if isinstance(extends, str) else extends
        _require(
            isinstance(targets, list) and all(isinstance(t, str) for t in targets),
            "extends must be str or list[str]",
        )
        for target in targets:
            parent_path = (base_dir / target).resolve()
            _require(parent_path.exists(), f"extends target not found: {target}")
            parent = _merge(
                parent,
                _parse_config(
                    yaml.safe_load(parent_path.read_text(encoding="utf-8")),
                    parent_path.parent,
                    depth + 1,
                ),
            )

    rules = raw.get("rules", {})
    _require(isinstance(rules, dict), "rules must be a mapping of rule-id -> severity")
    # YAML 1.1 parses unquoted ``off`` as boolean False; accept that as the "off" severity so
    # ``rule: off`` works without forcing the user to quote it (aislop quotes it; we tolerate both).
    rules = {rule_id: ("off" if sev is False else sev) for rule_id, sev in rules.items()}
    for rule_id, sev in rules.items():
        _require(
            isinstance(sev, str) and sev in _VALID_SEVERITIES,
            f"rule '{rule_id}' severity must be one of {sorted(_VALID_SEVERITIES)} (quote it if it is 'off'/'on')",
        )

    scoring = raw.get("scoring", {})
    _require(isinstance(scoring, dict), "scoring must be a mapping")
    unknown_scoring = set(scoring) - {"weights", "smoothing", "thresholds"}
    _require(not unknown_scoring, f"unknown scoring keys {sorted(unknown_scoring)}")
    weights = scoring.get("weights", {})
    _require(
        isinstance(weights, dict)
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in weights.values()),
        "scoring.weights must map engine -> number",
    )
    smoothing = scoring.get("smoothing")
    _require(
        smoothing is None or (isinstance(smoothing, int) and not isinstance(smoothing, bool)),
        "scoring.smoothing must be an int (unquoted YAML booleans like ``on`` are rejected)",
    )
    thresholds = scoring.get("thresholds", {})
    _require(isinstance(thresholds, dict), "scoring.thresholds must be a mapping")
    unknown_thresholds = set(thresholds) - {"good", "ok"}
    _require(not unknown_thresholds, f"unknown scoring.thresholds keys {sorted(unknown_thresholds)}")
    good = thresholds.get("good")
    ok = thresholds.get("ok")
    _require(
        good is None or (isinstance(good, int) and not isinstance(good, bool)),
        "scoring.thresholds.good must be an int",
    )
    _require(
        ok is None or (isinstance(ok, int) and not isinstance(ok, bool)),
        "scoring.thresholds.ok must be an int",
    )
    _require(good is None or ok is None or good >= ok, "scoring.thresholds.good must be >= scoring.thresholds.ok")

    ci = raw.get("ci", {})
    _require(isinstance(ci, dict), "ci must be a mapping")
    unknown_ci = set(ci) - {"failBelow"}
    _require(not unknown_ci, f"unknown ci keys {sorted(unknown_ci)}")
    fail_below = ci.get("failBelow")
    _require(
        fail_below is None or (isinstance(fail_below, int) and not isinstance(fail_below, bool)),
        "ci.failBelow must be an int",
    )

    exclude = raw.get("exclude", [])
    _require(
        isinstance(exclude, list) and all(isinstance(e, str) for e in exclude),
        "exclude must be a list of glob strings",
    )

    allowed_cycles = raw.get("allowed_cycles", [])
    _require(
        isinstance(allowed_cycles, list) and all(isinstance(c, str) for c in allowed_cycles),
        "allowed_cycles must be a list of '<a> <-> <b>' cycle-key strings",
    )

    project = raw.get("project", {})
    _require(isinstance(project, dict), "project must be a mapping")
    supported = {"source_roots", "import_roots", "test_roots", "mutation_roots", "mutation_also_copy", "mutation_test_map", "vulture_whitelist"}
    _require(not (set(project) - supported), "unknown project settings")
    for key, value in project.items():
        if key == "mutation_test_map":
            _require(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v) for k, v in value.items()), "mutation_test_map must map source prefixes to test paths")
            paths = [*value, *(x for tests in value.values() for x in tests)]
            _require(all(x and not Path(x).is_absolute() and ".." not in Path(x).parts for x in paths), "mutation_test_map paths must stay inside target checkout")
        else:
            _require(isinstance(value, list) and all(isinstance(x, str) and x for x in value), f"project.{key} must be list[str]")
            _require(all(not Path(x).is_absolute() and ".." not in Path(x).parts for x in value), f"project.{key} must stay inside target checkout")
            if key in {"source_roots", "import_roots", "test_roots", "mutation_roots"}:
                _require(value, f"project.{key} must not be empty — omit the key to use the built-in defaults")
    own = DeadcodeConfig(
        project=project,
        rule_severity={str(k): v for k, v in rules.items()},
        weights={str(k): float(v) for k, v in weights.items()},
        smoothing=smoothing,
        fail_below=fail_below,
        good_threshold=thresholds.get("good"),
        ok_threshold=thresholds.get("ok"),
        exclude=tuple(exclude),
        allowed_cycles=tuple(allowed_cycles),
    )
    return _merge(parent, own)


def _merge(parent: DeadcodeConfig, child: DeadcodeConfig) -> DeadcodeConfig:
    """Child wins on scalars; ``rule_severity``/``weights`` merge per key; ``project`` sections are
    replaced by the child's version (a shallow replace, not a recursive merge); ``exclude`` and
    ``allowed_cycles`` lists concatenate."""
    return DeadcodeConfig(
        project={**parent.project, **child.project},
        rule_severity={**parent.rule_severity, **child.rule_severity},
        weights={**parent.weights, **child.weights},
        smoothing=child.smoothing if child.smoothing is not None else parent.smoothing,
        fail_below=(child.fail_below if child.fail_below is not None else parent.fail_below),
        good_threshold=(child.good_threshold if child.good_threshold is not None else parent.good_threshold),
        ok_threshold=(child.ok_threshold if child.ok_threshold is not None else parent.ok_threshold),
        exclude=(*parent.exclude, *child.exclude),
        allowed_cycles=(*parent.allowed_cycles, *child.allowed_cycles),
    )


# Memoisation for :func:`load_deadcode_config`: per-file callers (``is_test_path`` runs once per
# scanned file) would otherwise re-parse the YAML O(files) times. Keyed by resolved path, stamped
# with ``st_mtime_ns`` + ``st_size`` so any rewrite of the file busts the cache.
_config_cache: dict[Path, tuple[tuple[int, int], DeadcodeConfig]] = {}


def load_deadcode_config(repo_root: Path, path: Path | None = None) -> DeadcodeConfig:
    """Load ``.deadcode.yml`` (absent file -> defaults; malformed file -> raise, never silent default)."""
    cfg_path = (path or (repo_root / DEADCODE_CONFIG_PATH)).resolve()
    if not cfg_path.exists():
        return DeadcodeConfig()
    stat = cfg_path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _config_cache.get(cfg_path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    parsed = _parse_config(yaml.safe_load(cfg_path.read_text(encoding="utf-8")), cfg_path.parent, depth=0)
    _config_cache[cfg_path] = (stamp, parsed)
    return parsed


def apply_rule_severities(diagnostics: list[Diagnostic], rule_severity: dict[str, str]) -> list[Diagnostic]:
    """Drop ``off`` rules and rewrite severities per config — applied before scoring/output."""
    from dataclasses import replace

    result: list[Diagnostic] = []
    for diag in diagnostics:
        override = rule_severity.get(diag.rule)
        if override is None:
            result.append(diag)
            continue
        if override == "off":
            continue
        result.append(replace(diag, severity=Severity(override)))
    return result
