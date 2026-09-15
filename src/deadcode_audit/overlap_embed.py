"""Lazy code embeddings. Requested model failures are errors, never silent fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

_MODEL: tuple[str, Any, Any] | None = None
_BATCH = 4
_MAX_TOKENS = 512


def get_embedder(model_id: str) -> Callable[[list[str]], np.ndarray]:
    """Load the requested model on CPU; propagate dependency/model-load errors."""
    global _MODEL
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(2)
    if _MODEL is None or _MODEL[0] != model_id:
        _MODEL = None
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=False)
        model = AutoModel.from_pretrained(model_id, trust_remote_code=False).eval()
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

    return embed


def model_metadata() -> dict[str, Any]:
    if _MODEL is None:
        raise RuntimeError("No embedding model loaded")
    model_id, _, model = _MODEL
    return {"model": model_id, "revision": model.config._commit_hash,
            "device": "cpu", "max_tokens": _MAX_TOKENS,
            "pooling": "attention-masked mean, L2 normalization"}
