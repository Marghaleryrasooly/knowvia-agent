from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from knowvia.answering import AnsweringService
from knowvia.evidence import EVIDENCE_RESPONSE_SCHEMA
from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.audit import JsonLinesAuditSink
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.conversation import (
    ConversationCoordinator,
    FollowUpResolver,
    SqliteConversationStore,
)
from knowvia.gemini_routing import (
    DEFAULT_GEMINI_ROUTER_MODEL,
    GeminiAgentRouter,
    GoogleGenAIRoutingTransport,
)
from knowvia.resilient_routing import ResilientAgentRouter, SafeFallbackPolicy
from knowvia.jira import JiraConfig, JiraTicketTool
from knowvia.mcp_integration import (
    MCPAgentTool,
    MCPIntegrationError,
    MCPTicketStatusProvider,
    OfficialMCPClientTransport,
)
from knowvia.multi_step import (
    DeterministicMultiStepPlanner,
    MultiStepCoordinator,
    safe_plan_failure_result,
    validate_plan,
)
from mcp import StdioServerParameters
from knowvia.embeddings import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_OPENAI_MODEL,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    OnnxEmbeddingProvider,
)
from knowvia.evidence import EvidenceEvaluator, LLMEvidenceEvaluator
from knowvia.generation import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    GenerationProvider,
    OllamaGenerationProvider,
    CloudflareGenerationProvider,
    DEFAULT_CLOUDFLARE_MODEL,
)
from knowvia.retrieval import Retriever, build_index
from knowvia.orchestrator import AgentOrchestrator
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.identity import IdentityError, LocalIdentityProvider
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.ticket_lifecycle import (
    JiraTicketStatusProvider,
    SqliteTicketLifecycleStore,
    StoredTicketStatusProvider,
    TicketLifecycleService,
)
from knowvia.skill_orchestration import SkillAwareAgent, SourceScopedRetriever
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.presentation import (
    DeterministicEmployeeResponsePresenter,
    EmployeeResponse,
    friendly_source_label,
)
from knowvia.vector_store import JsonVectorStore
from knowvia.runtime_paths import PROJECT_ROOT, RuntimePaths

load_dotenv()
DEFAULT_DOCUMENTS = PROJECT_ROOT / "knowvia_m1_dataset"
DEFAULT_RUNTIME_PATHS = RuntimePaths.from_environment()
DEFAULT_INDEX = DEFAULT_RUNTIME_PATHS.index
DEFAULT_PENDING_ACTIONS = DEFAULT_RUNTIME_PATHS.pending_actions
DEFAULT_AUDIT_LOG = DEFAULT_RUNTIME_PATHS.audit_log
DEFAULT_CONVERSATIONS = DEFAULT_RUNTIME_PATHS.conversations
DEFAULT_TICKET_LIFECYCLE = DEFAULT_RUNTIME_PATHS.ticket_lifecycle
DEFAULT_DEVELOPMENT_IDENTITY = "employee-001"


def create_embedder(provider: str) -> EmbeddingProvider:
    """Create the configured provider while keeping the pipeline provider-neutral."""
    if provider == "onnx":
        directory = os.getenv("KNOWVIA_ONNX_MODEL_DIR")
        if not directory:
            raise ValueError("KNOWVIA_ONNX_MODEL_DIR is required for ONNX embeddings")
        return OnnxEmbeddingProvider(Path(directory))
    if provider == "local":
        model = os.getenv("LOCAL_EMBEDDING_MODEL", DEFAULT_LOCAL_MODEL)
        return LocalEmbeddingProvider(model=model)
    if provider == "openai":
        model = os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_OPENAI_MODEL)
        return OpenAIEmbeddingProvider(model=model)
    raise ValueError(f"Unknown embedding provider: {provider}")


def create_generator(provider: str) -> GenerationProvider:
    """Create the configured generation provider."""
    if provider == "cloudflare":
        return _create_cloudflare_generator(evidence=False)
    if provider == "ollama":
        return OllamaGenerationProvider(
            model=os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL),
            host=os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        )
    raise ValueError(f"Unknown generation provider: {provider}")


def create_evidence_evaluator(provider: str, *, structured_output: bool = False) -> EvidenceEvaluator:
    """Create the fail-closed evidence evaluator used before answer generation."""
    if provider == "cloudflare":
        return LLMEvidenceEvaluator(_create_cloudflare_generator(evidence=True))
    if provider == "ollama":
        generator = OllamaGenerationProvider(
            model=os.getenv(
                "OLLAMA_EVIDENCE_MODEL",
                os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL),
            ),
            host=os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
            temperature=0.0,
            max_output_tokens=320,
            # Candidate remains opt-in until live semantic acceptance succeeds.
            response_schema=EVIDENCE_RESPONSE_SCHEMA if structured_output else None,
        )
        return LLMEvidenceEvaluator(generator)
    raise ValueError(f"Unknown evidence evaluator: {provider}")


def _create_cloudflare_generator(*, evidence: bool) -> CloudflareGenerationProvider:
    try:
        timeout = float(os.getenv("CLOUDFLARE_TIMEOUT_SECONDS", "120"))
    except ValueError:
        raise ValueError("Cloudflare timeout configuration is invalid") from None
    generation_model = os.getenv("CLOUDFLARE_GENERATION_MODEL", DEFAULT_CLOUDFLARE_MODEL)
    return CloudflareGenerationProvider(
        account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
        api_token=os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
        model=os.getenv("CLOUDFLARE_EVIDENCE_MODEL", generation_model) if evidence else generation_model,
        timeout=timeout, temperature=0.0 if evidence else 0.1,
        max_output_tokens=320 if evidence else 220,
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Knowvia: retrieve company evidence and generate grounded answers."
    )
    parser.add_argument(
        "--provider",
        choices=("local", "openai", "onnx"),
        default=os.getenv("KNOWVIA_EMBEDDING_PROVIDER", "local"),
        help="Embedding provider (default: local)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build the local vector index")
    index_parser.add_argument("--documents", type=Path, default=DEFAULT_DOCUMENTS)
    index_parser.add_argument("--index-file", type=Path, default=DEFAULT_INDEX)

    search_parser = subparsers.add_parser("search", help="Search the vector index")
    search_parser.add_argument("question", help="Natural-language employee question")
    search_parser.add_argument("--top-k", type=int, default=3)
    search_parser.add_argument("--index-file", type=Path, default=DEFAULT_INDEX)

    ask_parser = subparsers.add_parser(
        "ask", help="Retrieve evidence and generate a grounded answer"
    )
    ask_parser.add_argument("question", help="Natural-language employee question")
    ask_parser.add_argument("--top-k", type=int, default=3)
    ask_parser.add_argument("--index-file", type=Path, default=DEFAULT_INDEX)
    ask_parser.add_argument(
        "--generation-provider",
        choices=("ollama", "cloudflare"),
        default=os.getenv("KNOWVIA_GENERATION_PROVIDER", "ollama"),
        help="Generation provider (default: ollama)",
    )
    ask_parser.add_argument(
        "--evidence-evaluator",
        choices=("ollama", "cloudflare"),
        default=os.getenv("KNOWVIA_EVIDENCE_EVALUATOR", "ollama"),
        help="Evidence-sufficiency evaluator (default: ollama)",
    )
    ask_parser.add_argument(
        "--debug", action="store_true", help="Show structured developer-oriented output"
    )
    agent_parser = subparsers.add_parser(
        "agent", help="Route a request to knowledge, an action tool, or clarification"
    )
    agent_parser.add_argument("question", help="Employee request")
    agent_parser.add_argument("--top-k", type=int, default=3)
    agent_parser.add_argument("--index-file", type=Path, default=DEFAULT_INDEX)
    agent_parser.add_argument(
        "--generation-provider",
        choices=("ollama", "cloudflare"),
        default=os.getenv("KNOWVIA_GENERATION_PROVIDER", "ollama"),
    )
    agent_parser.add_argument(
        "--evidence-evaluator",
        choices=("ollama", "cloudflare"),
        default=os.getenv("KNOWVIA_EVIDENCE_EVALUATOR", "ollama"),
    )
    agent_parser.add_argument(
        "--router", choices=("deterministic", "gemini"),
        default=os.getenv("KNOWVIA_AGENT_ROUTER", "deterministic"),
    )
    agent_parser.add_argument(
        "--tool", choices=("mock", "jira", "mcp"),
        default=os.getenv("KNOWVIA_AGENT_TOOL", "mock"),
    )
    agent_parser.add_argument(
        "--debug", action="store_true", help="Show structured developer-oriented output"
    )
    agent_parser.add_argument(
        "--session",
        help="Optional conversation session ID for bounded knowledge follow-ups",
    )
    agent_parser.add_argument(
        "--identity",
        default=os.getenv("KNOWVIA_IDENTITY", DEFAULT_DEVELOPMENT_IDENTITY),
        help="Local development identity selector (not authentication)",
    )

    confirm_parser = subparsers.add_parser("confirm", help="Confirm one pending real action")
    confirm_parser.add_argument("proposal_id")
    confirm_parser.add_argument("--tool", choices=("jira", "mcp"), default="jira")
    confirm_parser.add_argument(
        "--identity",
        default=os.getenv("KNOWVIA_IDENTITY", DEFAULT_DEVELOPMENT_IDENTITY),
        help="Local development identity selector (not authentication)",
    )
    confirm_parser.add_argument(
        "--debug", action="store_true", help="Show structured developer-oriented output"
    )
    cancel_parser = subparsers.add_parser("cancel", help="Reject one pending real action")
    cancel_parser.add_argument("proposal_id")
    cancel_parser.add_argument("--tool", choices=("jira", "mcp"), default="jira")
    cancel_parser.add_argument(
        "--identity",
        default=os.getenv("KNOWVIA_IDENTITY", DEFAULT_DEVELOPMENT_IDENTITY),
        help="Local development identity selector (not authentication)",
    )
    cancel_parser.add_argument(
        "--debug", action="store_true", help="Show structured developer-oriented output"
    )
    notifications_parser = subparsers.add_parser(
        "notifications", help="List structured local ticket status notifications"
    )
    notifications_parser.add_argument(
        "--identity",
        default=os.getenv("KNOWVIA_IDENTITY", DEFAULT_DEVELOPMENT_IDENTITY),
        help="Local development identity selector (not authentication)",
    )
    return parser


def _mcp_transport(identity) -> OfficialMCPClientTransport:
    backend = os.getenv("KNOWVIA_MCP_TICKET_BACKEND", "mock").strip().lower()
    if backend not in {"mock", "jira"}:
        raise MCPIntegrationError("MCP ticket backend is invalid")
    environment: dict[str, str] = {
        "KNOWVIA_DATA_DIR": str(RuntimePaths.from_environment().data_dir),
    }
    if backend == "jira":
        for name in (
            "JIRA_BASE_URL",
            "JIRA_USER_EMAIL",
            "JIRA_API_TOKEN",
            "JIRA_PROJECT_KEY",
            "JIRA_ISSUE_TYPE",
            "JIRA_TIMEOUT_SECONDS",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "knowvia.mcp_server",
            "--backend",
            backend,
            "--identity",
            identity.subject_id,
        ],
        env=environment,
        cwd=PROJECT_ROOT,
    )
    return OfficialMCPClientTransport(
        parameters,
        timeout_seconds=float(os.getenv("KNOWVIA_MCP_TIMEOUT_SECONDS", "15")),
    )


def create_agent_tool(provider: str, identity=None, mcp_transport=None):
    if provider == "mock":
        return LocalITTicketTool()
    if provider == "jira":
        return JiraTicketTool(JiraConfig.from_environment())
    if provider == "mcp":
        if identity is None:
            raise MCPIntegrationError("MCP requires a trusted identity")
        transport = mcp_transport or _mcp_transport(identity)
        backend = os.getenv("KNOWVIA_MCP_TICKET_BACKEND", "mock").strip().lower()
        return MCPAgentTool(
            transport,
            "create_it_ticket",
            requires_confirmation=backend == "jira",
        )
    raise ValueError(f"Unknown agent tool provider: {provider}")


def create_agent_router(provider: str, tool):
    if provider == "deterministic":
        return DeterministicAgentRouter()
    if provider == "gemini":
        transport = GoogleGenAIRoutingTransport(
            model=os.getenv("GEMINI_ROUTER_MODEL", DEFAULT_GEMINI_ROUTER_MODEL)
        )
        specifications = (tool.specification,)
        return ResilientAgentRouter(
            GeminiAgentRouter(list(specifications), transport),
            DeterministicAgentRouter(),
            SafeFallbackPolicy(specifications),
        )
    raise ValueError(f"Unknown agent router: {provider}")


def _action_services(paths: RuntimePaths | None = None):
    selected = paths or RuntimePaths.from_environment()
    return (
        SqlitePendingActionStore(selected.pending_actions),
        JsonLinesAuditSink(selected.audit_log),
    )


def _ticket_lifecycle_service(tool_provider: str, audit, mcp_transport=None, paths: RuntimePaths | None = None):
    selected = paths or RuntimePaths.from_environment()
    store = SqliteTicketLifecycleStore(selected.ticket_lifecycle)
    if tool_provider == "mock":
        status_provider = StoredTicketStatusProvider()
    elif tool_provider == "jira":
        status_provider = JiraTicketStatusProvider(JiraConfig.from_environment())
    elif tool_provider == "mcp":
        if mcp_transport is None:
            raise MCPIntegrationError("MCP status transport is unavailable")
        status_provider = MCPTicketStatusProvider(mcp_transport)
    else:
        raise ValueError(f"Unknown agent tool provider: {tool_provider}")
    return TicketLifecycleService(store, status_provider, audit)


def _print_agent_result(result) -> None:
    print(f"\nDecision: {result.decision.decision.value}")
    if result.ticket_status_result:
        status = result.ticket_status_result
        print("Capability: TICKET_STATUS")
        print(f"Ticket: {status.ticket_id}")
        print(f"Status: {status.status.value}")
        print(f"Result: {status.message}")
        return
    if result.action_proposal:
        proposal = result.action_proposal
        print("Status: PROPOSED")
        print(f"Tool: {proposal.tool_name}")
        for name, value in proposal.tool_arguments.items():
            print(f"{name}: {value}")
        print(f"Proposal ID: {proposal.proposal_id}")
        print(f"Confirm: python -m knowvia.cli confirm {proposal.proposal_id}")
        return
    if result.decision.decision is AgentDecisionKind.KNOWLEDGE:
        if result.normalized_knowledge_request:
            normalized = result.normalized_knowledge_request
            print(f"Normalization: {normalized.normalization_kind.value}")
            print(f"Knowledge request: {normalized.normalized_request}")
        print("\nAnswer:")
        print(result.message)
        print("\nSources:")
        for source in result.knowledge_result.sources if result.knowledge_result else []:
            print(f"- {source.source} -> {source.section}")
    elif result.decision.decision is AgentDecisionKind.ACTION:
        print(f"Tool: {result.decision.tool_name}")
        print(f"Result: {result.message}")
        if result.tool_result:
            print(f"Success: {str(result.tool_result.success).lower()}")
            for name, value in result.tool_result.data.items():
                print(f"{name}: {value}")
    elif result.decision.decision is AgentDecisionKind.CLARIFY:
        print(f"Clarification: {result.message}")
    else:
        print(f"Result: {result.message}")


def _print_employee_response(response: EmployeeResponse) -> None:
    print(f"\n{response.text}")
    if response.sources:
        print("\nSources consulted:")
        for source in response.sources:
            print(f"- {friendly_source_label(source)}")
    if response.confirmation:
        proposal_id = response.confirmation.proposal_id
        print(f"\nTo confirm: python -m knowvia.cli confirm {proposal_id}")
        print(f"To cancel:  python -m knowvia.cli cancel {proposal_id}")


def _print_answer_debug(result) -> None:
    print("\nAnswer:")
    print(result.answer)
    print("\nSources:")
    for source in result.sources:
        print(f"- {source.source} -> {source.section}")


def main() -> None:
    load_dotenv()
    args = create_parser().parse_args()
    presenter = DeterministicEmployeeResponsePresenter()
    identity = None
    if args.command in {"agent", "confirm", "cancel", "notifications"}:
        try:
            identity = LocalIdentityProvider().resolve(args.identity)
        except IdentityError:
            denied = AgentResult(
                AgentDecision(
                    AgentDecisionKind.UNSUPPORTED,
                    reason="The local development identity could not be established.",
                ),
                "You don't have permission to access that capability.",
                permission_denied=True,
            )
            if args.debug:
                _print_agent_result(denied)
            else:
                _print_employee_response(presenter.present_agent(denied))
            return

    if args.command == "notifications":
        service = TicketLifecycleService(
            SqliteTicketLifecycleStore(DEFAULT_TICKET_LIFECYCLE),
            StoredTicketStatusProvider(),
        )
        events = service.notifications_for(identity)
        if not events:
            print("\nYou have no local ticket status notifications.")
        else:
            for event in events:
                print(f"\n{event.message}")
                if args.identity:
                    print(f"Event ID: {event.event_id}")
        return

    if args.command in {"confirm", "cancel"}:
        mcp_transport = _mcp_transport(identity) if args.tool == "mcp" else None
        try:
            tool = create_agent_tool(args.tool, identity, mcp_transport)
        except MCPIntegrationError:
            failure = AgentResult(
                AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="MCP discovery failed."),
                "The requested capability is currently unavailable.",
            )
            _print_agent_result(failure) if args.debug else _print_employee_response(presenter.present_agent(failure))
            return
        pending, audit = _action_services()
        lifecycle = _ticket_lifecycle_service(args.tool, audit, mcp_transport)
        permission_policy = IdentityPermissionPolicy()
        orchestrator = AgentOrchestrator(
            DeterministicAgentRouter(),
            object(),
            [tool],
            pending,
            authorizer=IdentityActionAuthorizer(
                identity,
                permission_policy,
                AllowRegisteredActionAuthorizer({tool.specification.name}),
            ),
            audit_sink=audit,
            identity_context=identity,
            ticket_lifecycle_recorder=lifecycle,
        )
        result = (
            orchestrator.confirm(args.proposal_id)
            if args.command == "confirm"
            else orchestrator.reject(args.proposal_id)
        )
        if args.debug:
            _print_agent_result(result)
        else:
            _print_employee_response(presenter.present_agent(result))
        return

    embedder = create_embedder(args.provider)
    store = JsonVectorStore(args.index_file)

    if args.command == "index":
        count = build_index(args.documents, embedder, store)
        print(f"Indexed {count} chunks into {args.index_file}")
        return

    retriever = Retriever(embedder, store)
    if args.command == "ask":
        answering_service = AnsweringService(
            retriever,
            create_generator(args.generation_provider),
            create_evidence_evaluator(args.evidence_evaluator),
        )
    if args.command == "ask":
        result = answering_service.answer(args.question, args.top_k)
        if args.debug:
            _print_answer_debug(result)
        else:
            _print_employee_response(presenter.present_answer(result))
        return

    if args.command == "agent":
        mcp_transport = _mcp_transport(identity) if args.tool == "mcp" else None
        try:
            tool = create_agent_tool(args.tool, identity, mcp_transport)
        except MCPIntegrationError:
            failure = AgentResult(
                AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="MCP discovery failed."),
                "The requested capability is currently unavailable.",
            )
            _print_agent_result(failure) if args.debug else _print_employee_response(presenter.present_agent(failure))
            return
        pending, audit = _action_services()
        lifecycle = _ticket_lifecycle_service(args.tool, audit, mcp_transport)
        def router_factory(skill, specifications):
            if args.router == "deterministic":
                return DeterministicAgentRouter()
            if args.router == "gemini":
                transport = GoogleGenAIRoutingTransport(
                    model=os.getenv("GEMINI_ROUTER_MODEL", DEFAULT_GEMINI_ROUTER_MODEL)
                )
                return ResilientAgentRouter(
                    GeminiAgentRouter(list(specifications), transport),
                    DeterministicAgentRouter(),
                    SafeFallbackPolicy(tuple(specifications)),
                )
            raise ValueError(f"Unknown agent router: {args.router}")

        def knowledge_factory(skill):
            return AnsweringService(
                SourceScopedRetriever(retriever, skill.knowledge_sources),
                create_generator(args.generation_provider),
                create_evidence_evaluator(args.evidence_evaluator),
            )

        registry = create_default_skill_registry()
        skill_selector = DeterministicSkillSelector()
        skill_agent = SkillAwareAgent(
            registry,
            skill_selector,
            router_factory,
            knowledge_factory,
            [tool],
            pending,
            audit_sink=audit,
            ticket_lifecycle_service=lifecycle,
        )
        planner = DeterministicMultiStepPlanner()
        raw_plan = planner.plan(args.question)
        try:
            planned = validate_plan(raw_plan)
        except Exception:
            planned = None
        if planned is None and len(raw_plan.steps) > 1:
            failure = safe_plan_failure_result()
            _print_agent_result(failure) if args.debug else _print_employee_response(
                presenter.present_agent(failure)
            )
            return
        if planned is not None and len(planned.steps) > 1:
            multi_result = MultiStepCoordinator(planner, skill_agent).handle(
                args.question, args.top_k, identity
            )
            if args.debug:
                print(f"\nIdentity: {identity.subject_id}")
                print("\nPlan:")
                for step, step_result in zip(
                    multi_result.plan.steps, multi_result.step_results
                ):
                    print(f"- {step.step_id}: {step.kind.value} — {step.request}")
                    print(f"  Skill: {step_result.selected_skill_id or 'none'}")
                    print(f"  Decision: {step_result.agent_result.decision.decision.value}")
                if not multi_result.completed:
                    print(f"Plan result: {multi_result.failure_reason or 'FAILED'}")
            response = presenter.present_multi_step(
                tuple(item.agent_result for item in multi_result.step_results)
            )
            _print_employee_response(response)
            return
        conversation_result = None
        if args.session:
            conversation_result = ConversationCoordinator(
                SqliteConversationStore(DEFAULT_CONVERSATIONS),
                FollowUpResolver(skill_selector),
                registry.discover(),
                skill_agent,
                presenter,
            ).handle(args.session, args.question, args.top_k, identity=identity)
            skill_result = conversation_result.skill_result
            result = conversation_result.agent_result
        else:
            skill_result = skill_agent.handle(
                args.question, args.top_k, identity=identity
            )
            result = skill_result.agent_result
        if args.debug:
            print(f"\nIdentity: {identity.subject_id}")
            selected_skill = skill_result.selected_skill_id if skill_result else None
            print(f"\nSkill: {selected_skill or 'none'}")
            if conversation_result:
                print(f"Resolved request: {conversation_result.resolved_request.standalone_request}")
            _print_agent_result(result)
        else:
            response = conversation_result.response if conversation_result else presenter.present_agent(result)
            _print_employee_response(response)
        return

    results = retriever.search(args.question, args.top_k)
    for rank, result in enumerate(results, start=1):
        print(f"\nRank: {rank}")
        print(f"Similarity: {result.score:.4f}")
        print(f"Source: {result.chunk.source}")
        print(f"Section: {result.chunk.section}")
        print("Text:")
        print(result.chunk.text)


if __name__ == "__main__":
    main()
