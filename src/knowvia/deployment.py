from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from uuid import uuid4

from knowvia.agent_models import ToolResult
from knowvia.agent_tools import LocalITTicketTool, ToolSpecification


class DeploymentConfigurationError(ValueError):
    """The process was configured with an unsafe deployment combination."""


class ApplicationEnvironment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    DEMO = "demo"
    PRODUCTION = "production"


def _flag(environment: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DeploymentConfigurationError(f"{name} must be a boolean value")


@dataclass(frozen=True, repr=False)
class DeploymentConfig:
    environment: ApplicationEnvironment = ApplicationEnvironment.PRODUCTION
    secure_cookie: bool = True
    development_identity_override: bool = False
    seed_demo_employee_directory: bool = False
    registration_enabled: bool = False
    developer_details_enabled: bool = False
    debug_enabled: bool = False
    action_backend: str = "mock"
    mcp_ticket_backend: str = "mock"
    real_jira_mutations_enabled: bool = False
    router_provider: str = "deterministic"
    gemini_api_key_configured: bool = False
    jira_credentials_configured: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    hsts_enabled: bool = False
    embedding_provider: str = "local"
    generation_provider: str = "ollama"
    evidence_provider: str = "ollama"
    cloudflare_credentials_configured: bool = False
    groq_api_key_configured: bool = False

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "DeploymentConfig":
        values = os.environ if environment is None else environment
        raw_mode = values.get("KNOWVIA_ENVIRONMENT", "production").strip().lower()
        try:
            mode = ApplicationEnvironment(raw_mode)
        except ValueError as exc:
            raise DeploymentConfigurationError("KNOWVIA_ENVIRONMENT is invalid") from exc
        hosts = tuple(
            host.strip() for host in values.get(
                "KNOWVIA_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
            ).split(",") if host.strip()
        )
        config = cls(
            environment=mode,
            secure_cookie=_flag(values, "KNOWVIA_AUTH_COOKIE_SECURE", True),
            development_identity_override=_flag(values, "KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE", False),
            seed_demo_employee_directory=_flag(values, "KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY", False),
            registration_enabled=_flag(values, "KNOWVIA_REGISTRATION_ENABLED", False),
            developer_details_enabled=_flag(values, "KNOWVIA_ALLOW_DEVELOPER_DETAILS", False),
            debug_enabled=_flag(values, "KNOWVIA_DEBUG", False),
            action_backend=values.get("KNOWVIA_AGENT_TOOL", "mock").strip().lower(),
            mcp_ticket_backend=values.get("KNOWVIA_MCP_TICKET_BACKEND", "mock").strip().lower(),
            real_jira_mutations_enabled=_flag(values, "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS", False),
            router_provider=values.get("KNOWVIA_AGENT_ROUTER", "deterministic").strip().lower(),
            gemini_api_key_configured=bool(values.get("GEMINI_API_KEY", "").strip()),
            jira_credentials_configured=all(
                bool(values.get(name, "").strip()) for name in (
                    "JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN",
                    "JIRA_PROJECT_KEY", "JIRA_ISSUE_TYPE",
                )
            ),
            allowed_hosts=hosts,
            hsts_enabled=_flag(values, "KNOWVIA_ENABLE_HSTS", False),
            embedding_provider=values.get("KNOWVIA_EMBEDDING_PROVIDER", "local").strip().lower(),
            generation_provider=values.get("KNOWVIA_GENERATION_PROVIDER", "groq").strip().lower(),
            evidence_provider=values.get("KNOWVIA_EVIDENCE_EVALUATOR", "groq").strip().lower(),
            groq_api_key_configured=bool(values.get("GROQ_API_KEY", "").strip()),
            cloudflare_credentials_configured=all(bool(values.get(name, "").strip()) for name in (
                "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.generation_provider not in {"ollama", "cloudflare", "gemini", "groq"} or self.evidence_provider not in {"ollama", "cloudflare", "gemini", "groq"}:
            raise DeploymentConfigurationError("Generation/evidence provider is invalid")
        if "groq" in {self.generation_provider, self.evidence_provider} and not self.groq_api_key_configured:
            raise DeploymentConfigurationError("Groq inference is selected but not configured")
        if "gemini" in {self.generation_provider, self.evidence_provider} and not self.gemini_api_key_configured:
            raise DeploymentConfigurationError("Gemini inference is selected but not configured")
        if "cloudflare" in {self.generation_provider, self.evidence_provider} and not self.cloudflare_credentials_configured:
            raise DeploymentConfigurationError("Cloudflare is selected but not configured")
        if self.embedding_provider not in {"local", "openai", "onnx"}:
            raise DeploymentConfigurationError("KNOWVIA_EMBEDDING_PROVIDER is invalid")
        public = self.environment in {ApplicationEnvironment.DEMO, ApplicationEnvironment.PRODUCTION}
        if self.action_backend not in {"mock", "jira", "mcp"}:
            raise DeploymentConfigurationError("KNOWVIA_AGENT_TOOL is invalid")
        if self.mcp_ticket_backend not in {"mock", "jira"}:
            raise DeploymentConfigurationError("KNOWVIA_MCP_TICKET_BACKEND is invalid")
        if self.router_provider not in {"deterministic", "gemini"}:
            raise DeploymentConfigurationError("KNOWVIA_AGENT_ROUTER is invalid")
        if self.router_provider == "gemini" and not self.gemini_api_key_configured:
            raise DeploymentConfigurationError("Gemini routing is selected but not configured")
        if not self.allowed_hosts:
            raise DeploymentConfigurationError("At least one allowed host is required")
        if public and self.development_identity_override:
            raise DeploymentConfigurationError("Development identity override is forbidden in demo/production")
        if public and not self.secure_cookie:
            raise DeploymentConfigurationError("Secure authentication cookies are required in demo/production")
        if public and self.debug_enabled:
            raise DeploymentConfigurationError("Debug mode is forbidden in demo/production")
        if public and self.developer_details_enabled:
            raise DeploymentConfigurationError("Developer details are forbidden in demo/production")
        if self.environment is ApplicationEnvironment.DEMO and self.real_jira_mutations_enabled:
            raise DeploymentConfigurationError("Real Jira mutation is forbidden in public demo mode")
        if self.environment is ApplicationEnvironment.DEMO and (
            self.action_backend == "jira" or
            (self.action_backend == "mcp" and self.mcp_ticket_backend == "jira")
        ):
            raise DeploymentConfigurationError("Public demo ticketing must use a local backend")
        jira_selected = self.action_backend == "jira" or (
            self.action_backend == "mcp" and self.mcp_ticket_backend == "jira"
        )
        if public and jira_selected and not self.real_jira_mutations_enabled:
            raise DeploymentConfigurationError("Real Jira requires explicit server-side enablement")
        if jira_selected and self.real_jira_mutations_enabled and not self.jira_credentials_configured:
            raise DeploymentConfigurationError("Real Jira is enabled but not fully configured")
        if self.environment is ApplicationEnvironment.PRODUCTION and self.seed_demo_employee_directory:
            raise DeploymentConfigurationError("Demo employee seeding is forbidden in production")
        if self.environment is ApplicationEnvironment.DEMO and self.registration_enabled and not self.seed_demo_employee_directory:
            raise DeploymentConfigurationError("Public demo registration requires the fictional demo directory")
        if self.hsts_enabled and not public:
            raise DeploymentConfigurationError("HSTS is only valid for HTTPS demo/production deployments")

    @property
    def public_demo(self) -> bool:
        return self.environment is ApplicationEnvironment.DEMO

    @property
    def effective_action_backend(self) -> str:
        jira_selected = self.action_backend == "jira" or (
            self.action_backend == "mcp" and self.mcp_ticket_backend == "jira"
        )
        if jira_selected and not self.real_jira_mutations_enabled:
            return "mock"
        return self.action_backend


class DemoITTicketTool(LocalITTicketTool):
    """Confirmation-gated, process-local ticketing for an internet-facing demo."""

    specification = ToolSpecification(
        name="create_it_ticket",
        description="Create a fictional local demo IT support ticket.",
        required_arguments=LocalITTicketTool.specification.required_arguments,
        argument_schema=LocalITTicketTool.specification.argument_schema,
        requires_confirmation=True,
    )

    def execute(self, arguments: Mapping[str, str]) -> ToolResult:
        result = super().execute(arguments)
        demo_id = f"DEMO_IT-{uuid4().int % 10_000_000_000:010d}"
        data = dict(result.data)
        data["ticket_id"] = demo_id
        return ToolResult(
            success=True,
            tool_name=result.tool_name,
            message=f"Created fictional local demo ticket {demo_id}.",
            data=data,
        )
