"""Tests for the swallowed-exception / redundant-try-catch AI-slop detectors.

Each rule gets >=3 positive cases (varied identifiers and call shapes, to prove the rule is
structural rather than a snippet match) and >=3 adversarial negatives (legitimate handlers that
must stay silent). One explicit "generalisation" negative per rule would trip a naive substring
matcher (it contains the words ``pass`` / ``raise`` / ``logger``) but must NOT fire because the
*structure* is real handling.
"""

from pathlib import Path

from deadcode_audit.detectors.exceptions import detect
from deadcode_audit.framework import build_file_context

SWALLOWED = "ai-slop/swallowed-exception"
REDUNDANT = "ai-slop/redundant-try-catch"


def _rule_ids(source: str) -> list[str]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.rule for d in detect(ctx)]


def _lines(source: str) -> list[int]:
    ctx = build_file_context(Path("src/x.py"), source)
    return [d.line for d in detect(ctx)]


# --------------------------------------------------------------------------------------------
# swallowed-exception — POSITIVE (must fire)
# --------------------------------------------------------------------------------------------


def test_swallow_bare_pass_fires() -> None:
    src = "def load():\n    try:\n        risky()\n    except ValueError:\n        pass\n"
    assert _rule_ids(src) == [SWALLOWED]


def test_swallow_logger_then_continue_fires() -> None:
    src = (
        "def sync(client):\n"
        "    try:\n"
        "        client.push()\n"
        "    except ConnectionError as exc:\n"
        "        logger.error('push failed: %s', exc)\n"
    )
    assert _rule_ids(src) == [SWALLOWED]


def test_swallow_logging_module_call_fires() -> None:
    src = "def parse(raw):\n    try:\n        decode(raw)\n    except KeyError:\n        logging.warning('bad key')\n"
    assert _rule_ids(src) == [SWALLOWED]


def test_swallow_print_call_fires() -> None:
    src = "def run():\n    try:\n        step()\n    except RuntimeError:\n        print('oops')\n"
    assert _rule_ids(src) == [SWALLOWED]


def test_swallow_warnings_warn_fires() -> None:
    src = (
        "def coerce(v):\n"
        "    try:\n"
        "        return int(v)\n"
        "    except TypeError:\n"
        "        warnings.warn('uncoercible value')\n"
    )
    assert _rule_ids(src) == [SWALLOWED]


def test_swallow_bare_except_pass_fires() -> None:
    src = "def teardown():\n    try:\n        close()\n    except:  # noqa: E722\n        pass\n"
    assert _rule_ids(src) == [SWALLOWED]


# --------------------------------------------------------------------------------------------
# swallowed-exception — ADVERSARIAL NEGATIVE (must NOT fire)
# --------------------------------------------------------------------------------------------


def test_handler_that_returns_fallback_is_not_swallow() -> None:
    src = "def lookup(key):\n    try:\n        return store[key]\n    except KeyError:\n        return None\n"
    assert _rule_ids(src) == []


def test_handler_that_assigns_fallback_is_not_swallow() -> None:
    src = (
        "def parse(raw):\n"
        "    try:\n"
        "        value = decode(raw)\n"
        "    except ValueError:\n"
        "        value = default_value()\n"
        "    return value\n"
    )
    assert _rule_ids(src) == []


def test_log_then_real_handling_is_not_swallow() -> None:
    # Two statements: a log call AND a recovery assignment -> real handling, must stay silent.
    src = (
        "def fetch(url):\n"
        "    try:\n"
        "        return get(url)\n"
        "    except TimeoutError as exc:\n"
        "        logger.warning('timeout: %s', exc)\n"
        "        return cached(url)\n"
    )
    assert _rule_ids(src) == []


def test_log_then_reraise_is_not_swallow() -> None:
    src = (
        "def commit(tx):\n"
        "    try:\n"
        "        tx.apply()\n"
        "    except DBError as exc:\n"
        "        logger.exception('commit failed')\n"
        "        raise\n"
    )
    assert _rule_ids(src) == []


def test_non_log_method_call_is_not_swallow() -> None:
    # A single call that is NOT a logging/print call (real recovery side effect) must not fire.
    src = (
        "def deliver(msg):\n"
        "    try:\n"
        "        primary.send(msg)\n"
        "    except QueueFull:\n"
        "        fallback.enqueue(msg)\n"
    )
    assert _rule_ids(src) == []


def test_generalisation_log_is_a_substring_of_a_real_handler_call() -> None:
    # A naive matcher keying on the word 'log' would trip on 'audit_log.persist(...)' — but that
    # is a side-effecting recovery call rooted at 'audit_log', not a bare logger root, so silent.
    src = (
        "def record(event):\n"
        "    try:\n"
        "        emit(event)\n"
        "    except BrokerError:\n"
        "        audit_log.persist(event)\n"
    )
    assert _rule_ids(src) == []


# --------------------------------------------------------------------------------------------
# redundant-try-catch — POSITIVE (must fire)
# --------------------------------------------------------------------------------------------


def test_bare_reraise_fires() -> None:
    src = "def open_it():\n    try:\n        do()\n    except OSError:\n        raise\n"
    assert _rule_ids(src) == [REDUNDANT]


def test_bare_reraise_with_alias_fires() -> None:
    src = "def step():\n    try:\n        work()\n    except ValueError as err:\n        raise\n"
    assert _rule_ids(src) == [REDUNDANT]


def test_bare_reraise_bare_except_fires() -> None:
    src = "def guard():\n    try:\n        run()\n    except:  # noqa: E722\n        raise\n"
    assert _rule_ids(src) == [REDUNDANT]


def test_bare_reraise_tuple_handler_fires() -> None:
    src = "def attempt():\n    try:\n        go()\n    except (KeyError, IndexError):\n        raise\n"
    assert _rule_ids(src) == [REDUNDANT]


# --------------------------------------------------------------------------------------------
# redundant-try-catch — ADVERSARIAL NEGATIVE (must NOT fire)
# --------------------------------------------------------------------------------------------


def test_wrapped_reraise_from_is_not_redundant() -> None:
    src = (
        "def parse(raw):\n"
        "    try:\n"
        "        return decode(raw)\n"
        "    except ValueError as exc:\n"
        "        raise ParseError('bad input') from exc\n"
    )
    assert _rule_ids(src) == []


def test_reraise_new_error_without_from_is_not_redundant() -> None:
    src = "def f():\n    try:\n        g()\n    except KeyError:\n        raise RuntimeError('missing')\n"
    assert _rule_ids(src) == []


def test_cleanup_then_bare_reraise_is_not_redundant() -> None:
    # Cleanup before the re-raise means the handler does real work -> length > 1 -> silent.
    src = (
        "def transaction(tx):\n"
        "    try:\n"
        "        tx.run()\n"
        "    except DBError:\n"
        "        tx.rollback()\n"
        "        raise\n"
    )
    assert _rule_ids(src) == []


def test_generalisation_raise_inside_nested_if_is_not_a_sole_bare_reraise() -> None:
    # Contains a bare 'raise' (naive matcher bait) but it is guarded by an 'if' that adds logic,
    # so the handler body is a single If, not a sole Raise -> must not fire either rule.
    src = (
        "def fetch(url, retries):\n"
        "    try:\n"
        "        return get(url)\n"
        "    except TimeoutError:\n"
        "        if retries <= 0:\n"
        "            raise\n"
        "        return fetch(url, retries - 1)\n"
    )
    assert _rule_ids(src) == []


# --------------------------------------------------------------------------------------------
# cross-rule / metadata
# --------------------------------------------------------------------------------------------


def test_multiple_handlers_each_flagged_at_their_own_line() -> None:
    src = (
        "def pipeline():\n"
        "    try:\n"
        "        a()\n"
        "    except KeyError:\n"  # line 4
        "        pass\n"
        "    try:\n"
        "        b()\n"
        "    except OSError:\n"  # line 8
        "        raise\n"
    )
    ids = _rule_ids(src)
    assert sorted(ids) == sorted([SWALLOWED, REDUNDANT])
    assert _lines(src) == [4, 8]


def test_clean_handler_emits_nothing() -> None:
    src = (
        "def safe(key):\n"
        "    try:\n"
        "        return store[key]\n"
        "    except KeyError as exc:\n"
        "        logger.info('miss')\n"
        "        return resolve(key, exc)\n"
    )
    assert _rule_ids(src) == []
