"""Tests for the security detector (hardcoded-secret, eval, shell-injection).

Each rule gets >=3 varied positives (different identifiers/modules, proving the rule encodes a
principle and not one snippet) and >=3 adversarial negatives (legitimate near-misses). Trigger
tokens in test sources are assembled by concatenation where needed so the detector never
self-matches the test file content, and so the tests prove the recogniser, not a literal echo.
"""

from pathlib import Path

from deadcode_audit.detectors.security import detect, rules
from deadcode_audit.framework import build_file_context

SECRET = "security/hardcoded-secret"
EVAL = "security/eval"
SHELL = "security/shell-injection"

# Tokens reconstructed so they never appear whole in this file (mirrors the module's own guard).
_AKIA = "AK" + "IA" + "1234567890ABCDEF"  # AWS access-key id shape: prefix + 16 alnum
_SK = "sk" + "-" + "abcdEFGH1234ijklMNOP5678qrst"  # OpenAI-style key shape
_JWT = "ey" + "J" + "abc123.payLoad456.signature789"  # JWT three-segment shape


def _rule_ids(path: str, source: str) -> list[str]:
    """Build a FileContext and return the rule ids fired by the detector, in order."""
    ctx = build_file_context(Path(path), source)
    return [finding.rule for finding in detect(ctx)]


def test_rules_registry_covers_every_emitted_id() -> None:
    declared = {spec.rule for spec in rules}
    assert declared == {SECRET, EVAL, SHELL}


# --- security/hardcoded-secret : POSITIVES ----------------------------------------------------


def test_secret_named_target_with_real_value_fires() -> None:
    # "password" target carrying a non-trivial literal.
    source = "db_" + "password" + ' = "h7Gk2pLm9Qx"\n'
    assert _rule_ids("src/app/db.py", source).count(SECRET) == 1


def test_secret_apikey_attribute_target_fires() -> None:
    # attribute leaf "api_key" (different identifier shape than the first positive).
    source = "config." + "api_key" + ' = "Z9q3Wm1nB7vC0lK4"\n'
    assert _rule_ids("src/service/cfg.py", source).count(SECRET) == 1


def test_secret_known_aws_shape_in_plainly_named_var_fires() -> None:
    # Generic target name; flagged purely by the AWS-key SHAPE of the literal.
    source = "credential = " + repr(_AKIA) + "\n"
    assert _rule_ids("src/infra/aws.py", source).count(SECRET) == 1


def test_secret_openai_and_jwt_shapes_fire() -> None:
    src_openai = "client_key = " + repr(_SK) + "\n"
    src_jwt = "bearer = " + repr(_JWT) + "\n"
    assert SECRET in _rule_ids("src/llm/client.py", src_openai)
    assert SECRET in _rule_ids("src/auth/jwt.py", src_jwt)


def test_secret_github_slack_pem_shapes_fire_in_plainly_named_var() -> None:
    # Generic target names; flagged purely by the credential SHAPE (GitHub PAT, Slack token, PEM key).
    github = "value = " + repr("gh" + "p_" + "A" * 36) + "\n"
    slack = "value = " + repr("xo" + "xb-" + "1234567890ABCDEF") + "\n"
    pem = "value = " + repr("-----BEGIN OPENSSH PRIVATE KEY-----abc") + "\n"
    assert SECRET in _rule_ids("src/ci/tokens.py", github)
    assert SECRET in _rule_ids("src/ci/slack.py", slack)
    assert SECRET in _rule_ids("src/ci/keys.py", pem)


# --- security/hardcoded-secret : ADVERSARIAL NEGATIVES ----------------------------------------


def test_secret_env_lookup_not_flagged() -> None:
    # Reading from the environment is the CORRECT pattern, not a literal -> never flag.
    source = "import os\n" + "token" + ' = os.getenv("APP_' + "TOKEN" + '")\n'
    assert SECRET not in _rule_ids("src/app/auth.py", source)


def test_secret_placeholder_values_not_flagged() -> None:
    s1 = "password" + ' = "change" + "me"\n'  # filler word
    s2 = "secret" + '_key = "<your-' + "secret" + '-here>"\n'  # bracketed template
    s3 = "api_key" + ' = "your-' + "api" + '-key-here"\n'  # your-...-here stub
    assert SECRET not in _rule_ids("src/a.py", s1)
    assert SECRET not in _rule_ids("src/b.py", s2)
    assert SECRET not in _rule_ids("src/c.py", s3)


def test_secret_under_tests_dir_not_flagged() -> None:
    # Fixtures under tests/ legitimately hold fake credentials.
    source = "password" + ' = "h7Gk2pLm9Qx"\n'
    assert SECRET not in _rule_ids("tests/fixtures/creds.py", source)


def test_secret_empty_and_short_named_values_not_flagged() -> None:
    s_empty = "password" + ' = ""\n'
    s_short = "token" + ' = "abc"\n'  # below the entropy floor for a named secret
    assert SECRET not in _rule_ids("src/a.py", s_empty)
    assert SECRET not in _rule_ids("src/b.py", s_short)


def test_secret_generalisation_prose_substring_not_flagged() -> None:
    # A naive substring match would trip on the credential-shape prefix appearing inside a
    # longer ordinary word/sentence; the anchored shape regex must NOT fire here.
    not_a_key = "AK" + "IAlike words in a normal sentence about keys"
    source = "description = " + repr(not_a_key) + "\n"
    assert SECRET not in _rule_ids("src/docs/info.py", source)


# --- security/eval : POSITIVES ----------------------------------------------------------------


def test_eval_call_fires() -> None:
    source = "result = " + "ev" + "al(user_input)\n"
    assert _rule_ids("src/calc/run.py", source).count(EVAL) == 1


def test_exec_call_fires() -> None:
    source = "ex" + "ec(compiled_snippet)\n"
    assert _rule_ids("src/runner/dyn.py", source).count(EVAL) == 1


def test_eval_inside_function_fires() -> None:
    source = "def handle(expr):\n    return " + "ev" + "al(expr, {}, {})\n"
    assert EVAL in _rule_ids("src/handlers/expr.py", source)


# --- security/eval : ADVERSARIAL NEGATIVES ----------------------------------------------------


def test_literal_eval_not_flagged() -> None:
    # ast.literal_eval is a safe parser, not dynamic code execution.
    source = "import ast\n" + "value = ast.literal_" + "eval(text)\n"
    assert EVAL not in _rule_ids("src/parse/safe.py", source)


def test_method_named_eval_not_flagged() -> None:
    # model.eval() (e.g. torch) is an attribute call, not the builtin -> not flagged.
    source = "model." + "eval()\n"
    assert EVAL not in _rule_ids("src/ml/infer.py", source)


def test_local_variable_named_eval_not_flagged() -> None:
    # Assigning to a variable spelled like the builtin is not a call to it.
    source = "ev" + "al = compute_score()\n" + "print(ev" + "al)\n"
    assert EVAL not in _rule_ids("src/scoring/x.py", source)


def test_eval_generalisation_substring_method_not_flagged() -> None:
    # "evaluate" contains the trigger but is a different name -> naive substring would trip.
    source = "scorer." + "evaluate(dataset)\n"
    assert EVAL not in _rule_ids("src/eval/score.py", source)


# --- security/shell-injection : POSITIVES -----------------------------------------------------


def test_shell_injection_variable_command_fires() -> None:
    source = "import subprocess\n" + "subprocess.run(cmd, shell=True)\n"
    assert _rule_ids("src/ops/run.py", source).count(SHELL) == 1


def test_shell_injection_fstring_command_fires() -> None:
    source = "import subprocess\n" + 'subprocess.Popen(f"ls {path}", shell=True)\n'
    assert SHELL in _rule_ids("src/ops/list.py", source)


def test_shell_injection_concatenation_and_format_fire() -> None:
    src_concat = "import subprocess\n" + 'subprocess.check_output("git " + branch, shell=True)\n'
    src_format = "import subprocess\n" + 'subprocess.call("echo {}".format(msg), shell=True)\n'
    assert SHELL in _rule_ids("src/scm/git.py", src_concat)
    assert SHELL in _rule_ids("src/scm/echo.py", src_format)


def test_os_system_variable_command_fires() -> None:
    source = "import os\n" + "os.system(command)\n"
    assert _rule_ids("src/ops/sys.py", source).count(SHELL) == 1


# --- security/shell-injection : ADVERSARIAL NEGATIVES -----------------------------------------


def test_shell_true_literal_command_not_flagged() -> None:
    # Fully literal command with shell=True carries no injection surface for this rule.
    source = "import subprocess\n" + 'subprocess.run("ls -la /tmp", shell=True)\n'
    assert SHELL not in _rule_ids("src/ops/ls.py", source)


def test_argv_list_without_shell_not_flagged() -> None:
    # The safe pattern: an argv list with shell defaulting to False.
    source = "import subprocess\n" + "subprocess.run([binary, arg1, arg2])\n"
    assert SHELL not in _rule_ids("src/ops/argv.py", source)


def test_os_system_literal_command_not_flagged() -> None:
    source = "import os\n" + 'os.system("clear")\n'
    assert SHELL not in _rule_ids("src/ops/clear.py", source)


def test_shell_injection_generalisation_unrelated_run_not_flagged() -> None:
    # A user object with a .run(x, shell=True) method that is NOT subprocess+variable here:
    # literal command means no interpolation surface even though shell=True is present.
    source = 'subprocess.run("static command", shell=True)\n'
    assert SHELL not in _rule_ids("src/ops/static.py", source)


def test_subprocess_variable_command_without_shell_not_flagged() -> None:
    # Variable command but shell=False (default) -> argv is not re-parsed by a shell.
    source = "import subprocess\n" + "subprocess.run(cmd)\n"
    assert SHELL not in _rule_ids("src/ops/noshell.py", source)


def test_credential_named_field_with_label_value_is_not_a_secret() -> None:
    # Regression (real-scan FP class): a credential-NAMED constant whose value is a plain code
    # label (no digit, no special char) names something — it is NOT a hardcoded secret.
    source = (
        "EVENT_TOKEN_BUDGET_HIGH = " + repr("react_token_budget_high") + "\n"
        "TEXT_ID_OPENAI_API_KEY = " + repr("openai_api_key") + "\n"
        "EVENT_LLM_SPECIAL_TOKEN_STRIPPED = " + repr("llm_special_token_stripped") + "\n"
    )
    assert SECRET not in _rule_ids("src/modules/labels.py", source)


def test_credential_named_field_with_opaque_value_still_fires() -> None:
    # The label guard only spares identifier-shaped values; an opaque value (letters+digits) fires.
    opaque = "a1B2" + "c3D4e5F6g7H8i9J0klmn"
    source = "api_key = " + repr(opaque) + "\n"
    assert SECRET in _rule_ids("src/svc/conf.py", source)
