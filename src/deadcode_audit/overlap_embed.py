"""Lazy code embeddings. Requested model failures are errors, never silent fallback."""

from __future__ import annotations

import hashlib
import re
from importlib import metadata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

# trust_remote_code runs repository code: allowed ONLY for these ids, always at the pinned commit (q162).
_PINNED_REMOTE = {
    "codesage/codesage-small-v2": "4844c2f24b25e181aa43ca058cc73dd2622565c1",
    "jinaai/jina-embeddings-v2-base-code": "516f4baf13dec4ddddda8631e019b5737c8bc250",
}
_MODEL: tuple[str, Any, Any] | None = None
_BATCH = 4
_MAX_TOKENS = 512
_ALGO_VERSION = 1  # bump when pooling/normalisation/input preparation below changes
_COMMIT = re.compile(r"[0-9a-f]{40}")


def get_embedder(model_id: str) -> Callable[[list[str]], np.ndarray]:
    """Load the requested model on CPU; propagate dependency/model-load errors."""
    global _MODEL
    import torch
    from transformers import AutoModel, AutoTokenizer

    from deadcode_audit import embed_cache

    torch.set_num_threads(2)
    revision = _PINNED_REMOTE.get(model_id)
    if _MODEL is None or _MODEL[0] != model_id:
        _MODEL = None
        remote = revision is not None
        if model_id.startswith("codesage/"):  # pinned remote code predates transformers>=4.50 Conv1D move
            import transformers.modeling_utils as modeling_utils
            import transformers.pytorch_utils as pytorch_utils

            modeling_utils.Conv1D = pytorch_utils.Conv1D
        extra = {"add_eos_token": True} if model_id.startswith("codesage/") else {}
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=remote, revision=revision, **extra)
        model = AutoModel.from_pretrained(model_id, trust_remote_code=remote, revision=revision).eval()
        _MODEL = (model_id, tokenizer, model)
    _, tokenizer, model = _MODEL

    def embed(sources: list[str]) -> np.ndarray:
        import numpy as np
        import torch

        rows: list[np.ndarray] = []
        for start in range(0, len(sources), _BATCH):
            batch = [text or " " for text in sources[start : start + _BATCH]]  # empty -> " ": never a zero-token tensor
            encoded = tokenizer(batch, padding=True, truncation=True, max_length=_MAX_TOKENS, return_tensors="pt")
            with torch.no_grad():
                hidden = model(**encoded).last_hidden_state  # (batch, tokens, dim)
            mask = encoded["attention_mask"].unsqueeze(-1).type_as(hidden)
            summed = (hidden * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            normed = torch.nn.functional.normalize(summed / counts, p=2, dim=1)  # masked mean-pool + L2
            rows.append(normed.cpu().numpy())
        return np.vstack(rows) if rows else np.empty((0, model.config.hidden_size), dtype=np.float32)

    return embed_cache.cached(embed, _identity(model_id, revision, tokenizer, model), model.config.hidden_size)


def _identity(model_id: str, revision: str | None, tokenizer: Any, model: Any) -> dict[str, Any] | None:
    """Everything that can change a vector, or ``None`` when the model/tokenizer cannot be pinned to a commit."""
    model_commit = getattr(model.config, "_commit_hash", None)
    if revision is not None:  # pinned remote id: valid only if the loaded model really is that commit
        tokenizer_commit = revision if model_commit == revision else None
    else:
        tokenizer_commit = getattr(tokenizer, "init_kwargs", {}).get("_commit_hash")
    for commit in (model_commit, tokenizer_commit):
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            return None
    try:
        versions = {name: metadata.version(name) for name in ("torch", "transformers", "numpy")}
    except metadata.PackageNotFoundError:
        return None
    return {
        "algo": _ALGO_VERSION, "model": model_id, "model_commit": model_commit, "tokenizer_commit": tokenizer_commit,
        "tokenizer": [type(tokenizer).__name__, getattr(tokenizer, "eos_token", None),
                      getattr(tokenizer, "eos_token_id", None), getattr(tokenizer, "add_eos_token", None)],
        "config": hashlib.sha256(model.config.to_json_string().encode()).hexdigest(),
        "max_tokens": _MAX_TOKENS, "batch": _BATCH, "pooling": "attention-masked mean, L2 normalization",
        "libraries": versions,
    }


def model_metadata() -> dict[str, Any]:
    if _MODEL is None:
        raise RuntimeError("No embedding model loaded")
    model_id, _, model = _MODEL
    return {"model": model_id, "revision": model.config._commit_hash,
            "device": "cpu", "max_tokens": _MAX_TOKENS,
            "pooling": "attention-masked mean, L2 normalization"}
