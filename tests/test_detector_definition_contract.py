"""A duplicate detector definition can silently replace its implementation."""
import ast
from pathlib import Path

from deadcode_audit import detectors


def test_detector_modules_do_not_redefine_top_level_functions():
    root = Path(detectors.__file__).parent
    duplicates = {}
    for path in sorted(root.glob("*.py")):
        names = [
            node.name for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            duplicates[path.name] = repeated
    assert duplicates == {}
