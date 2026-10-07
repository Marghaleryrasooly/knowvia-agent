from __future__ import annotations

import hashlib
import re


class DeterministicEmbeddingProvider:
    """Small local test double; it never calls an external API."""

    model_name = "deterministic-test-embedding"

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        aliases = {
            "vacation": "leave",
            "vacations": "leave",
            "home": "remote",
            "remotely": "remote",
            "length": "characters",
            "long": "characters",
            "development": "learning",
            "failed": "fails",
            "failing": "fails",
            "push": "pushes",
            "counts": "includes",
            "receipt": "receipts",
        }
        vector = [0.0] * self.dimensions
        words = re.findall(r"[a-z0-9]+", text.lower())
        for word in words:
            word = aliases.get(word, word)
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0
        return vector

