"""Dynamic-import security parity regressions."""

from pathlib import Path

import pytest

from deadcode_audit.detectors import security
from deadcode_audit.framework import build_file_context, run_detectors

DYNAMIC_IMPORT = "security/dynamic-import"


def _rule_ids(path, source):
    return [
        finding.rule
        for finding in security.detect(build_file_context(Path(path), source))
    ]


@pytest.mark.parametrize(
    "source", ["importlib.import_module(module_name)", '__import__("trusted.module")']
)
def test_actual_dynamic_import_requires_call_site_authorization(source):
    assert _rule_ids("src/loader.py", source) == [DYNAMIC_IMPORT]


@pytest.mark.parametrize(
    "source",
    [
        '"""importlib.import_module(name); __import__(name)"""',
        "client.import_module(name)",
    ],
)
def test_prose_and_unrelated_method_are_not_dynamic_import_calls(source):
    assert _rule_ids("src/loader.py", source) == []


def test_dynamic_import_annotation_authorizes_only_its_specific_call():
    source = (
        "importlib.import_module(name)  # ai-slop: ignore[security/dynamic-import] - trusted plugin registry\n"
        "importlib.import_module(other)\n"
    )
    findings = run_detectors([(Path("src/loader.py"), source)], [security])
    assert [(item.rule, item.line) for item in findings] == [(DYNAMIC_IMPORT, 2)]
