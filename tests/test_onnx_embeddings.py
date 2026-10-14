from pathlib import Path
import json
import hashlib
import os

import numpy as np
import pytest

from knowvia.embeddings import (
    OnnxEmbeddingProvider, LocalEmbeddingProvider, DEFAULT_LOCAL_MODEL,
    MINILM_REVISION, validate_onnx_assets,
)
from knowvia.vector_store import JsonVectorStore
from knowvia.retrieval import Retriever
from knowvia.skill_orchestration import SourceScopedRetriever
from knowvia.models import Chunk


def test_missing_and_changed_assets_fail_closed(tmp_path):
    with pytest.raises(RuntimeError, match="Missing"):
        validate_onnx_assets(tmp_path)
    (tmp_path / "model.onnx").write_bytes(b"wrong model")
    with pytest.raises(RuntimeError, match="checksum"):
        validate_onnx_assets(tmp_path)


def test_onnx_empty_input_never_calls_runtime():
    provider = OnnxEmbeddingProvider.__new__(OnnxEmbeddingProvider)
    assert provider.embed([]) == []
    assert provider.model_name == DEFAULT_LOCAL_MODEL


def test_masked_pooling_normalization_and_order():
    import threading
    provider = OnnxEmbeddingProvider.__new__(OnnxEmbeddingProvider)
    provider._lock = threading.Lock()
    provider.tokenize = lambda texts: {"attention_mask": np.array([[1, 0], [1, 1]])}
    class Session:
        def get_inputs(self):
            return [type("Input", (), {"name": "attention_mask"})()]
        def run(self, outputs, feeds):
            tokens = np.zeros((2, 2, 384), dtype=np.float32)
            tokens[0, 0, 0] = 1
            tokens[0, 1, 1] = 999  # Padding must not contribute.
            tokens[1, :, 1] = 2
            return [tokens]
    provider._session = Session()
    values = np.array(provider.embed(["first", "second"]))
    assert values.shape == (2, 384)
    assert values[0, 0] == values[1, 1] == 1
    assert values[0, 1] == values[1, 0] == 0


def test_explicit_selection_preserves_local_default(monkeypatch):
    from knowvia import cli
    monkeypatch.delenv("KNOWVIA_ONNX_MODEL_DIR", raising=False)
    with pytest.raises(ValueError, match="required"):
        cli.create_embedder("onnx")
    monkeypatch.setenv("KNOWVIA_ONNX_MODEL_DIR", "prepared-assets")
    monkeypatch.setattr(cli, "OnnxEmbeddingProvider", lambda path: path)
    assert cli.create_embedder("onnx") == Path("prepared-assets")


def test_invalid_embedding_configuration_fails_closed():
    from knowvia.deployment import DeploymentConfig, DeploymentConfigurationError
    assert DeploymentConfig.from_environment({"GROQ_API_KEY": "fictional"}).embedding_provider == "local"
    assert DeploymentConfig.from_environment({"KNOWVIA_EMBEDDING_PROVIDER": "onnx", "GROQ_API_KEY": "fictional"}).embedding_provider == "onnx"
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig.from_environment({"KNOWVIA_EMBEDDING_PROVIDER": "unknown"})


def test_pinned_asset_setup_is_idempotent(monkeypatch, tmp_path):
    from knowvia import onnx_assets
    calls = []
    monkeypatch.setattr(onnx_assets, "validate_onnx_assets", lambda directory: calls.append(directory))
    monkeypatch.setattr(onnx_assets, "urlopen", lambda *args, **kwargs: pytest.fail("Unexpected download"))
    onnx_assets.prepare_assets(tmp_path)
    assert calls == [tmp_path]


QUERIES = [
    "How many vacation days do I get?", "i want know my yearly leave",
    "What paid absence is available when I am unwell?", "sick days?",
    "Can I work remotely?", "What are our standard working hours?",
    "my vpn dose not work", "My VPN isn't working. What should I do?",
    "What should I do if VPN troubleshooting does not fix my VPN problem?",
    "i cant access my work account", "Minimum work password length?",
    "How do I claim travel expenses?", "Explain our code of conduct",
    "What is the company's lunar relocation allowance?",
    "  VACATION policy\t\n", "Café — passwords?!", "你好 remote work",
    "", "?!", "VPN " * 254, "VPN " * 255, "VPN " * 256,
    "VPN " * 257, "vacation " * 300,
]


@pytest.mark.embedding_parity
def test_pinned_reference_index_and_onnx_retrieval_parity():
    directory = os.getenv("KNOWVIA_ONNX_MODEL_DIR")
    if not directory:
        pytest.skip("Set KNOWVIA_ONNX_MODEL_DIR to checksum-pinned local assets")
    from sentence_transformers import SentenceTransformer
    encoder = SentenceTransformer(DEFAULT_LOCAL_MODEL, revision=MINILM_REVISION,
                                  local_files_only=True, device="cpu")
    reference = LocalEmbeddingProvider(encoder=encoder)
    proposed = OnnxEmbeddingProvider(Path(directory))
    index = Path(__file__).parents[1] / "data" / "knowvia_index.json"
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    assert before == "e96ff2544ed3d83fc330608dc4b72d9d3a1867589b6b736ae1f107d9fb480319"
    payload = json.loads(index.read_text(encoding="utf-8"))
    chunks = [Chunk.from_dict(value) for value in payload["chunks"]]
    assert len(chunks) == 37
    stored = np.asarray([chunk.embedding for chunk in chunks])
    rebuilt = np.asarray(reference.embed([chunk.embedding_text for chunk in chunks]))
    document_cosines = np.sum(stored * rebuilt, axis=1) / (
        np.linalg.norm(stored, axis=1) * np.linalg.norm(rebuilt, axis=1))
    assert document_cosines.min() >= 0.99999
    assert np.max(np.abs(stored - rebuilt)) <= 1e-4

    old = np.asarray(reference.embed(QUERIES))
    new = np.asarray(proposed.embed(QUERIES))
    assert old.shape == new.shape == (len(QUERIES), 384)
    assert np.isfinite(new).all()
    assert np.isfinite(old).all()
    assert np.max(np.abs(np.linalg.norm(old, axis=1) - 1)) <= 1e-6
    assert np.max(np.abs(np.linalg.norm(new, axis=1) - 1)) <= 1e-6
    cosines = np.sum(old * new, axis=1) / (
        np.linalg.norm(old, axis=1) * np.linalg.norm(new, axis=1))
    assert cosines.min() >= 0.99999
    expected_tokens = encoder.tokenize(QUERIES)
    actual_tokens = proposed.tokenize(QUERIES)
    for key in actual_tokens:
        assert np.array_equal(expected_tokens[key].numpy(), actual_tokens[key]), key
    assert actual_tokens["input_ids"].shape[1] == 256
    singles = np.asarray([proposed.embed([query])[0] for query in QUERIES])
    assert np.max(np.abs(new - singles)) <= 1e-4
    store = JsonVectorStore(index)
    max_score_delta = 0.0
    for old_vector, new_vector in zip(old, new):
        a, b = store.search(old_vector.tolist(), 37), store.search(new_vector.tolist(), 37)
        scores_a = {result.chunk.id: result.score for result in a}
        scores_b = {result.chunk.id: result.score for result in b}
        max_score_delta = max(max_score_delta, max(abs(scores_a[key] - scores_b[key]) for key in scores_a))
        for k in (3, 5, 10):
            assert [r.chunk.id for r in a[:k]] == [r.chunk.id for r in b[:k]]
    assert max_score_delta <= 1e-4
    sources = sorted({chunk.source for chunk in chunks})
    class RecordedProvider:
        model_name = DEFAULT_LOCAL_MODEL
        def __init__(self, values):
            self.values = dict(zip((query.strip() for query in QUERIES), values.tolist()))
        def embed(self, texts):
            return [self.values[text] for text in texts]
    for query in QUERIES:
        if not query.strip():
            continue
        for scope in [tuple(sources), *((source,) for source in sources)]:
            a = SourceScopedRetriever(Retriever(RecordedProvider(old), store), scope)
            b = SourceScopedRetriever(Retriever(RecordedProvider(new), store), scope)
            for k in (3, 5, 10):
                assert [r.chunk.id for r in a.search(query, k)] == [r.chunk.id for r in b.search(query, k)]
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    print({"queries": len(QUERIES), "document_min_cosine": float(document_cosines.min()),
           "document_max_coordinate_delta": float(np.max(np.abs(stored - rebuilt))),
           "query_max_norm_error": float(np.max(np.abs(np.linalg.norm(new, axis=1) - 1))),
           "query_min_cosine": float(cosines.min()), "max_score_delta": max_score_delta,
           "index_sha256": before})
