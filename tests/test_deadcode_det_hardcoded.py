"""Tests for the hard-coded URL / provider-id AI-slop detector.

Each rule gets >=3 positive cases (varied hosts/prefixes/identifiers, proving the rule encodes a
shape principle rather than one memorised snippet) and >=3 adversarial negatives (legitimate
near-misses that must stay silent). Every literal id used as a *positive* fixture is assembled by
concatenation so this test file does not itself become a leaked-secret tripwire, and so the
detector under test cannot match the test source by accident.
"""

from pathlib import Path

from deadcode_audit.detectors.hardcoded import detect
from deadcode_audit.framework import build_file_context

URL_RULE = "ai-slop/hardcoded-url"
ID_RULE = "ai-slop/hardcoded-id"


def _rules_fired(source: str, path: str = "src/x.py") -> set[str]:
    ctx = build_file_context(Path(path), source)
    return {d.rule for d in detect(ctx)}


# --- hardcoded-url: positives (varied hosts, varied usage sites) ------------------------------


def test_url_assigned_to_name_fires() -> None:
    source = "API_BASE = " + repr("https://api.payments-co.io/v2")
    assert URL_RULE in _rules_fired(source)


def test_url_passed_as_call_argument_fires() -> None:
    source = "client.connect(" + repr("https://billing.internal-corp.net/rpc") + ")"
    assert URL_RULE in _rules_fired(source)


def test_url_as_function_default_fires() -> None:
    source = "def fetch(endpoint=" + repr("http://search.acme-cluster.org/query") + "):\n    return endpoint\n"
    assert URL_RULE in _rules_fired(source)


# --- hardcoded-url: adversarial negatives -----------------------------------------------------


def test_url_localhost_does_not_fire() -> None:
    source = "DEV_URL = " + repr("http://localhost:8080/health")
    assert URL_RULE not in _rules_fired(source)


def test_url_loopback_and_local_host_does_not_fire() -> None:
    source = (
        "A = " + repr("http://127.0.0.1:5000/x") + "\n"
        "B = " + repr("https://printer.local/status") + "\n"
        "C = " + repr("http://0.0.0.0:9000/") + "\n"
    )
    assert URL_RULE not in _rules_fired(source)


def test_url_schema_namespace_and_example_hosts_do_not_fire() -> None:
    source = (
        "NS = " + repr("http://www.w3.org/2001/XMLSchema") + "\n"
        "SCHEMA = " + repr("https://json-schema.org/draft/2020-12/schema") + "\n"
        "DOC = " + repr("https://example.com/path") + "\n"
        "REPO = " + repr("https://github.com/owner/spec") + "\n"
        "SCH = " + repr("https://schemas.acme.org/thing.json") + "\n"
    )
    assert URL_RULE not in _rules_fired(source)


def test_url_in_module_docstring_does_not_fire() -> None:
    source = '"""See https://api.payments-co.io/v2 for the endpoint."""\nVALUE = 1\n'
    assert URL_RULE not in _rules_fired(source)


def test_url_in_test_file_does_not_fire() -> None:
    source = "MOCK = " + repr("https://api.payments-co.io/v2")
    assert URL_RULE not in _rules_fired(source, path="tests/unit/test_client.py")
    assert URL_RULE not in _rules_fired(source, path="src/pkg/tests/test_inner.py")


def test_url_generalisation_bare_scheme_does_not_fire() -> None:
    # A naive 'https://' substring match would trip here; a real host is required, so it must not.
    source = "PREFIX = " + repr("https://") + "\nSCHEME = " + repr("http://")
    assert URL_RULE not in _rules_fired(source)


# --- hardcoded-id: positives (UUID, varied vendor prefixes, hex/secret token) -----------------


def test_id_uuid_assigned_fires() -> None:
    uuid = "9f1c2d3e" + "-" + "4a5b" + "-" + "6c7d" + "-" + "8e9f" + "-" + "0a1b2c3d4e5f"
    source = "TENANT = " + repr(uuid)
    assert ID_RULE in _rules_fired(source)


def test_id_vendor_prefixed_token_fires() -> None:
    secret = "s" + "k_" + "live9aZ8bQ7cX6dW5eV4"
    project = "proj" + "_" + "9kLmN0pQ7rS2tU4vW6"
    source = "KEY = " + repr(secret) + "\nPROJECT = " + repr(project) + "\n"
    fired = _rules_fired(source)
    assert ID_RULE in fired


def test_id_long_hex_token_passed_as_arg_fires() -> None:
    token = "ab12cd34ef56" + "7890abcdef01" + "2345"  # 28 hex chars
    source = "authorize(" + repr(token) + ")"
    assert ID_RULE in _rules_fired(source)


# --- hardcoded-id: adversarial negatives ------------------------------------------------------


def test_id_all_zero_uuid_placeholder_does_not_fire() -> None:
    zeros = "00000000" + "-0000-0000-0000-" + "000000000000"
    source = "DEFAULT_ID = " + repr(zeros)
    assert ID_RULE not in _rules_fired(source)


def test_id_obvious_placeholders_do_not_fire() -> None:
    source = (
        "A = " + repr("xxxxxxxxxxxxxxxxxxxxxxxxxxxx") + "\n"
        "B = " + repr("<your-project-id>") + "\n"
        "C = " + repr("your_account_id_here_changeme") + "\n"
    )
    assert ID_RULE not in _rules_fired(source)


def test_id_short_ids_do_not_fire() -> None:
    short_prefixed = "org" + "_" + "abc"  # remainder too short
    short_hex = "deadbeef"  # only 8 hex chars
    source = "X = " + repr(short_prefixed) + "\nY = " + repr(short_hex) + "\n"
    assert ID_RULE not in _rules_fired(source)


def test_id_in_test_file_does_not_fire() -> None:
    uuid = "9f1c2d3e" + "-4a5b-6c7d-8e9f-" + "0a1b2c3d4e5f"
    source = "FIXTURE = " + repr(uuid)
    assert ID_RULE not in _rules_fired(source, path="tests/test_billing.py")


def test_id_generalisation_dotted_path_and_prose_do_not_fire() -> None:
    # A naive ">=24 chars, alphanumeric" match would catch a dotted module path or a long prose
    # string; the single-character-class / mixed-class and whitespace guards must keep these silent.
    dotted = "modules.core.services.billing.gateway.adapter"
    prose = "this is a perfectly ordinary sentence of words"
    snake = "a_very_long_snake_case_identifier_name_here"
    source = "P = " + repr(dotted) + "\nQ = " + repr(prose) + "\nR = " + repr(snake) + "\n"
    assert ID_RULE not in _rules_fired(source)


def test_id_long_camelcase_class_names_in_all_do_not_fire() -> None:
    # Regression (real-scan FP class): long CamelCase class names / symbols in __all__ are
    # identifiers, not opaque ids — they have letters only (no digit), so must stay silent.
    names = ["ObsidianGetRecentChangesArgs", "TelegramDatabaseLockedError", "register_all_handlers"]
    source = "__all__ = [\n" + "".join(f"    {n!r},\n" for n in names) + "]\n"
    assert ID_RULE not in _rules_fired(source)


def test_id_snake_case_label_with_version_suffix_does_not_fire() -> None:
    # Regression: a word-segmented snake_case label with an incidental digit (a version/dimension
    # suffix) is a readable identifier, not an opaque token — the no-underscore guard keeps it silent.
    source = (
        "scenario_id = " + repr("prioritization_backlog_v1") + "\n"
        "field = " + repr("wellness_epoch_spo2_data_dto_list") + "\n"
        "event = " + repr("phase_r5_consume_stream_cancelled") + "\n"
    )
    assert ID_RULE not in _rules_fired(source)


def test_id_inline_hex_hash_still_fires() -> None:
    # The guard must NOT spare a genuine unsegmented hex hash baked into source.
    digest = "eb06d4ab" + "fb49dc3eeb1aeb98" + "ae0f581e"  # 32 hex chars, no separators
    source = "DEFAULT_API_HASH = " + repr(digest) + "\n"
    assert ID_RULE in _rules_fired(source)
