import pytest
from knowvia.deployment import DeploymentConfig, DeploymentConfigurationError
from knowvia.cli import create_parser, create_generator
from knowvia.generation import OllamaGenerationProvider


def test_normal_startup_defaults_to_groq_and_requires_server_secret():
    with pytest.raises(DeploymentConfigurationError, match='Groq inference'):
        DeploymentConfig.from_environment({})
    config = DeploymentConfig.from_environment({'GROQ_API_KEY': 'fictional-offline'})
    assert config.generation_provider == config.evidence_provider == 'groq'


def test_explicit_local_alternative_needs_no_groq_key():
    config = DeploymentConfig.from_environment({'KNOWVIA_GENERATION_PROVIDER': 'ollama',
                                               'KNOWVIA_EVIDENCE_EVALUATOR': 'ollama'})
    assert config.generation_provider == config.evidence_provider == 'ollama'
    assert isinstance(create_generator('ollama'), OllamaGenerationProvider)


def test_both_cli_entrypoints_default_to_groq(monkeypatch):
    monkeypatch.delenv('KNOWVIA_GENERATION_PROVIDER', raising=False)
    monkeypatch.delenv('KNOWVIA_EVIDENCE_EVALUATOR', raising=False)
    args = create_parser().parse_args(['ask', 'question'])
    assert args.generation_provider == args.evidence_evaluator == 'groq'
