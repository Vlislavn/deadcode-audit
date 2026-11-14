"""Persistent function-vector cache: unchanged sources are not re-encoded (fake encoder, no weights)."""

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from deadcode_audit import embed_cache, overlap_embed

DIM = 4
COMMIT = "a" * 40
IDENTITY = {"model": "m", "model_commit": COMMIT, "max_tokens": 512, "algo": 1}


class Encoder:
    """Deterministic unit vectors from the text; records every text it is asked to encode."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, sources: list[str]) -> np.ndarray:
        self.seen.extend(sources)
        rows = []
        for text in sources:
            raw = np.frombuffer(hashlib.sha256(text.encode()).digest()[:DIM], dtype=np.uint8).astype(np.float32) + 1
            rows.append(raw / np.linalg.norm(raw))
        return np.array(rows, dtype=np.float32)


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(embed_cache, "_warned", False)
    return tmp_path / "cache" / "deadcode-audit" / "embeddings.sqlite3"


def test_cold_then_warm_fresh_wrapper_encodes_nothing() -> None:
    first = Encoder()
    cold = embed_cache.cached(first, IDENTITY, DIM)(["def a(): pass", "def b(): pass"])
    assert first.seen == ["def a(): pass", "def b(): pass"]
    second = Encoder()
    warm = embed_cache.cached(second, IDENTITY, DIM)(["def a(): pass", "def b(): pass"])
    assert second.seen == []
    assert np.array_equal(cold, warm)


def test_changed_and_added_sources_encode_only_those() -> None:
    embed_cache.cached(Encoder(), IDENTITY, DIM)(["a", "b"])
    changed = Encoder()
    embed_cache.cached(changed, IDENTITY, DIM)(["a", "B"])
    assert changed.seen == ["B"]
    added = Encoder()
    embed_cache.cached(added, IDENTITY, DIM)(["a", "B", "c"])
    assert added.seen == ["c"]


def test_duplicates_are_encoded_once_and_order_is_preserved() -> None:
    reference = Encoder()(["x", "y", "z"])
    encoder = Encoder()
    out = embed_cache.cached(encoder, IDENTITY, DIM)(["y", "x", "y", "z", "x"])
    assert sorted(encoder.seen) == ["x", "y", "z"]
    assert np.array_equal(out, reference[[1, 0, 1, 2, 0]])
    warm = embed_cache.cached(Encoder(), IDENTITY, DIM)(["z", "y", "x", "x"])
    assert np.array_equal(warm, reference[[2, 1, 0, 0]])


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "other"},
        {"model_commit": "b" * 40},
        {"max_tokens": 256},
        {"algo": 2},
    ],
)
def test_model_revision_or_processing_change_is_a_miss(changed: dict[str, object]) -> None:
    embed_cache.cached(Encoder(), IDENTITY, DIM)(["a"])
    encoder = Encoder()
    embed_cache.cached(encoder, {**IDENTITY, **changed}, DIM)(["a"])
    assert encoder.seen == ["a"]


def test_dimension_change_is_a_miss() -> None:
    embed_cache.cached(Encoder(), IDENTITY, DIM)(["a"])
    encoder = Encoder()
    with pytest.raises(ValueError, match="shape"):
        embed_cache.cached(encoder, IDENTITY, DIM + 1)(["a"])
    assert encoder.seen == ["a"]


def test_unpinnable_identity_bypasses_the_cache(cache_home: Path) -> None:
    encoder = Encoder()
    out = embed_cache.cached(encoder, None, DIM)(["a"])
    embed_cache.cached(encoder, None, DIM)(["a"])
    assert encoder.seen == ["a", "a"]
    assert out.shape == (1, DIM)
    assert not cache_home.exists()


def test_invalid_stored_vector_is_recomputed_not_reused(cache_home: Path) -> None:
    embed_cache.cached(Encoder(), IDENTITY, DIM)(["a", "b"])
    with sqlite3.connect(cache_home) as conn:
        conn.execute("UPDATE vectors SET vec = ?", (b"\x00" * 3,))  # truncated row
    encoder = Encoder()
    out = embed_cache.cached(encoder, IDENTITY, DIM)(["a", "b"])
    assert encoder.seen == ["a", "b"]
    assert np.array_equal(out, Encoder()(["a", "b"]))
    with sqlite3.connect(cache_home) as conn:
        conn.execute("UPDATE vectors SET vec = ?", (np.full(DIM, np.nan, dtype=np.float32).tobytes(),))
    again = Encoder()
    assert np.array_equal(embed_cache.cached(again, IDENTITY, DIM)(["a", "b"]), out)
    assert again.seen == ["a", "b"]


def test_corrupt_database_file_computes_for_real_and_warns_once(
    cache_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_home.parent.mkdir(parents=True)
    cache_home.write_bytes(b"this is not an sqlite database" * 100)
    encoder = Encoder()
    embed = embed_cache.cached(encoder, IDENTITY, DIM)
    assert np.array_equal(embed(["a"]), Encoder()(["a"]))
    embed(["a"])
    assert encoder.seen == ["a", "a"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("embedding cache unavailable") == 1


def test_unwritable_cache_path_computes_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    encoder = Encoder()
    assert np.array_equal(embed_cache.cached(encoder, IDENTITY, DIM)(["a", "b"]), Encoder()(["a", "b"]))
    assert encoder.seen == ["a", "b"]
    assert "embedding cache unavailable" in capsys.readouterr().err


def test_encoder_errors_are_not_hidden() -> None:
    def broken(_sources: list[str]) -> np.ndarray:
        raise RuntimeError("model exploded")

    with pytest.raises(RuntimeError, match="model exploded"):
        embed_cache.cached(broken, IDENTITY, DIM)(["a"])


def test_database_holds_vectors_but_no_source_text(cache_home: Path) -> None:
    secret = "def api_key(): return 'TOP-SECRET-LITERAL'"
    embed_cache.cached(Encoder(), IDENTITY, DIM)([secret])
    assert b"TOP-SECRET-LITERAL" not in cache_home.read_bytes()
    with sqlite3.connect(cache_home) as conn:
        assert conn.execute("SELECT count(*), length(vec) FROM vectors").fetchone() == (1, DIM * 4)


# --- identity from a loaded model/tokenizer (fake objects, no weights) ---


def _loaded(model_commit: str | None = COMMIT, tokenizer_commit: str | None = COMMIT):
    config = SimpleNamespace(_commit_hash=model_commit, to_json_string=lambda: "{}", hidden_size=DIM)
    tokenizer = SimpleNamespace(init_kwargs={"_commit_hash": tokenizer_commit}, eos_token="</s>", eos_token_id=2)
    return tokenizer, SimpleNamespace(config=config)


def test_identity_for_resolved_commits_and_changes_with_them() -> None:
    base = overlap_embed._identity("org/m", None, *_loaded())
    assert base is not None
    assert overlap_embed._identity("org/m", None, *_loaded(model_commit="b" * 40)) != base
    assert overlap_embed._identity("org/m", None, *_loaded(tokenizer_commit="b" * 40)) != base
    assert overlap_embed._identity("org/other", None, *_loaded()) != base


@pytest.mark.parametrize(
    ("revision", "model_commit", "tokenizer_commit"),
    [
        (None, None, COMMIT),
        (None, "main", COMMIT),
        (None, COMMIT, None),
        (None, COMMIT, "main"),
        (COMMIT, "b" * 40, COMMIT),  # pinned revision differs from what actually loaded
        (COMMIT, None, COMMIT),
    ],
)
def test_identity_bypasses_when_not_immutably_resolved(
    revision: str | None, model_commit: str | None, tokenizer_commit: str | None
) -> None:
    assert overlap_embed._identity("org/m", revision, *_loaded(model_commit, tokenizer_commit)) is None


def test_pinned_revision_matching_loaded_model_is_cacheable() -> None:
    assert overlap_embed._identity("org/m", COMMIT, *_loaded(tokenizer_commit=None)) is not None
