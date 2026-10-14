from __future__ import annotations

import os
import json
import re
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from knowvia.accounts import RegistrationError, RegistrationRequest, RegistrationService, SqliteAccountRepository
from knowvia.authentication import AuthenticationError, AuthenticationService, LoginRequest, SqliteAuthenticatedSessionStore
from knowvia.employee_directory import EmployeeDirectoryIdentityService, SqliteEmployeeDirectory, seed_demo_employee_directory
from knowvia.deployment import ApplicationEnvironment, DemoITTicketTool, DeploymentConfig, DeploymentConfigurationError

from knowvia.agent_models import (
    ActionProposalStatus,
    AgentDecision,
    AgentDecisionKind,
    AgentResult,
)
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import ConfirmationRequiredTool
from knowvia.answering import AnsweringService
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.cli import (
    DEFAULT_DEVELOPMENT_IDENTITY,
    _action_services,
    _mcp_transport,
    _ticket_lifecycle_service,
    create_agent_tool,
    create_embedder,
    create_evidence_evaluator,
    create_generator,
)
from knowvia.confirmation import PendingActionStore
from knowvia.conversation import ConversationCoordinator, FollowUpResolver, SqliteConversationStore
from knowvia.gemini_routing import DEFAULT_GEMINI_ROUTER_MODEL, GeminiAgentRouter, GoogleGenAIRoutingTransport
from knowvia.resilient_routing import ResilientAgentRouter, SafeFallbackPolicy
from knowvia.identity import IdentityContext, IdentityError, IdentityProvider, LocalIdentityProvider
from knowvia.multi_step import DeterministicMultiStepPlanner, MultiStepCoordinator, validate_plan
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.presentation import DeterministicEmployeeResponsePresenter, EmployeeResponse, friendly_source_label
from knowvia.retrieval import Retriever
from knowvia.skill_orchestration import SkillAgentResult, SkillAwareAgent, SourceScopedRetriever
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.ticket_lifecycle import SqliteTicketLifecycleStore, StoredTicketStatusProvider, TicketLifecycleService
from knowvia.vector_store import JsonVectorStore
from knowvia.runtime_paths import RuntimePaths
from knowvia.admin import AdministrationError, LoginAbuseGuard, SqliteAuthority
from knowvia.knowledge_management import KnowledgeManagement, SnapshotSourcePolicy, EmptyKnowledgeRetriever
from knowvia.employee_directory import EmployeeRecord


STATIC_ROOT = Path(__file__).with_name("static")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class WebAuthenticationConfig:
    cookie_name: str = "knowvia_session"
    secure_cookie: bool = True
    same_site: str = "lax"
    allow_development_identity_override: bool = False

    @classmethod
    def from_environment(cls) -> "WebAuthenticationConfig":
        deployment = DeploymentConfig.from_environment()
        return cls(
            secure_cookie=deployment.secure_cookie,
            allow_development_identity_override=deployment.development_identity_override,
        )

    @classmethod
    def local_development(cls) -> "WebAuthenticationConfig":
        return cls(secure_cookie=False, allow_development_identity_override=True)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hsts_enabled: bool = False) -> None:
        super().__init__(app)
        self._hsts_enabled = hsts_enabled

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
            "script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; connect-src 'self'"
        )
        if self._hsts_enabled:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


@dataclass(frozen=True)
class AuthenticationRuntime:
    registration: RegistrationService
    authentication: AuthenticationService


@dataclass(frozen=True)
class RuntimeBundle:
    skill_agent: SkillAwareAgent
    confirmation_agent: AgentOrchestrator
    ticket_lifecycle: TicketLifecycleService
    transport_label: str


class ConversationInFlightError(RuntimeError):
    """A destructive operation targeted a conversation with active work."""


class RuntimeFactory(Protocol):
    def build(self, identity: IdentityContext) -> RuntimeBundle: ...


class ProductionRuntimeFactory:
    """Build request-scoped safety boundaries around shared retrieval components."""

    def __init__(self, deployment: DeploymentConfig | None = None, paths: RuntimePaths | None = None, knowledge_manager=None, embedder_factory=None) -> None:
        self.deployment = deployment or DeploymentConfig.from_environment()
        self.paths = paths or RuntimePaths.from_environment()
        self.embedding_provider = self.deployment.embedding_provider
        self.generation_provider = self.deployment.generation_provider
        self.evidence_provider = self.deployment.evidence_provider
        self.tool_provider = self.deployment.effective_action_backend
        self.router_provider = self.deployment.router_provider
        self._knowledge_lock = Lock()
        self._knowledge_components = None
        self.knowledge_manager = knowledge_manager
        self.embedder_factory = embedder_factory or (lambda: create_embedder(self.embedding_provider))

    def _load_knowledge_components(self):
        if self._knowledge_components is None:
            with self._knowledge_lock:
                if self._knowledge_components is None:
                    self._knowledge_components = (
                        Retriever(
                            self.embedder_factory(),
                            JsonVectorStore(self.paths.index),
                        ),
                        create_generator(self.generation_provider),
                        create_evidence_evaluator(self.evidence_provider),
                    )
        return self._knowledge_components

    def build(self, identity: IdentityContext) -> RuntimeBundle:
        retriever, generator, evidence = self._load_knowledge_components()
        snapshot, allowed_sources = (self.knowledge_manager.request_snapshot(identity)
                                     if self.knowledge_manager is not None else (None, ()))
        registry = snapshot.registry() if snapshot else create_default_skill_registry()
        if snapshot:
            # A request keeps its immutable index and ACL snapshot even while an
            # administrator publishes or rolls back a newer version.
            retriever = Retriever(retriever.embedder, JsonVectorStore(snapshot.index))
        mcp_transport = _mcp_transport(identity) if self.tool_provider == "mcp" else None
        tool = DemoITTicketTool() if self.deployment.public_demo else create_agent_tool(self.tool_provider, identity, mcp_transport)
        if not tool.specification.requires_confirmation:
            tool = ConfirmationRequiredTool(tool)
        pending, audit = _action_services(self.paths)
        lifecycle_provider = "mock" if self.deployment.public_demo else self.tool_provider
        lifecycle = _ticket_lifecycle_service(lifecycle_provider, audit, mcp_transport, self.paths)

        def router_factory(skill, specifications):
            if self.router_provider == "deterministic":
                return DeterministicAgentRouter()
            if self.router_provider == "gemini":
                primary = GeminiAgentRouter(
                    list(specifications),
                    GoogleGenAIRoutingTransport(
                        model=os.getenv("GEMINI_ROUTER_MODEL", DEFAULT_GEMINI_ROUTER_MODEL)
                    ),
                )
                return ResilientAgentRouter(
                    primary,
                    DeterministicAgentRouter(),
                    SafeFallbackPolicy(tuple(specifications)),
                )
            raise ValueError("Unsupported router configuration")

        def knowledge_factory(skill):
            sources = tuple(s for s in skill.knowledge_sources if s in allowed_sources) if snapshot else skill.knowledge_sources
            return AnsweringService(
                SourceScopedRetriever(retriever, sources) if sources else EmptyKnowledgeRetriever(),
                generator,
                evidence,
            )

        policy = IdentityPermissionPolicy()
        authorizer = IdentityActionAuthorizer(
            identity,
            policy,
            AllowRegisteredActionAuthorizer({tool.specification.name}),
        )
        skill_agent = SkillAwareAgent(
            registry,
            DeterministicSkillSelector(),
            router_factory,
            knowledge_factory,
            [tool],
            pending,
            audit_sink=audit,
            ticket_lifecycle_service=lifecycle,
            knowledge_source_policy=SnapshotSourcePolicy(allowed_sources) if snapshot else None,
        )
        confirmation_agent = AgentOrchestrator(
            DeterministicAgentRouter(),
            object(),
            [tool],
            pending,
            authorizer=authorizer,
            audit_sink=audit,
            identity_context=identity,
            ticket_lifecycle_recorder=lifecycle,
        )
        label = "Demo local" if self.deployment.public_demo else ("MCP" if self.tool_provider == "mcp" else "Direct")
        return RuntimeBundle(skill_agent, confirmation_agent, lifecycle, label)


class KnowviaWebService:
    """Interface-neutral application service consumed by the web API."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        identity_provider: IdentityProvider | None = None,
        conversation_store=None,
        presenter=None,
        proposal_store: PendingActionStore | None = None,
    ) -> None:
        self.runtime_factory = runtime_factory
        self.identity_provider = identity_provider or LocalIdentityProvider()
        self.conversation_store = conversation_store or SqliteConversationStore(RuntimePaths.from_environment().conversations)
        self.presenter = presenter or DeterministicEmployeeResponsePresenter()
        self.proposal_store = proposal_store
        self._conversation_request_lock = Lock()
        self._conversation_requests: set[tuple[str, str]] = set()

    def begin_conversation_request(self, session_id: str, identity_id: str) -> bool:
        key = (identity_id, session_id)
        with self._conversation_request_lock:
            if key in self._conversation_requests:
                return False
            self._conversation_requests.add(key)
            return True

    def end_conversation_request(self, session_id: str, identity_id: str) -> None:
        with self._conversation_request_lock:
            self._conversation_requests.discard((identity_id, session_id))

    def chat(self, session_id: str, message: str, identity_id: str, demo: bool = False) -> dict:
        identity = self._identity(identity_id)
        if identity is None:
            return self._denied_payload()
        runtime = self.runtime_factory.build(identity)
        planner = DeterministicMultiStepPlanner()
        try:
            plan = validate_plan(planner.plan(message))
        except Exception:
            return self._payload(
                EmployeeResponse(
                    kind=self.presenter.present_agent(self._unsupported()).kind,
                    text="I couldn't safely plan that request using the available capabilities.",
                ),
                (),
                runtime.transport_label,
                demo,
            )

        if len(plan.steps) > 1:
            multi = MultiStepCoordinator(planner, runtime.skill_agent).handle(
                message, identity=identity
            )
            agent_results = tuple(item.agent_result for item in multi.step_results)
            response = self.presenter.present_multi_step(agent_results)
            skills = tuple(item.selected_skill_id for item in multi.step_results if item.selected_skill_id)
            return self._payload(response, agent_results, runtime.transport_label, demo, skills)

        registry = create_default_skill_registry()
        selector = DeterministicSkillSelector()
        conversation = ConversationCoordinator(
            self.conversation_store,
            FollowUpResolver(selector),
            registry.discover(),
            runtime.skill_agent,
            self.presenter,
        ).handle(session_id, message, identity=identity)
        skills = (
            (conversation.skill_result.selected_skill_id,)
            if conversation.skill_result and conversation.skill_result.selected_skill_id
            else ()
        )
        return self._payload(
            conversation.response,
            (conversation.agent_result,),
            runtime.transport_label,
            demo,
            skills,
        )

    def confirm(self, proposal_id: str, identity_id: str, demo: bool = False) -> dict:
        identity = self._identity(identity_id)
        if identity is None:
            return self._denied_payload()
        runtime = self.runtime_factory.build(identity)
        result = runtime.confirmation_agent.confirm(proposal_id)
        payload = self._payload(
            self.presenter.present_agent(result), (result,), runtime.transport_label, demo
        )
        pending_actions = runtime.confirmation_agent.pending_actions
        proposal = pending_actions.get(proposal_id) if pending_actions is not None else None
        if (
            proposal is not None
            and proposal.subject_id == identity.subject_id
            and proposal.status in {
                ActionProposalStatus.EXECUTED,
                ActionProposalStatus.FAILED,
                ActionProposalStatus.EXPIRED,
            }
        ):
            update: dict[str, str] = {
                "proposal_id": proposal.proposal_id,
                "status": proposal.status.value,
            }
            if proposal.result is not None:
                identifier = (
                    proposal.result.data.get("issue_key")
                    or proposal.result.data.get("ticket_id")
                )
                if isinstance(identifier, str) and identifier.strip():
                    update["ticket_id"] = identifier.strip()
            payload["proposal_update"] = update
        return payload

    def cancel(self, proposal_id: str, identity_id: str, demo: bool = False) -> dict:
        identity = self._identity(identity_id)
        if identity is None:
            return self._denied_payload()
        runtime = self.runtime_factory.build(identity)
        result = runtime.confirmation_agent.reject(proposal_id)
        return self._payload(
            self.presenter.present_agent(result), (result,), runtime.transport_label, demo
        )

    def notifications(self, identity_id: str) -> dict:
        identity = self._identity(identity_id)
        if identity is None:
            return {"notifications": []}
        events = self.runtime_factory.build(identity).ticket_lifecycle.notifications_for(identity)
        return {
            "notifications": [
                {
                    "id": event.event_id,
                    "ticket_id": event.ticket_id,
                    "message": event.message,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
        }

    def conversations(self, identity_id: str) -> dict:
        identity = self._identity(identity_id)
        if identity is None:
            return {"conversations": []}
        records = self.conversation_store.list_conversations(identity.subject_id)
        return {
            "conversations": [
                {
                    "session_id": item.session_id,
                    "title": item.title,
                    "pinned": item.pinned,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in records
            ]
        }

    def conversation_messages(self, session_id: str, identity_id: str) -> dict | None:
        identity = self._identity(identity_id)
        if identity is None:
            return None
        if not any(
            item.session_id == session_id
            for item in self.conversation_store.list_conversations(identity.subject_id)
        ):
            return None
        messages = self.conversation_store.load_messages(session_id, identity.subject_id)
        if messages and self.proposal_store is not None:
            changed = False
            for message in messages:
                action = message.payload.get("action")
                if not isinstance(action, dict):
                    continue
                proposal_id = action.get("proposal_id")
                if not isinstance(proposal_id, str):
                    action["status"] = "UNAVAILABLE"
                    continue
                proposal = self.proposal_store.get(proposal_id)
                if proposal is None or proposal.subject_id != identity.subject_id:
                    changed = self.conversation_store.update_proposal_state(
                        proposal_id,
                        "UNAVAILABLE",
                        identity.subject_id,
                        session_id=session_id,
                    ) or changed
                    continue
                terminal_status = {
                    ActionProposalStatus.REJECTED: "CANCELLED",
                    ActionProposalStatus.EXECUTED: "EXECUTED",
                    ActionProposalStatus.FAILED: "FAILED",
                    ActionProposalStatus.EXPIRED: "EXPIRED",
                }.get(proposal.status)
                if terminal_status is None:
                    continue
                identifier = None
                if proposal.result is not None:
                    value = (
                        proposal.result.data.get("issue_key")
                        or proposal.result.data.get("ticket_id")
                    )
                    if isinstance(value, str) and value.strip():
                        identifier = value.strip()
                changed = self.conversation_store.update_proposal_state(
                    proposal_id,
                    terminal_status,
                    identity.subject_id,
                    session_id=session_id,
                    external_identifier=identifier,
                ) or changed
            if changed:
                messages = self.conversation_store.load_messages(
                    session_id, identity.subject_id
                )
        if messages and any(item.role == "assistant" for item in messages):
            history_status = "complete"
        else:
            legacy_messages = self.conversation_store.load_legacy_user_messages(
                session_id, identity.subject_id
            )
            if messages:
                legacy_messages = messages
            messages = legacy_messages
            history_status = "partial" if messages else "unavailable"
        return {
            "history_status": history_status,
            "messages": [
                {"role": item.role, **item.payload}
                for item in messages
            ]
        }

    def record_conversation_exchange(
        self,
        session_id: str,
        identity_id: str,
        employee_message: str,
        response_payload: dict,
    ) -> None:
        self.record_conversation_request(session_id, identity_id, employee_message)
        self.record_conversation_response(session_id, identity_id, response_payload)

    def record_conversation_request(
        self,
        session_id: str,
        identity_id: str,
        employee_message: str,
    ) -> bool:
        identity = self._identity(identity_id)
        if identity is None:
            return False
        try:
            messages = self.conversation_store.load_messages(
                session_id, identity.subject_id
            )
            if (
                messages
                and messages[-1].role == "user"
                and messages[-1].payload.get("text") == employee_message
            ):
                return True
            self.conversation_store.append_message(
                session_id, "user", {"text": employee_message}, identity.subject_id
            )
            return True
        except Exception:
            return False

    def record_conversation_response(
        self,
        session_id: str,
        identity_id: str,
        response_payload: dict,
    ) -> None:
        identity = self._identity(identity_id)
        if identity is None:
            return
        try:
            proposal_update = response_payload.get("proposal_update")
            if isinstance(proposal_update, dict):
                proposal_id = proposal_update.get("proposal_id")
                status = proposal_update.get("status")
                ticket_id = proposal_update.get("ticket_id")
                if isinstance(proposal_id, str) and isinstance(status, str):
                    self.conversation_store.update_proposal_state(
                        proposal_id,
                        status,
                        identity.subject_id,
                        session_id=session_id,
                        external_identifier=(
                            ticket_id if isinstance(ticket_id, str) else None
                        ),
                    )
            self.conversation_store.append_message(
                session_id, "assistant", response_payload, identity.subject_id
            )
        except Exception:
            return

    def update_conversation(
        self,
        session_id: str,
        identity_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict | None:
        identity = self._identity(identity_id)
        if identity is None:
            return None
        item = self.conversation_store.update_metadata(
            session_id,
            identity.subject_id,
            title=title,
            pinned=pinned,
        )
        if item is None:
            return None
        return {
            "session_id": item.session_id,
            "title": item.title,
            "pinned": item.pinned,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    def delete_conversation(self, session_id: str, identity_id: str) -> bool:
        identity = self._identity(identity_id)
        if identity is None:
            return False
        key = (identity.subject_id, session_id)
        with self._conversation_request_lock:
            if key in self._conversation_requests:
                raise ConversationInFlightError(
                    "Conversation cannot be deleted while a request is active"
                )
            return self.conversation_store.delete(session_id, identity.subject_id)

    def identities(self) -> list[dict[str, str]]:
        provider = self.identity_provider
        list_identities = getattr(provider, "list_identities", None)
        records = list_identities() if callable(list_identities) else ()
        return [
            {"id": item.subject_id, "name": item.display_name}
            for item in records
            if item.authenticated
        ]

    def _identity(self, identity_id: str) -> IdentityContext | None:
        try:
            return self.identity_provider.resolve(identity_id)
        except (IdentityError, Exception):
            return None

    def _payload(
        self,
        response: EmployeeResponse,
        results: tuple[AgentResult, ...],
        transport: str,
        demo: bool,
        skills: tuple[str, ...] = (),
    ) -> dict:
        payload: dict[str, object] = {
            "kind": response.kind.value,
            "text": response.text,
            "sources": [
                {
                    "document": friendly_source_label(source).split(" — ", 1)[0],
                    "section": source.section,
                    "source": source.source,
                }
                for source in response.sources
            ],
        }
        if response.confirmation:
            proposal = next(
                (result.action_proposal for result in results if result.action_proposal), None
            )
            if proposal:
                payload["action"] = {
                    "status": "PROPOSED",
                    "proposal_id": response.confirmation.proposal_id,
                    "title": proposal.tool_arguments.get("title", ""),
                    "description": proposal.tool_arguments.get("description", ""),
                }
        else:
            cancelled = next(
                (
                    result.action_proposal
                    for result in results
                    if result.action_proposal is not None
                    and result.action_proposal.status.value == "REJECTED"
                ),
                None,
            )
            if cancelled is not None:
                payload["proposal_update"] = {
                    "proposal_id": cancelled.proposal_id,
                    "status": "CANCELLED",
                }
        ticket = self._ticket_payload(results)
        if ticket:
            payload["ticket"] = ticket
        if demo:
            decisions = [result.decision.decision.value for result in results]
            grounded = any(result.knowledge_result is not None for result in results)
            abstained = any(
                result.knowledge_result is not None
                and not result.knowledge_result.supporting_results
                and result.knowledge_result.evidence_decision is not None
                for result in results
            )
            payload["demo"] = {
                "skills": list(dict.fromkeys(skills)),
                "capabilities": decisions,
                "grounding": "abstained" if abstained else "grounded" if grounded else "not-applicable",
                "action_transport": transport,
                "authorization": "denied" if any(r.permission_denied for r in results) else "enforced",
            }
        return payload

    @staticmethod
    def _ticket_payload(results: tuple[AgentResult, ...]) -> dict | None:
        for result in reversed(results):
            if result.ticket_status_result:
                status = result.ticket_status_result
                return {"ticket_id": status.ticket_id, "status": status.status.value}
            if result.tool_result and result.tool_result.success:
                ticket_id = result.tool_result.data.get("issue_key") or result.tool_result.data.get("ticket_id")
                if isinstance(ticket_id, str):
                    return {
                        "ticket_id": ticket_id,
                        "status": str(result.tool_result.data.get("status", "OPEN")),
                    }
        return None

    @staticmethod
    def _unsupported() -> AgentResult:
        return AgentResult(
            AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="Request validation failed."),
            "The request could not be handled safely.",
        )

    def _denied_payload(self) -> dict:
        result = AgentResult(
            AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="Unknown identity."),
            "Permission denied.", permission_denied=True,
        )
        return self._payload(self.presenter.present_agent(result), (result,), "Unavailable", False)


def create_web_app(
    service: KnowviaWebService | None = None,
    authentication_runtime: AuthenticationRuntime | None = None,
    authentication_config: WebAuthenticationConfig | None = None,
    deployment_config: DeploymentConfig | None = None,
    runtime_paths: RuntimePaths | None = None,
    admin_authority: SqliteAuthority | None = None,
    knowledge_manager: KnowledgeManagement | None = None,
) -> Starlette:
    load_dotenv()
    deployment = deployment_config or DeploymentConfig.from_environment()
    paths = runtime_paths or RuntimePaths.from_environment()
    paths.ensure_writable()
    service_holder = {"value": service}
    auth_holder = {"value": authentication_runtime}
    authority_holder = {"value": admin_authority}
    knowledge_holder = {"value": knowledge_manager}
    embedding_holder = {"value": None}
    embedding_lock = Lock()
    login_guard = LoginAbuseGuard()

    def current_authority():
        if authority_holder["value"] is None:
            authority = SqliteAuthority(paths)
            if deployment.seed_demo_employee_directory:
                authority.seed_demo_once()
            authority_holder["value"] = authority
        return authority_holder["value"]

    def current_knowledge():
        if knowledge_holder["value"] is None:
            knowledge_holder["value"] = KnowledgeManagement(current_authority(), current_embedder)
        return knowledge_holder["value"]

    def current_embedder():
        # Administrative indexing and query retrieval share the configured
        # embedding runtime instead of loading a second resident model.
        with embedding_lock:
            if embedding_holder["value"] is None:
                embedding_holder["value"] = create_embedder(deployment.embedding_provider)
        return embedding_holder["value"]
    auth_config = authentication_config or WebAuthenticationConfig(
        secure_cookie=deployment.secure_cookie,
        allow_development_identity_override=deployment.development_identity_override,
    )
    if deployment.environment in {ApplicationEnvironment.DEMO, ApplicationEnvironment.PRODUCTION}:
        if not auth_config.secure_cookie:
            raise DeploymentConfigurationError("Secure authentication cookies are required in demo/production")
        if auth_config.allow_development_identity_override:
            raise DeploymentConfigurationError("Development identity override is forbidden in demo/production")

    def current_service() -> KnowviaWebService:
        if service_holder["value"] is None:
            service_holder["value"] = KnowviaWebService(
                ProductionRuntimeFactory(deployment, paths, current_knowledge(), current_embedder),
                identity_provider=current_authority(),
                conversation_store=SqliteConversationStore(paths.conversations),
                proposal_store=_action_services(paths)[0],
            )
        return service_holder["value"]

    def current_authentication() -> AuthenticationRuntime:
        if auth_holder["value"] is None:
            directory = SqliteEmployeeDirectory(paths.employee_directory)
            identity_provider = current_authority()
            bridge = EmployeeDirectoryIdentityService(directory, identity_provider)
            accounts = SqliteAccountRepository(paths.accounts)
            registration = RegistrationService(bridge, accounts)
            authentication = AuthenticationService(
                accounts,
                bridge,
                SqliteAuthenticatedSessionStore(paths.authenticated_sessions),
            )
            auth_holder["value"] = AuthenticationRuntime(registration, authentication)
        return auth_holder["value"]

    def authenticated_identity(request: Request, supplied_identity) -> str:
        token = request.cookies.get(auth_config.cookie_name)
        if token is not None:
            principal = current_authentication().authentication.resolve_principal(token)
            return principal.identity.subject_id
        if auth_config.allow_development_identity_override:
            return _bounded_id(supplied_identity, "identity_id")
        raise AuthenticationError("Authentication required")

    def account_payload(account) -> dict:
        return {
            "account_id": account.account_id,
            "employee_id": account.employee_id,
            "work_email": account.work_email,
            "created_at": account.created_at.isoformat(),
            "status": account.status.value,
        }

    def principal_payload(principal) -> dict:
        return {
            "account_id": principal.account.account_id,
            "employee_id": principal.account.employee_id,
            "work_email": principal.account.work_email,
            "display_name": principal.identity.display_name,
            "department": principal.department,
            "role": principal.organizational_role,
        }

    async def homepage(request: Request):
        return FileResponse(STATIC_ROOT / "index.html")

    async def admin_homepage(request: Request):
        # Public shell contains no administrative data; API authorization remains authoritative.
        return FileResponse(STATIC_ROOT / "admin.html", headers={"Cache-Control": "no-store"})

    async def config(request: Request):
        return JSONResponse({
            "identities": current_service().identities() if auth_config.allow_development_identity_override else [],
            "default_identity": os.getenv("KNOWVIA_IDENTITY", DEFAULT_DEVELOPMENT_IDENTITY) if auth_config.allow_development_identity_override else None,
            "demo_identity_notice": "Development selector only — not production authentication.",
            "development_identity_override": auth_config.allow_development_identity_override,
            "demo_registration_guidance": deployment.public_demo and deployment.seed_demo_employee_directory,
            "registration_enabled": deployment.registration_enabled,
            "developer_details_enabled": deployment.developer_details_enabled,
        })

    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    async def register(request: Request):
        if not deployment.registration_enabled:
            return JSONResponse({"error": "Registration is not available."}, status_code=404)
        try:
            body = await request.json()
            if set(body) != {"employee_id", "work_email", "password", "password_confirmation"}:
                raise RegistrationError("Registration could not be completed")
            result = await run_in_threadpool(
                current_authentication().registration.register,
                RegistrationRequest(body["employee_id"], body["work_email"], body["password"], body["password_confirmation"]),
            )
            return JSONResponse({"account": account_payload(result.account)}, status_code=201)
        except Exception:
            return JSONResponse({"error": "Registration could not be completed."}, status_code=400)

    async def login(request: Request):
        try:
            body = await request.json()
            if set(body) != {"work_email", "password"}:
                raise AuthenticationError("Authentication failed")
            if not login_guard.allow(request.client.host if request.client else "unknown", body["work_email"]):
                return JSONResponse({"error": "Authentication temporarily unavailable."}, status_code=429)
            result = await run_in_threadpool(
                current_authentication().authentication.login,
                LoginRequest(body["work_email"], body["password"]),
            )
            response = JSONResponse({"employee": principal_payload(result.principal)})
            response.set_cookie(
                auth_config.cookie_name,
                result.issued_session.token,
                max_age=int((result.issued_session.session.expires_at - result.issued_session.session.created_at).total_seconds()),
                expires=result.issued_session.session.expires_at,
                path="/",
                secure=auth_config.secure_cookie,
                httponly=True,
                samesite=auth_config.same_site,
            )
            return response
        except Exception:
            return JSONResponse({"error": "Authentication failed."}, status_code=401)

    async def logout(request: Request):
        token = request.cookies.get(auth_config.cookie_name)
        if token is not None:
            await run_in_threadpool(current_authentication().authentication.logout, token)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            auth_config.cookie_name,
            path="/",
            secure=auth_config.secure_cookie,
            httponly=True,
            samesite=auth_config.same_site,
        )
        return response

    async def me(request: Request):
        try:
            token = request.cookies.get(auth_config.cookie_name)
            if token is None:
                raise AuthenticationError("Authentication required")
            principal = await run_in_threadpool(
                current_authentication().authentication.resolve_principal, token
            )
            return JSONResponse({"employee": principal_payload(principal)})
        except Exception:
            return JSONResponse({"error": "Authentication required."}, status_code=401)

    async def admin_endpoint(request: Request):
        # No development override: administration always requires a real session.
        try:
            token = request.cookies.get(auth_config.cookie_name)
            if token is None: raise AuthenticationError("Authentication required")
            principal = await run_in_threadpool(current_authentication().authentication.resolve_principal, token)
        except Exception:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        authority = current_authority()
        actor = principal.identity.subject_id
        try:
            permission = "admin.audit.read" if request.url.path.endswith("/audit") else "admin.accounts.manage"
            if request.url.path.endswith("/roles"): permission = "admin.roles.manage"
            if request.url.path.startswith("/api/admin/knowledge"): permission = "admin.knowledge.manage"
            with authority.transaction() as db:
                authority._require(db, actor, permission)
        except (IdentityError, AdministrationError):
            return JSONResponse({"error": "Administrator permission required."}, status_code=403)
        if request.method == "GET":
            try:
                if request.url.path == "/api/admin/knowledge":
                    data = await run_in_threadpool(current_knowledge().listing, actor)
                elif request.url.path.endswith("/audit"):
                    data = {"events": await run_in_threadpool(authority.events, actor)}
                elif request.url.path.endswith("/accounts"):
                    data = {"accounts": await run_in_threadpool(authority.account_listing, actor)}
                elif request.url.path.endswith("/roles"):
                    data = {"roles": await run_in_threadpool(authority.role_catalog, actor)}
                else:
                    data = {"administrator": True, "employee_id": actor}
            except (IdentityError, AdministrationError):
                return JSONResponse({"error": "Administrator permission required."}, status_code=403, headers={"Cache-Control": "no-store"})
            except Exception:
                return JSONResponse({"error": "Administrative data is temporarily unavailable."}, status_code=503, headers={"Cache-Control": "no-store"})
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        # Strict same-origin mutations; Host is separately allowlisted by middleware.
        from urllib.parse import urlsplit
        origin = request.headers.get("origin", "")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return JSONResponse({"error": "Administrative request origin rejected."}, status_code=403)
        if (parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc
                or parsed.path or parsed.query or parsed.fragment
                or request.headers.get("content-type", "").split(";")[0].strip() != "application/json"):
            return JSONResponse({"error": "Administrative request origin rejected."}, status_code=403)
        try:
            if request.url.path.startswith("/api/admin/knowledge/"):
                # Bound the body before decoding; Markdown is data, never HTML.
                payload = bytearray()
                async for part in request.stream():
                    payload.extend(part)
                    if len(payload) > 800_000:
                        raise AdministrationError("Administrative upload too large")
                body = json.loads(payload)
                if not isinstance(body, dict): raise AdministrationError("Invalid request")
                operation = request.url.path.rsplit("/", 1)[1]
                manager = current_knowledge()
                if operation == "stage":
                    if set(body) != {"name", "content", "skill", "roles", "revision"}: raise AdministrationError("Invalid request")
                    await run_in_threadpool(manager.stage, actor, body["name"], body["content"], body["skill"], body["roles"], body["revision"])
                elif operation == "remove":
                    if set(body) != {"name", "revision"}: raise AdministrationError("Invalid request")
                    await run_in_threadpool(manager.remove, actor, body["name"], body["revision"])
                elif operation in {"publish", "rollback", "discard"}:
                    if set(body) != {"revision"}: raise AdministrationError("Invalid request")
                    await run_in_threadpool(getattr(manager, operation), actor, body["revision"])
                else:
                    raise AdministrationError("Invalid operation")
            else:
                body = await request.json()
            if request.url.path.startswith("/api/admin/knowledge/"):
                pass
            elif request.url.path.endswith("/activation"):
                if set(body) != {"active"}: raise AdministrationError("Invalid request")
                await run_in_threadpool(authority.set_active, actor, request.path_params["employee_id"], body["active"])
            elif request.url.path.endswith("/roles"):
                if set(body) != {"roles"}: raise AdministrationError("Invalid request")
                await run_in_threadpool(authority.assign_roles, actor, request.path_params["employee_id"], body["roles"])
            else:
                fields = {"employee_id", "first_name", "last_name", "work_email", "department", "role", "password", "password_confirmation", "roles"}
                if set(body) != fields: raise AdministrationError("Invalid request")
                employee = EmployeeRecord(*(body[k] for k in ("employee_id", "first_name", "last_name", "work_email", "department", "role")))
                await run_in_threadpool(authority.provision, actor, employee, body["password"], body["password_confirmation"], body["roles"])
            return JSONResponse({"updated": True})
        except Exception:
            return JSONResponse({"error": "Administrative change could not be completed."}, status_code=400)

    async def chat(request: Request):
        active_service = None
        session_id = None
        identity_id = None
        request_registered = False
        try:
            body = await request.json()
            session_id = _bounded_id(body.get("session_id"), "session_id")
            identity_id = authenticated_identity(request, body.get("identity_id"))
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                raise ValueError("Invalid message")
            active_service = current_service()
            begin = getattr(active_service, "begin_conversation_request", None)
            if callable(begin):
                request_registered = await run_in_threadpool(
                    begin, session_id, identity_id
                )
                if not request_registered:
                    return JSONResponse(
                        {
                            "kind": "CLARIFICATION",
                            "text": "That conversation is already processing a request.",
                            "sources": [],
                        },
                        status_code=409,
                    )
            request_recorder = getattr(
                active_service, "record_conversation_request", None
            )
            if callable(request_recorder):
                request_persisted = await run_in_threadpool(
                    request_recorder, session_id, identity_id, message.strip()
                )
                if request_persisted is False:
                    raise RuntimeError("Conversation message persistence failed")
            result = await run_in_threadpool(
                active_service.chat, session_id, message.strip(), identity_id,
                deployment.developer_details_enabled and bool(body.get("demo"))
            )
            response_recorder = getattr(
                active_service, "record_conversation_response", None
            )
            if callable(response_recorder):
                await run_in_threadpool(
                    response_recorder, session_id, identity_id, result
                )
            else:
                recorder = getattr(
                    active_service, "record_conversation_exchange", None
                )
                if callable(recorder):
                    await run_in_threadpool(
                        recorder, session_id, identity_id, message.strip(), result
                    )
            return JSONResponse(result)
        except AuthenticationError:
            return JSONResponse({"kind": "UNSUPPORTED", "text": "Authentication required.", "sources": []}, status_code=401)
        except Exception:
            return JSONResponse({"kind": "UNSUPPORTED", "text": "I couldn't safely process that request.", "sources": []}, status_code=400)
        finally:
            if request_registered and active_service is not None:
                end = getattr(active_service, "end_conversation_request", None)
                if callable(end):
                    await run_in_threadpool(end, session_id, identity_id)

    async def proposal(request: Request):
        active_service = None
        session_id = None
        identity_id = None
        request_registered = False
        try:
            body = await request.json()
            proposal_id = _bounded_id(request.path_params["proposal_id"], "proposal_id")
            identity_id = authenticated_identity(request, body.get("identity_id"))
            action = request.path_params["action"]
            if action not in {"confirm", "cancel"}:
                return JSONResponse({"kind": "UNSUPPORTED", "text": "That proposal action is not available.", "sources": []}, status_code=404)
            active_service = current_service()
            session_value = body.get("session_id")
            if session_value is not None:
                session_id = _bounded_id(session_value, "session_id")
                begin = getattr(active_service, "begin_conversation_request", None)
                if callable(begin):
                    request_registered = await run_in_threadpool(
                        begin, session_id, identity_id
                    )
                    if not request_registered:
                        return JSONResponse(
                            {
                                "kind": "CLARIFICATION",
                                "text": "That conversation is already processing a request.",
                                "sources": [],
                            },
                            status_code=409,
                        )
            operation = active_service.confirm if action == "confirm" else active_service.cancel
            result = await run_in_threadpool(
                operation, proposal_id, identity_id,
                deployment.developer_details_enabled and bool(body.get("demo")),
            )
            recorder = getattr(active_service, "record_conversation_response", None)
            if session_id is not None and callable(recorder):
                await run_in_threadpool(recorder, session_id, identity_id, result)
            return JSONResponse(result)
        except AuthenticationError:
            return JSONResponse({"kind": "UNSUPPORTED", "text": "Authentication required.", "sources": []}, status_code=401)
        except Exception:
            return JSONResponse({"kind": "UNSUPPORTED", "text": "I couldn't safely process that confirmation.", "sources": []}, status_code=400)
        finally:
            if request_registered and active_service is not None:
                end = getattr(active_service, "end_conversation_request", None)
                if callable(end):
                    await run_in_threadpool(end, session_id, identity_id)

    async def notifications(request: Request):
        try:
            identity_id = authenticated_identity(request, request.query_params.get("identity_id"))
            return JSONResponse(await run_in_threadpool(current_service().notifications, identity_id))
        except Exception:
            return JSONResponse({"notifications": []}, status_code=400)

    async def conversations(request: Request):
        try:
            identity_id = authenticated_identity(request, request.query_params.get("identity_id"))
            return JSONResponse(
                await run_in_threadpool(current_service().conversations, identity_id)
            )
        except Exception:
            return JSONResponse({"conversations": []}, status_code=400)

    async def accept_conversation_message(request: Request):
        try:
            session_id = _bounded_id(request.path_params["session_id"], "session_id")
            body = await request.json()
            identity_id = authenticated_identity(request, body.get("identity_id"))
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                raise ValueError("Invalid message")
            recorder = getattr(
                current_service(), "record_conversation_request", None
            )
            if not callable(recorder):
                raise RuntimeError("Conversation persistence unavailable")
            persisted = await run_in_threadpool(
                recorder, session_id, identity_id, message.strip()
            )
            if persisted is not True:
                raise RuntimeError("Conversation message persistence failed")
            return JSONResponse({"accepted": True}, status_code=201)
        except AuthenticationError:
            return JSONResponse({"error": "Authentication required."}, status_code=401)
        except Exception:
            return JSONResponse(
                {"error": "Conversation message could not be accepted."},
                status_code=400,
            )

    async def conversation_messages(request: Request):
        try:
            session_id = _bounded_id(request.path_params["session_id"], "session_id")
            identity_id = authenticated_identity(request, request.query_params.get("identity_id"))
            result = await run_in_threadpool(
                current_service().conversation_messages, session_id, identity_id
            )
            if result is None:
                return JSONResponse({"error": "Conversation not found."}, status_code=404)
            return JSONResponse(result)
        except Exception:
            return JSONResponse({"error": "Conversation could not be loaded."}, status_code=400)

    async def update_conversation(request: Request):
        try:
            session_id = _bounded_id(request.path_params["session_id"], "session_id")
            body = await request.json()
            identity_id = authenticated_identity(request, body.get("identity_id"))
            provided_title = "title" in body
            provided_pinned = "pinned" in body
            if provided_title == provided_pinned:
                raise ValueError("Exactly one metadata field is required")
            title = body.get("title") if provided_title else None
            pinned = body.get("pinned") if provided_pinned else None
            item = await run_in_threadpool(
                current_service().update_conversation,
                session_id,
                identity_id,
                title=title,
                pinned=pinned,
            )
            if item is None:
                return JSONResponse(
                    {"error": "Conversation not found."}, status_code=404
                )
            return JSONResponse({"conversation": item})
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid conversation update."}, status_code=400)
        except Exception:
            return JSONResponse({"error": "Conversation update failed."}, status_code=400)

    async def delete_conversation(request: Request):
        try:
            session_id = _bounded_id(request.path_params["session_id"], "session_id")
            identity_id = authenticated_identity(request, request.query_params.get("identity_id"))
            deleted = await run_in_threadpool(
                current_service().delete_conversation, session_id, identity_id
            )
            if not deleted:
                return JSONResponse(
                    {"error": "Conversation not found."}, status_code=404
                )
            return JSONResponse({"deleted": True})
        except ConversationInFlightError:
            return JSONResponse(
                {"error": "Wait for the conversation response before deleting it."},
                status_code=409,
            )
        except Exception:
            return JSONResponse({"error": "Conversation deletion failed."}, status_code=400)

    return Starlette(
        debug=deployment.debug_enabled,
        middleware=[
            Middleware(SecurityHeadersMiddleware, hsts_enabled=deployment.hsts_enabled),
            Middleware(TrustedHostMiddleware, allowed_hosts=list(deployment.allowed_hosts)),
        ],
        routes=[
            Route("/", homepage),
            Route("/admin", admin_homepage),
            Route("/health", health),
            Route("/api/config", config),
            Route("/api/auth/register", register, methods=["POST"]),
            Route("/api/auth/login", login, methods=["POST"]),
            Route("/api/auth/logout", logout, methods=["POST"]),
            Route("/api/auth/me", me),
            Route("/api/admin/me", admin_endpoint),
            Route("/api/admin/audit", admin_endpoint),
            Route("/api/admin/accounts", admin_endpoint, methods=["GET", "POST"]),
            Route("/api/admin/roles", admin_endpoint),
            Route("/api/admin/accounts/{employee_id:str}/activation", admin_endpoint, methods=["POST"]),
            Route("/api/admin/accounts/{employee_id:str}/roles", admin_endpoint, methods=["POST"]),
            Route("/api/admin/knowledge", admin_endpoint),
            Route("/api/admin/knowledge/{operation:str}", admin_endpoint, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/conversations", conversations),
            Route(
                "/api/conversations/{session_id:str}/messages",
                accept_conversation_message,
                methods=["POST"],
            ),
            Route(
                "/api/conversations/{session_id:str}",
                conversation_messages,
                methods=["GET"],
            ),
            Route(
                "/api/conversations/{session_id:str}",
                update_conversation,
                methods=["PATCH"],
            ),
            Route(
                "/api/conversations/{session_id:str}",
                delete_conversation,
                methods=["DELETE"],
            ),
            Route("/api/proposals/{proposal_id:str}/{action:str}", proposal, methods=["POST"]),
            Route("/api/notifications", notifications),
            Mount("/static", StaticFiles(directory=STATIC_ROOT), name="static"),
        ],
    )


def _bounded_id(value, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


app = create_web_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "knowvia.web:app",
        host=os.getenv("KNOWVIA_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("KNOWVIA_WEB_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
