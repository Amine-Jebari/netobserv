"""Sentence-transformers wrapper used by both the seed script and rag.py.

The model is loaded lazily on first use. Hold a single instance per process to
avoid paying the load cost (and the GPU/CPU memory) more than once.
"""

from __future__ import annotations

from threading import Lock
from typing import Iterable

import numpy as np


# ── Module-level singleton ───────────────────────────────────────────────────
_MODEL = None
_MODEL_NAME: str | None = None
_LOCK = Lock()


def get_embedding_model(model_name: str):
    """Return a cached SentenceTransformer instance for ``model_name``.

    The first call loads the model from the Hugging Face hub (or local cache)
    and caches it for the lifetime of the process. Subsequent calls with the
    same name return the same instance; calling with a different name reloads.
    """
    global _MODEL, _MODEL_NAME

    with _LOCK:
        if _MODEL is None or _MODEL_NAME != model_name:
            # Imported lazily so unit tests for chunking and config do not pay
            # the import cost of sentence-transformers.
            from sentence_transformers import SentenceTransformer

            _MODEL = SentenceTransformer(model_name)
            _MODEL_NAME = model_name
        return _MODEL


def embed_text(text: str, model_name: str) -> np.ndarray:
    """Embed a single string and return a 1-D numpy array."""
    model = get_embedding_model(model_name)
    vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=False)
    return np.asarray(vec, dtype=np.float32)


def embed_batch(texts: Iterable[str], model_name: str, batch_size: int = 32) -> np.ndarray:
    """Embed a batch of strings; returns a 2-D numpy array shape (N, dim)."""
    model = get_embedding_model(model_name)
    vecs = model.encode(
        list(texts),
        batch_size=batch_size,
        convert_to_numpy=True,
        normalize_embeddings=False,
        show_progress_bar=False,
    )
    return np.asarray(vecs, dtype=np.float32)


def get_embedding_dim(model_name: str) -> int:
    """Return the embedding dimension reported by the model."""
    model = get_embedding_model(model_name)
    return int(model.get_sentence_embedding_dimension())
