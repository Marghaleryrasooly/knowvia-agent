from __future__ import annotations

import os

import pytest

from knowvia.generation import OllamaGenerationProvider


@pytest.mark.ollama_live
@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA_LIVE") != "1",
    reason="Set RUN_OLLAMA_LIVE=1 to test the locally running Ollama model",
)
def test_live_ollama_generates_text() -> None:
    provider = OllamaGenerationProvider()

    answer = provider.generate(
        "Answer only using the supplied evidence.",
        "Evidence: Employees receive 24 paid vacation days.\n"
        "Question: How many vacation days do employees receive?",
    )

    assert answer.strip()
