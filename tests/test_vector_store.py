from dataclasses import replace
from pathlib import Path

import pytest

from knowvia.models import Chunk
from knowvia.vector_store import JsonVectorStore


def test_json_store_persists_metadata_and_ranks_cosine_similarity(tmp_path: Path) -> None:
    store = JsonVectorStore(tmp_path / "index.json")
    chunks = [
        Chunk("a", "a.md", "A", "Leave", "Vacation", [1.0, 0.0]),
        Chunk("b", "b.md", "B", "VPN", "Network", [0.0, 1.0]),
    ]
    store.replace_all(chunks, "test-model")

    results = store.search([0.9, 0.1], top_k=1)

    assert store.embedding_model == "test-model"
    assert results[0].chunk.id == "a"
    assert results[0].score == pytest.approx(0.9938837)


def test_store_rejects_chunks_without_embeddings(tmp_path: Path) -> None:
    store = JsonVectorStore(tmp_path / "index.json")
    chunk = Chunk("a", "a.md", "A", "Section", "Text")

    with pytest.raises(ValueError, match="must have embeddings"):
        store.replace_all([replace(chunk)], "test-model")

