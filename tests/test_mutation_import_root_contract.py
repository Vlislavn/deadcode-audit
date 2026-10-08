"""Import-root configuration must not purge the installed native runner."""
import os
import subprocess
import sys


def test_repository_import_root_preserves_external_modules(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/project_source.py").write_text("value = 42\n")
    external = tmp_path / ".venv/lib/python/site-packages"
    external.mkdir(parents=True)
    (external / "external_source.py").write_text("value = 43\n")
    (tmp_path / ".deadcode.yml").write_text(
        "project:\n  source_roots: [lib]\n  import_roots: [lib, '.']\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from deadcode_audit import mutation
sys.path[:0] = ['lib', '.venv/lib/python/site-packages']
import project_source, external_source
mutation._purge_original_source_modules()
assert 'project_source' not in sys.modules
assert sys.modules['external_source'] is external_source
"""],
        cwd=tmp_path,
        env={**os.environ, "DEADCODE_REPO_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
