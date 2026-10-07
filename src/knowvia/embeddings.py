from __future__ import annotations

from typing import Any, Protocol
from pathlib import Path
import hashlib
import threading

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
MINILM_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
ONNX_ARTIFACTS = {
    "model.onnx": ("onnx/model.onnx", "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    "tokenizer.json": ("tokenizer.json", "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
}


def validate_onnx_assets(directory: Path) -> None:
    """Verify immutable FP32 assets without importing an inference runtime."""
    for name, (_, digest) in ONNX_ARTIFACTS.items():
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f"Missing ONNX embedding asset: {name}; run knowvia.onnx_assets")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise RuntimeError(f"ONNX embedding asset checksum mismatch: {name}")


class OnnxEmbeddingProvider:
    """Pinned MiniLM FP32 inference; no Torch, Transformers or network access."""

    def __init__(self, directory: Path) -> None:
        validate_onnx_assets(directory)
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=256)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", pad_type_id=0)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(directory / "model.onnx"), sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return DEFAULT_LOCAL_MODEL

    def tokenize(self, texts: list[str]) -> dict[str, Any]:
        import numpy as np
        encoded = self._tokenizer.encode_batch([text.strip() for text in texts])
        return {
            "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import numpy as np
        vectors = []
        # Bound activation memory like the reference encoder's default batch size.
        with self._lock:
            for start in range(0, len(texts), 32):
                inputs = self.tokenize(texts[start:start + 32])
                feeds = {item.name: inputs[item.name] for item in self._session.get_inputs()}
                tokens = self._session.run(None, feeds)[0]
                mask = inputs["attention_mask"].astype(np.float32)[..., None]
                pooled = (tokens * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                if pooled.shape[1] != 384 or not np.isfinite(pooled).all() or (norms == 0).any():
                    raise RuntimeError("Invalid ONNX embedding output")
                vectors.extend((pooled / norms).tolist())
        return vectors


class EmbeddingProvider(Protocol):
    """Converts text into vectors, independent of storage and retrieval."""

    @property
    def model_name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbeddingProvider:
    """Free local embeddings powered by Sentence Transformers."""

    def __init__(
        self,
        model: str = DEFAULT_LOCAL_MODEL,
        encoder: Any | None = None,
    ) -> None:
        self._model_name = model
        if encoder is None:
            # Import lazily: loading this module should not initialize PyTorch or
            # download a model. The model is cached locally after its first use.
            from sentence_transformers import SentenceTransformer

            try:
                # Prefer the cache so normal use remains fully offline and does
                # not pause for remote metadata checks.
                encoder = SentenceTransformer(model, local_files_only=True)
            except OSError:
                # On first use only, download the requested model into the cache.
                encoder = SentenceTransformer(model)
        self._encoder = encoder

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._encoder.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


class OpenAIEmbeddingProvider:
    """Embedding provider backed by the OpenAI Embeddings API."""

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        client: Any | None = None,
    ) -> None:
        self._model = model
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    "The OpenAI provider requires the optional dependency. "
                    "Install it with: python -m pip install -e \".[openai]\""
                ) from error
            client = OpenAI()
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
