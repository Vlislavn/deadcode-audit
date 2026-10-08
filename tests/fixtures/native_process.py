"""Test-only bridge for native mutmut instrumentation in fresh Python children."""

import atexit
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def native_child_environment(environment: dict[str, str]) -> Iterator[dict[str, str]]:
    """Retain a scrubbed environment and collect only actual native child hits."""
    root = Path.cwd().resolve()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError("Native child test context must be a copied project root")
    with TemporaryDirectory(prefix="ivai-native-child-") as directory:
        child_env = dict(environment)
        child_env.setdefault("HOME", directory)
        child_env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root), str(root / "tests"), child_env.get("PYTHONPATH", "")))
        selector = os.environ.get("MUTANT_UNDER_TEST")
        hits = Path(directory) / "hits.json"
        if selector is not None:
            child_env.update(
                MUTANT_UNDER_TEST=selector,
                IVAI_TEST_MUTATION_ROOT=str(root),
                IVAI_TEST_MUTATION_HITS=str(hits),
            )
        try:
            yield child_env
        finally:
            if selector == "stats" and sys.exc_info()[0] is None:
                data = json.loads(hits.read_text(encoding="utf-8"))
                if not isinstance(data, list) or not all(isinstance(hit, str) for hit in data):
                    raise ValueError("Native child mutation hits must be a list of function names")
                import mutmut

                mutmut._stats.update(data)


def bootstrap_native_child() -> None:
    """Load the installed owner's real config before copied application imports."""
    if "MUTANT_UNDER_TEST" not in os.environ:
        return
    from mutmut.configuration import Config

    root = Path(os.environ["IVAI_TEST_MUTATION_ROOT"])
    if Path(__file__).resolve().parents[2] != root:
        raise RuntimeError("Native child bootstrap imported a helper outside the copied project")
    previous = Path.cwd()
    try:
        os.chdir(root)
        Config.ensure_loaded()
    finally:
        os.chdir(previous)
    atexit.register(flush_native_child_hits)


def flush_native_child_hits() -> None:
    """Export native hits explicitly before the contracts' bounded hard exits."""
    if os.environ.get("MUTANT_UNDER_TEST") == "stats":
        import mutmut

        Path(os.environ["IVAI_TEST_MUTATION_HITS"]).write_text(json.dumps(sorted(mutmut._stats)), encoding="utf-8")
