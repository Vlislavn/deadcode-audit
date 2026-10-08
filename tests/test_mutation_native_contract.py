"""Native mutmut configuration and source-import isolation contracts (no mocks)."""
import os
import subprocess
import sys

import pytest

pytest.importorskip("mutmut.configuration")


def run_contract(root, code):
    return subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env=dict(os.environ, DEADCODE_REPO_ROOT=str(root)),
        capture_output=True, text=True, timeout=20,
    )


def test_native_config_selects_exact_files_and_broad_copy_roots(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/example.py").write_text("def answer():\n    return 42\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths=["src"]\n')
    (tmp_path / ".deadcode.yml").write_text(
        'project:\n  source_roots: [src]\n  mutation_roots: [src]\n'
        '  test_roots: [tests]\n  mutation_also_copy: [tests]\n'
    )
    result = run_contract(tmp_path, '''
from pathlib import Path
from deadcode_audit.mutation import _configure_mutation_for_paths
from mutmut.configuration import Config
c = _configure_mutation_for_paths([Path("src/example.py")])
assert c is Config.get()
assert c.source_paths == [Path("src")]
assert c.resolved_mutated_source_paths == [Path.cwd() / "mutants/src"]
assert c.only_mutate == ["src/example.py"]
assert Path("tests") in c.also_copy
assert c.pytest_add_cli_args_test_selection == ["tests"]
assert c.mutate_only_covered_lines
assert not c.use_setproctitle
assert c.should_mutate(Path("src/example.py"))
assert not c.should_mutate(Path("src/other.py"))
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_source_purge_preserves_native_entry_aliases(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/fixture_source.py").write_text("value = 42\n")
    (tmp_path / ".deadcode.yml").write_text(
        'project:\n  source_roots: [lib]\n  import_roots: [lib]\n'
    )
    result = run_contract(tmp_path, '''
import sys
import __main__
from deadcode_audit import mutation
sys.path.insert(0, "lib")
import fixture_source
sys.modules["__mp_main__"] = __main__
main = sys.modules["__main__"]
mutation._purge_original_source_modules()
assert "fixture_source" not in sys.modules
assert "deadcode_audit.mutation" not in sys.modules
assert sys.modules["__main__"] is main
assert sys.modules["__mp_main__"] is main
''')
    assert result.returncode == 0, result.stdout + result.stderr
