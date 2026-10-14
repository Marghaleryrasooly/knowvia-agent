"""Offline contracts for the inactive, explicitly resettable cloud recipe."""
import json
from pathlib import Path

import pytest

from knowvia.deployment import DeploymentConfig
from knowvia.runtime_paths import PROJECT_ROOT, RuntimePaths


def recipe():
    # JSON is valid YAML and avoids a new YAML parsing dependency.
    return json.loads((PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]


def environment():
    return {v["key"]: v["value"] for v in recipe()["envVars"] if "value" in v}


def test_recipe_is_free_single_service_without_disk_database_or_auto_deploy():
    payload = json.loads((PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert len(payload["services"]) == 1 and "databases" not in payload
    service = recipe()
    assert service["plan"] == "free" and service["runtime"] == "docker"
    assert service["autoDeployTrigger"] == "off" and "disk" not in service
    assert service["healthCheckPath"] == "/health"


def test_recipe_requests_secrets_privately_and_requires_exact_host_configuration():
    variables = {v["key"]: v for v in recipe()["envVars"]}
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "KNOWVIA_ALLOWED_HOSTS"):
        assert variables[name] == {"key": name, "sync": False}
    assert not any("PASSWORD" in name or name.startswith("JIRA_") for name in variables)


def test_cloud_configuration_is_explicit_and_preserves_local_defaults():
    local = DeploymentConfig.from_environment({"KNOWVIA_GENERATION_PROVIDER": "ollama", "KNOWVIA_EVIDENCE_EVALUATOR": "ollama"})
    assert local.generation_provider == local.evidence_provider == "ollama"
    values = environment()
    values.update(GROQ_API_KEY="fictional-offline-key", GEMINI_API_KEY="fictional-offline-key")
    values["KNOWVIA_ALLOWED_HOSTS"] = "knowvia-demo.onrender.com,localhost,127.0.0.1"
    cloud = DeploymentConfig.from_environment(values)
    cloud.validate()
    assert cloud.generation_provider == cloud.evidence_provider == "groq"
    assert cloud.router_provider == "gemini" and cloud.embedding_provider == "onnx"
    assert cloud.secure_cookie and not cloud.development_identity_override
    assert not cloud.registration_enabled and not cloud.real_jira_mutations_enabled
    assert cloud.action_backend == cloud.mcp_ticket_backend == "mock"


def test_groq_factories_need_no_ollama_and_preserve_candidate_settings(monkeypatch):
    from knowvia.cli import create_generator, create_evidence_evaluator
    from knowvia.generation import GroqGenerationProvider
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GROQ_API_KEY", "fictional-offline-key")
    answer = create_generator("groq")
    evidence = create_evidence_evaluator("groq")
    assert isinstance(answer, GroqGenerationProvider)
    assert isinstance(evidence.generator, GroqGenerationProvider)
    assert answer.model_name == evidence.generator.model_name == "openai/gpt-oss-120b"
    assert (answer._temperature, answer._max_output_tokens, answer.reasoning_effort) == (.1, 1024, "low")
    assert (evidence.generator._temperature, evidence.generator._max_output_tokens,
            evidence.generator.reasoning_effort) == (0., 2048, "medium")
    assert evidence.require_scope


def test_cloud_readiness_and_health_do_not_initialize_or_call_inference(tmp_path, monkeypatch):
    from knowvia.deployment_bootstrap import DeploymentResources
    from knowvia.deployment_readiness import validate_readiness
    import knowvia.cli as cli
    import knowvia.embeddings as embeddings
    values = environment()
    values.update(GROQ_API_KEY="fictional-offline-key", GEMINI_API_KEY="fictional-offline-key")
    values["KNOWVIA_ALLOWED_HOSTS"] = "localhost,127.0.0.1"
    config = DeploymentConfig.from_environment(values)
    model = tmp_path / "model-assets"
    monkeypatch.setenv("KNOWVIA_ONNX_MODEL_DIR", str(model))
    checked = []
    monkeypatch.setattr(embeddings, "validate_onnx_assets", lambda path: checked.append(path))
    def forbidden(*args, **kwargs):
        pytest.fail("Readiness must not initialize embeddings or an inference provider")
    monkeypatch.setattr(cli, "create_embedder", forbidden)
    monkeypatch.setattr(cli, "create_generator", forbidden)
    monkeypatch.setattr(cli, "create_evidence_evaluator", forbidden)
    resources = DeploymentResources(PROJECT_ROOT / "knowvia_m1_dataset",
                                    PROJECT_ROOT / "data/knowvia_index.json",
                                    PROJECT_ROOT / "src/knowvia/static")
    result = validate_readiness(config, RuntimePaths(tmp_path / "runtime"), resources)
    assert result["health"] == "ok" and checked == [model]


def test_recipe_reuses_existing_image_entrypoint_and_onnx_build_switch():
    text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG EMBEDDING_PROVIDER=local' in text
    assert 'CMD ["python", "-m", "knowvia.production"]' in text
    assert environment()["EMBEDDING_PROVIDER"] == "onnx"
    assert "dockerCommand" not in recipe()
    assert "ARG GROQ_API_KEY" not in text and "ARG GEMINI_API_KEY" not in text


def test_documentation_discloses_reset_bootstrap_and_memory_gates():
    text = (PROJECT_ROOT / "docs/hosting-options-history.md").read_text(encoding="utf-8")
    assert "resettable" in text and "no shell" in text
    assert "cannot bootstrap an admin" in text
    assert "not** a Linux" in text and "knowledge/versions/" in text
    assert "Groq" in text and "acceptance" in text


def test_unfilled_recipe_fails_closed_without_server_secrets():
    from knowvia.deployment import DeploymentConfigurationError
    with pytest.raises(DeploymentConfigurationError, match="not configured"):
        DeploymentConfig.from_environment(environment())
