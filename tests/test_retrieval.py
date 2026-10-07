from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import DeterministicEmbeddingProvider
from knowvia.embeddings import LocalEmbeddingProvider, OpenAIEmbeddingProvider
from knowvia.retrieval import Retriever, build_index
from knowvia.vector_store import JsonVectorStore

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "knowvia_m1_dataset"
GROUND_TRUTH = DATASET / "m1_ground_truth.json"


def retrieval_cases() -> list[dict[str, object]]:
    cases = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    return [case for case in cases if case["expected_behavior"] == "retrieve"]


@pytest.fixture
def local_retriever(tmp_path: Path) -> Retriever:
    embedder = DeterministicEmbeddingProvider()
    store = JsonVectorStore(tmp_path / "index.json")
    assert build_index(DATASET, embedder, store) == 37
    return Retriever(embedder, store)


@pytest.mark.parametrize("case", retrieval_cases(), ids=lambda c: str(c["expected_section"]))
def test_ground_truth_target_is_in_top_three(
    local_retriever: Retriever, case: dict[str, object]
) -> None:
    results = local_retriever.search(str(case["question"]), top_k=3)
    matches = {(result.chunk.source, result.chunk.section) for result in results}

    assert (case["expected_doc"], case["expected_section"]) in matches


def test_empty_question_is_rejected(local_retriever: Retriever) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        local_retriever.search("   ")


@pytest.mark.local_model
def test_default_local_model_ground_truth_targets_are_in_top_three(tmp_path: Path) -> None:
    embedder = LocalEmbeddingProvider()
    store = JsonVectorStore(tmp_path / "local-model-index.json")
    build_index(DATASET, embedder, store)
    retriever = Retriever(embedder, store)

    for case in retrieval_cases():
        results = retriever.search(str(case["question"]), top_k=3)
        matches = {(result.chunk.source, result.chunk.section) for result in results}

        assert (case["expected_doc"], case["expected_section"]) in matches


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_live_openai_ground_truth_targets_are_in_top_three(tmp_path: Path) -> None:
    embedder = OpenAIEmbeddingProvider()
    store = JsonVectorStore(tmp_path / "openai-index.json")
    build_index(DATASET, embedder, store)
    retriever = Retriever(embedder, store)

    for case in retrieval_cases():
        results = retriever.search(str(case["question"]), top_k=3)
        matches = {(result.chunk.source, result.chunk.section) for result in results}

        assert (case["expected_doc"], case["expected_section"]) in matches
