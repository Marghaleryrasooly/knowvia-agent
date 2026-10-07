from __future__ import annotations

import numpy as np
import pytest

from knowvia.embeddings import LocalEmbeddingProvider, OpenAIEmbeddingProvider


class FakeLocalEncoder:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        self.arguments = kwargs
        return np.asarray([[1.0, 2.0] for _ in texts])


def test_local_provider_uses_encoder_and_returns_plain_lists() -> None:
    encoder = FakeLocalEncoder()
    provider = LocalEmbeddingProvider("local-test-model", encoder=encoder)

    vectors = provider.embed(["first", "second"])

    assert provider.model_name == "local-test-model"
    assert vectors == [[1.0, 2.0], [1.0, 2.0]]
    assert encoder.arguments["normalize_embeddings"] is True
    assert encoder.arguments["show_progress_bar"] is False


def test_openai_provider_remains_available_with_injected_client() -> None:
    class Embedding:
        def __init__(self, index: int, vector: list[float]) -> None:
            self.index = index
            self.embedding = vector

    class Embeddings:
        def create(self, **_: object) -> object:
            return type("Response", (), {"data": [Embedding(0, [0.5, 0.5])]})()

    client = type("Client", (), {"embeddings": Embeddings()})()
    provider = OpenAIEmbeddingProvider("openai-test-model", client=client)

    assert provider.embed(["question"]) == [[0.5, 0.5]]
    assert provider.model_name == "openai-test-model"


def test_local_provider_prefers_cached_model(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeSentenceTransformer:
        def __init__(self, _: str, **kwargs: object) -> None:
            calls.append(kwargs)

        def encode(self, texts: list[str], **_: object) -> np.ndarray:
            return np.asarray([[1.0] for _ in texts])

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", FakeSentenceTransformer)
    LocalEmbeddingProvider("cached-model")

    assert calls == [{"local_files_only": True}]


@pytest.mark.local_model
def test_real_default_local_model_creates_384_dimension_vectors() -> None:
    provider = LocalEmbeddingProvider()

    vectors = provider.embed(["vacation policy", "remote work policy"])

    assert len(vectors) == 2
    assert all(len(vector) == 384 for vector in vectors)
