"""Run real dead-code CLI owners against a bounded filesystem corpus."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from coverage import Coverage, CoverageData

from fixtures.native_process import native_child_environment


def run_deadcode_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Execute the current real CLI owner against a bounded source corpus."""
    from importlib.util import find_spec

    owner = Path(find_spec("deadcode_audit.cli").origin).resolve().parent
    active = Coverage.current()
    with (
        TemporaryDirectory(prefix="ivai-deadcode-coverage-") as directory,
        native_child_environment(
            {
                "PATH": os.defpath,
                "PYTHONPATH": os.pathsep.join(sys.path),
                "DEADCODE_REPO_ROOT": str(root),
                "PYTHON_DOTENV_DISABLED": "1",
                "OPIK_DISABLED": "1",
            }
        ) as environment,
    ):
        data_file = Path(directory) / "coverage"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from fixtures.native_process import bootstrap_native_child
bootstrap_native_child()
import sys
from pathlib import Path
from coverage import Coverage

coverage = Coverage(data_file=sys.argv[1], source=[sys.argv[2]], config_file=False, branch=sys.argv[3] == '1')
coverage.start()
try:
    from deadcode_audit import cli
    if Path(cli.__file__).resolve() != Path(sys.argv[2]) / 'cli.py':
        raise RuntimeError('Dead-code subprocess imported a different CLI owner')
    status = cli.main(sys.argv[4:])
finally:
    coverage.stop()
    coverage.save()
sys.exit(status)
""",
                str(data_file),
                str(owner),
                str(int(active is not None and active.config.branch)),
                *arguments,
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if active is not None:
            data = CoverageData(basename=str(data_file))
            data.read()
            measured = data.measured_files()
            if not data.lines(str(owner / "cli.py")) or any(
                not Path(filename).is_absolute() or not Path(filename).resolve().is_relative_to(owner)
                for filename in measured
            ):
                raise ValueError("Dead-code child coverage must measure the actual CLI owner")
            if data.has_arcs() != active.config.branch:
                raise ValueError("Dead-code child coverage must preserve the parent's branch mode")
            parent_data = active.get_data()
            if data.has_arcs():
                parent_data.add_arcs({filename: data.arcs(filename) or [] for filename in measured})
            else:
                parent_data.add_lines({filename: data.lines(filename) or [] for filename in measured})
        return result
