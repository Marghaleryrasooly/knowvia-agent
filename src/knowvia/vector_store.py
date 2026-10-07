from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from knowvia.models import Chunk, RetrievalResult


class VectorStore(ABC):
    """Storage boundary used by the retrieval pipeline.

    A future pgvector implementation can implement this interface without
    changing parsing, chunking, embedding, indexing, or CLI orchestration.
    """

    @abstractmethod
    def replace_all(self, chunks: list[Chunk], embedding_model: str) -> None:
        """Replace the complete index with embedded chunks."""

    @abstractmethod
    def search(self, query_embedding: list[float], top_k: int) -> list[RetrievalResult]:
        """Return chunks ordered from most to least similar."""

    @property
    @abstractmethod
    def embedding_model(self) -> str | None:
        """Return the model used to build this store, if it is indexed."""


class JsonVectorStore(VectorStore):
    """Tiny, inspectable JSON vector store for Milestone 1."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def embedding_model(self) -> str | None:
        if not self.path.exists():
            return None
        return str(self._read()["embedding_model"])

    def replace_all(self, chunks: list[Chunk], embedding_model: str) -> None:
        if any(chunk.embedding is None for chunk in chunks):
            raise ValueError("All chunks must have embeddings before storage")
        payload = {
            "version": 1,
            "embedding_model": embedding_model,
            "chunks": [chunk.to_dict() for chunk in chunks],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def search(self, query_embedding: list[float], top_k: int) -> list[RetrievalResult]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if not self.path.exists():
            raise FileNotFoundError(
                f"Vector index not found at {self.path}. Run the index command first."
            )

        chunks = [Chunk.from_dict(value) for value in self._read()["chunks"]]
        if not chunks:
            return []
        matrix = np.asarray([chunk.embedding for chunk in chunks], dtype=np.float64)
        query = np.asarray(query_embedding, dtype=np.float64)
        if matrix.shape[1] != query.shape[0]:
            raise ValueError("Query and stored embeddings have different dimensions")

        matrix_norms = np.linalg.norm(matrix, axis=1)
        query_norm = np.linalg.norm(query)
        if query_norm == 0 or np.any(matrix_norms == 0):
            raise ValueError("Cannot compare zero-length embedding vectors")
        scores = (matrix @ query) / (matrix_norms * query_norm)
        ranked_indices = np.argsort(-scores, kind="stable")[:top_k]
        return [
            RetrievalResult(chunk=chunks[index], score=float(scores[index]))
            for index in ranked_indices
        ]

    def _read(self) -> dict[str, object]:
        return json.loads(self.path.read_text(encoding="utf-8"))

