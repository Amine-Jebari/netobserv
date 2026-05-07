"""Embedding-model contract tests.

These exercise the actual sentence-transformers model so they require network
egress (or a pre-warmed local cache) on first run. They are deliberately
narrow: the only behaviour we want to lock down is the embedding dimension
and that the vector is L2-finite. Anything beyond that is the model's
responsibility, not ours.
"""

from __future__ import annotations

import numpy as np
import pytest

from services.agent.config import Settings


pytest.importorskip("sentence_transformers")

from services.agent.embeddings import (  # noqa: E402  (after importorskip)
    embed_batch,
    embed_text,
    get_embedding_dim,
)


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EXPECTED_DIM = 384


def test_embedding_dimension_matches_settings() -> None:
    settings = Settings(embedding_model_name=MODEL_NAME, embedding_dim=EXPECTED_DIM)
    assert get_embedding_dim(settings.embedding_model_name) == settings.embedding_dim
    assert settings.embedding_dim == EXPECTED_DIM


def test_embed_text_returns_finite_vector_of_expected_dim() -> None:
    vec = embed_text("BGP session flapping repeatedly between peers", MODEL_NAME)
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (EXPECTED_DIM,)
    assert np.isfinite(vec).all()
    # Non-zero norm: the model must not return the zero vector for a real query.
    assert float(np.linalg.norm(vec)) > 0


def test_embed_batch_shape() -> None:
    queries = [
        "BGP flap on edge router",
        "DDoS volumetric attack mitigation",
        "MTU blackhole on tunnel",
    ]
    arr = embed_batch(queries, MODEL_NAME, batch_size=2)
    assert arr.shape == (3, EXPECTED_DIM)
    assert np.isfinite(arr).all()


def test_semantically_similar_queries_score_higher_than_unrelated() -> None:
    """Sanity: cosine sim of two BGP queries should beat a BGP-vs-DNS pair."""
    vecs = embed_batch(
        [
            "BGP peering session is flapping",
            "BGP neighbor going down repeatedly",
            "DNS resolver returning NXDOMAIN",
        ],
        MODEL_NAME,
    )

    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    bgp_pair = cosine(vecs[0], vecs[1])
    cross = cosine(vecs[0], vecs[2])
    assert bgp_pair > cross, (bgp_pair, cross)
