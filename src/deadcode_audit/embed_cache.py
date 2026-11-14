"""Persistent per-function embedding vectors (SQLite), keyed by exact source and encoder identity.

Only vectors are stored, never source text. A cache failure (unwritable path, corrupt file or row) degrades to
recomputing with the real encoder, with one stderr warning per process; encoder errors always propagate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_CHUNK = 500  # stay below SQLite's bound-variable limit
_warned = False


def _db_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "deadcode-audit" / "embeddings.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors (identity TEXT NOT NULL, source TEXT NOT NULL, "
        "vec BLOB NOT NULL, PRIMARY KEY (identity, source))"
    )
    return conn


def _warn(exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"warning: embedding cache unavailable ({exc}); computing all vectors", file=sys.stderr)


def _valid(rows: np.ndarray) -> bool:
    """Finite, unit-norm rows: what the encoder promises, and what a torn or foreign row would not satisfy."""
    return bool(np.isfinite(rows).all() and np.allclose(np.linalg.norm(rows, axis=1), 1.0, atol=1e-3))


def _lookup(identity: str, hashes: Sequence[str], dim: int) -> dict[str, np.ndarray] | None:
    """Valid cached vectors by source hash; ``None`` when the cache itself is unusable."""
    found: dict[str, np.ndarray] = {}
    try:
        with closing(_connect()) as conn:
            for start in range(0, len(hashes), _CHUNK):
                part = list(hashes[start : start + _CHUNK])
                marks = ",".join("?" * len(part))
                query = f"SELECT source, vec FROM vectors WHERE identity = ? AND source IN ({marks})"
                for source, blob in conn.execute(query, [identity, *part]):
                    if isinstance(blob, bytes) and len(blob) == dim * 4:
                        vec = np.frombuffer(blob, dtype=np.float32).copy()
                        if _valid(vec[None, :]):
                            found[source] = vec
    except (sqlite3.Error, OSError) as exc:
        _warn(exc)
        return None
    return found


def _store(identity: str, vectors: dict[str, np.ndarray]) -> None:
    rows = [(identity, source, vec.tobytes()) for source, vec in vectors.items()]
    try:
        with closing(_connect()) as conn, conn:  # one short atomic transaction, after inference
            conn.executemany("INSERT OR REPLACE INTO vectors (identity, source, vec) VALUES (?, ?, ?)", rows)
    except (sqlite3.Error, OSError) as exc:
        _warn(exc)


def cached(
    embed: Callable[[list[str]], np.ndarray], identity: dict[str, Any] | None, dim: int
) -> Callable[[list[str]], np.ndarray]:
    """Wrap ``embed`` so unchanged sources are read from the cache and only misses are encoded.

    ``identity`` must pin everything that can change a vector; ``None`` means it cannot be pinned, so the
    encoder is returned unwrapped and nothing is persisted.
    """
    if identity is None:
        return embed
    key = hashlib.sha256(json.dumps({**identity, "dim": dim}, sort_keys=True).encode()).hexdigest()

    def embed_cached(sources: list[str]) -> np.ndarray:
        if not sources:
            return embed(sources)
        hashes = [hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest() for s in sources]
        text = dict(zip(hashes, sources, strict=True))  # equal sources share one hash: encode once
        found = _lookup(key, list(text), dim)
        vectors = dict(found or {})
        missing = [h for h in text if h not in vectors]
        if missing:
            computed = np.asarray(embed([text[h] for h in missing]), dtype=np.float32)
            if computed.shape != (len(missing), dim):
                raise ValueError(f"encoder returned shape {computed.shape}, expected {(len(missing), dim)}")
            fresh = dict(zip(missing, computed, strict=True))
            vectors.update(fresh)
            if found is not None:
                _store(key, {h: v for h, v in fresh.items() if _valid(v[None, :])})
        return np.stack([vectors[h] for h in hashes])

    return embed_cached
