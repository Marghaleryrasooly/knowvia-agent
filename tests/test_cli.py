from knowvia import cli
from knowvia.embeddings import LocalEmbeddingProvider, OpenAIEmbeddingProvider
from knowvia.generation import OllamaGenerationProvider
from knowvia.models import AnswerResult, SourceCitation


def test_cli_defaults_to_local_provider(monkeypatch) -> None:
    monkeypatch.delenv("KNOWVIA_EMBEDDING_PROVIDER", raising=False)

    args = cli.create_parser().parse_args(["index"])

    assert args.provider == "local"


def test_provider_factory_selects_configured_implementation(monkeypatch) -> None:
    local_sentinel = object()
    openai_sentinel = object()
    monkeypatch.setattr(cli, "LocalEmbeddingProvider", lambda model: local_sentinel)
    monkeypatch.setattr(cli, "OpenAIEmbeddingProvider", lambda model: openai_sentinel)

    assert cli.create_embedder("local") is local_sentinel
    assert cli.create_embedder("openai") is openai_sentinel


def test_provider_classes_preserve_common_embedding_interface() -> None:
    assert callable(LocalEmbeddingProvider.embed)
    assert callable(OpenAIEmbeddingProvider.embed)


def test_ask_defaults_to_groq_and_three_chunks(monkeypatch) -> None:
    monkeypatch.delenv("KNOWVIA_GENERATION_PROVIDER", raising=False)

    args = cli.create_parser().parse_args(["ask", "What is the leave policy?"])

    assert args.generation_provider == "groq"
    assert args.top_k == 3


def test_confirm_and_cancel_support_employee_and_debug_rendering_modes() -> None:
    parser = cli.create_parser()

    confirm = parser.parse_args(["confirm", "proposal-1"])
    cancel = parser.parse_args(["cancel", "proposal-1", "--debug"])

    assert confirm.tool == "jira" and confirm.debug is False
    assert cancel.tool == "jira" and cancel.debug is True


def test_agent_accepts_explicit_conversation_session() -> None:
    args = cli.create_parser().parse_args(
        ["agent", "What about sick leave?", "--session", "employee-demo"]
    )

    assert args.session == "employee-demo"


def test_generator_factory_uses_llama_latest_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_GENERATION_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    generator = cli.create_generator("ollama")

    assert isinstance(generator, OllamaGenerationProvider)
    assert generator.model_name == "llama3.2:latest"


def test_ask_cli_prints_natural_answer_and_sources_consulted(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self, *_: object) -> None:
            pass

        def answer(self, question: str, top_k: int) -> AnswerResult:
            assert question == "How many vacation days do I get?"
            assert top_k == 3
            return AnswerResult(
                answer="Full-time employees receive 24 paid vacation days.",
                sources=[SourceCitation("01_employee_handbook.md", "Annual Leave")],
                retrieval_results=[],
            )

    monkeypatch.setattr(cli, "create_embedder", lambda _: object())
    monkeypatch.setattr(cli, "create_generator", lambda _: object())
    monkeypatch.setattr(cli, "create_evidence_evaluator", lambda _: object())
    monkeypatch.setattr(cli, "Retriever", lambda *_: object())
    monkeypatch.setattr(cli, "AnsweringService", FakeService)
    monkeypatch.setattr(
        "sys.argv",
        ["knowvia", "ask", "How many vacation days do I get?"],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "Full-time employees receive 24 paid vacation days." in output
    assert "Sources consulted:\n- Employee Handbook — Annual Leave" in output
    assert "Decision:" not in output and "Answer:" not in output


def test_ask_debug_preserves_structured_output(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self, *_: object) -> None:
            pass

        def answer(self, question: str, top_k: int) -> AnswerResult:
            return AnswerResult(
                answer="24 days",
                sources=[SourceCitation("01_employee_handbook.md", "Annual Leave")],
                retrieval_results=[],
            )

    monkeypatch.setattr(cli, "create_embedder", lambda _: object())
    monkeypatch.setattr(cli, "create_generator", lambda _: object())
    monkeypatch.setattr(cli, "create_evidence_evaluator", lambda _: object())
    monkeypatch.setattr(cli, "Retriever", lambda *_: object())
    monkeypatch.setattr(cli, "AnsweringService", FakeService)
    monkeypatch.setattr("sys.argv", ["knowvia", "ask", "question", "--debug"])

    cli.main()

    output = capsys.readouterr().out
    assert "Answer:\n24 days" in output
    assert "Sources:\n- 01_employee_handbook.md -> Annual Leave" in output
