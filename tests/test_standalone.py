"""Public CLI against real, non-monorepo checkout layouts."""
import json
import os
import subprocess
import sys


def run(root, *args):
    env = dict(os.environ, DEADCODE_REPO_ROOT=str(root))
    return subprocess.run([sys.executable, '-m', 'deadcode_audit', *args], cwd=root, env=env,
                          text=True, capture_output=True, timeout=20)


def project(tmp_path):
    source = tmp_path / 'plans/скрипты/Example/production'
    source.mkdir(parents=True)
    (tmp_path / '.deadcode.yml').write_text('project:\n  source_roots: ["plans/скрипты/Example/production"]\n  import_roots: ["plans/скрипты/Example/production"]\n')
    return source


def test_nested_target_layout_is_scanned(tmp_path):
    source = project(tmp_path)
    (source / 'example.py').write_text('def bad():\n    try:\n        risky()\n    except Exception:\n        pass\n')
    result = run(tmp_path, 'scan', '--json')
    assert result.returncode == 0, result.stderr
    assert 'example.py' in result.stdout
    result = run(tmp_path, 'reachability-scan', '--json')
    assert result.returncode == 0, result.stderr
    assert 'bad' in result.stdout


def test_nested_import_cycles_and_function_overlaps(tmp_path):
    source = project(tmp_path)
    (source / 'a.py').write_text('import b\ndef first(value):\n    total = value + 1\n    return total * 2\n')
    (source / 'b.py').write_text('import a\ndef second(value):\n    total = value + 1\n    return total * 2\n')
    cycle = run(tmp_path, 'cycles', '--json')
    assert 'a' in cycle.stdout and 'b' in cycle.stdout, cycle.stderr
    result = run(tmp_path, 'overlaps', '--no-embed', '--min-tokens', '1', '--threshold', '0', '--json')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['functions_scanned'] == 2
    assert data['embed_active'] is False
    assert data['count'] == 1


def test_missing_target_and_bad_config_are_errors(tmp_path):
    assert run(tmp_path, 'scan', 'missing', '--json').returncode != 0
    (tmp_path / '.deadcode.yml').write_text('project:\n  source_roots: ["../outside"]\n')
    assert run(tmp_path, 'scan', '--json').returncode != 0


def test_installed_vulture_runs_on_nested_sources(tmp_path):
    from deadcode_audit import diffscope, vulture_gate
    source = project(tmp_path)
    (source / 'unused.py').write_text('def unused_function():\n    return 42\n')
    previous = diffscope.REPO_ROOT
    try:
        diffscope.REPO_ROOT = tmp_path
        output = vulture_gate._run_vulture(60)
    finally:
        diffscope.REPO_ROOT = previous
    assert 'unused_function' in output


def test_bom_source_is_not_a_parse_failure(tmp_path):
    source = project(tmp_path)
    (source / "bom.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8-sig")
    result = run(tmp_path, "overlaps", "--no-embed", "--min-tokens", "1", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["functions_scanned"] == 1


def test_clean_file_reports_actual_coverage(tmp_path):
    source = project(tmp_path)
    (source / "clean.py").write_text("answer = 42\n")
    result = run(tmp_path, "scan", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 1
    assert payload["summary"]["files"] == 0  # files with findings, retained for compatibility
