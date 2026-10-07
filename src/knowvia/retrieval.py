from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from knowvia.chunking import chunk_sections
from knowvia.embeddings import EmbeddingProvider
from knowvia.models import RetrievalResult
from knowvia.parsing import load_markdown_documents
from knowvia.vector_store import VectorStore


def build_index(
    document_directory: Path,
    embedder: EmbeddingProvider,
    store: VectorStore,
) -> int:
    """Parse, chunk, embed, and persist all Markdown policies."""
    sections = load_markdown_documents(document_directory)
    chunks = chunk_sections(sections)
    vectors = embedder.embed([chunk.embedding_text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("Embedding provider returned an unexpected vector count")
    embedded_chunks = [
        replace(chunk, embedding=vector)
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    store.replace_all(embedded_chunks, embedder.model_name)
    return len(embedded_chunks)


class Retriever:
    """Embeds natural-language questions and searches a vector store."""

    def __init__(self, embedder: EmbeddingProvider, store: VectorStore) -> None:
        self.embedder = embedder
        self.store = store

    def search(self, question: str, top_k: int = 3) -> list[RetrievalResult]:
        question = question.strip()
        if not question:
            raise ValueError("Question cannot be empty")
        stored_model = self.store.embedding_model
        if stored_model and stored_model != self.embedder.model_name:
            raise ValueError(
                f"Index uses {stored_model}, but search is configured for "
                f"{self.embedder.model_name}. Rebuild the index or use the same model."
            )
        query_vector = self.embedder.embed([question])[0]
        return self.store.search(query_vector, top_k)

