from dataclasses import replace
from datetime import datetime, timezone

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.identity import LocalIdentityProvider
from knowvia.jira import HttpResponse, JiraConfig
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.presentation import DeterministicEmployeeResponsePresenter, EmployeeResponseKind
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.ticket_lifecycle import (
    JiraTicketStatusProvider,
    SqliteTicketLifecycleStore,
    TicketLifecycleService,
    TicketRecord,
    TicketStatus,
    TicketStatusObservation,
)


class Provider:
    def __init__(self, status=TicketStatus.OPEN):
        self.status = status
        self.calls = []
    def get_status(self, ticket):
        self.calls.append(ticket)
        return TicketStatusObservation(True, self.status, ticket.external_reference)


class Knowledge:
    def __init__(self): self.calls = []
    def answer(self, question, top_k=3):
        self.calls.append(question)
        return AnswerResult("knowledge", [], [])


def identity(subject="employee-001"):
    return LocalIdentityProvider().resolve(subject)


def register(store, owner="employee-001", ticket_id="KIT-15"):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return store.register(
        TicketRecord(
            ticket_id,
            owner,
            TicketStatus.OPEN,
            "jira",
            ticket_id,
            now,
            now,
        )
    )


def test_created_ticket_is_bound_to_owner_through_existing_execution(tmp_path) -> None:
    user = identity()
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")
    lifecycle = TicketLifecycleService(store, Provider())
    tool = LocalITTicketTool()
    orchestrator = AgentOrchestrator(
        DeterministicAgentRouter(),
        Knowledge(),
        [tool],
        authorizer=IdentityActionAuthorizer(
            user,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=user,
        ticket_lifecycle_recorder=lifecycle,
    )

    result = orchestrator.handle("Create an IT ticket for my VPN problem.")
    record = store.get(result.tool_result.data["ticket_id"])

    assert result.tool_result.success
    assert record and record.owner_subject_id == "employee-001"
    assert record.status is TicketStatus.OPEN
    assert record.external_system == "local-mock"


def test_owner_can_check_status_but_another_identity_cannot(tmp_path) -> None:
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")
    register(store)
    provider = Provider(TicketStatus.OPEN)
    service = TicketLifecycleService(store, provider)

    own = service.lookup("KIT-15", identity())
    other = replace(identity(), subject_id="employee-other")
    denied = service.lookup("KIT-15", other)

    assert own.success and own.status is TicketStatus.OPEN
    assert denied.permission_denied is True
    assert len(provider.calls) == 1


def test_unknown_ticket_fails_before_provider_lookup(tmp_path) -> None:
    provider = Provider()
    service = TicketLifecycleService(
        SqliteTicketLifecycleStore(tmp_path / "tickets.db"), provider
    )

    result = service.lookup("KIT-404", identity())

    assert result.success is False
    assert result.error_category == "NOT_FOUND"
    assert provider.calls == []


def test_status_transitions_create_notifications_once(tmp_path) -> None:
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")
    register(store)
    provider = Provider(TicketStatus.IN_PROGRESS)
    service = TicketLifecycleService(store, provider)

    in_progress = service.lookup("KIT-15", identity())
    duplicate = service.lookup("KIT-15", identity())
    provider.status = TicketStatus.RESOLVED
    resolved = service.lookup("KIT-15", identity())

    assert in_progress.status is TicketStatus.IN_PROGRESS
    assert in_progress.notification
    assert duplicate.notification is None
    assert resolved.status is TicketStatus.RESOLVED
    assert resolved.notification
    events = store.notifications_for("employee-001")
    assert [event.new_status for event in events] == [
        TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED,
    ]
    assert events[-1].message == "Your IT ticket KIT-15 has been resolved."


def test_status_request_bypasses_model_and_uses_trusted_provider(tmp_path) -> None:
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")
    register(store)
    provider = Provider(TicketStatus.RESOLVED)
    service = TicketLifecycleService(store, provider)
    router_calls = []
    knowledge = Knowledge()

    def router_factory(skill, specifications):
        router_calls.append(True)
        raise AssertionError("Gemini/router must not decide trusted ticket status")

    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        router_factory,
        lambda skill: knowledge,
        [LocalITTicketTool()],
        ticket_lifecycle_service=service,
    )
    result = agent.handle(
        "What is the status of my ticket KIT-15?", identity=identity()
    )
    response = DeterministicEmployeeResponsePresenter().present_agent(
        result.agent_result
    )

    assert result.selected_skill_id == "it_support"
    assert result.agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert result.agent_result.ticket_status_result.status is TicketStatus.RESOLVED
    assert response.kind is EmployeeResponseKind.TICKET_STATUS
    assert "KIT-15" in response.text and "resolved" in response.text
    assert router_calls == [] and knowledge.calls == []


def test_identity_without_status_permission_is_denied_before_provider(tmp_path) -> None:
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")
    register(store, owner="employee-002")
    provider = Provider()
    service = TicketLifecycleService(store, provider)

    result = service.lookup("KIT-15", identity("employee-002"))

    assert result.permission_denied is True
    assert provider.calls == []


class JiraStatusTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.calls += 1
        assert url.endswith("/rest/api/3/issue/KIT-15?fields=status")
        assert headers["Accept"] == "application/json"
        assert timeout == 4.0
        return self.response


def jira_record() -> TicketRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TicketRecord("KIT-15", "employee-001", TicketStatus.OPEN, "jira", "KIT-15", now, now)


def test_jira_status_provider_maps_read_only_status_without_exposing_credentials() -> None:
    transport = JiraStatusTransport(
        HttpResponse(200, '{"fields":{"status":{"name":"In Progress"}}}')
    )
    provider = JiraTicketStatusProvider(
        JiraConfig("https://example.atlassian.net", "employee@example.com", "secret-token", "KIT", "Task", 4.0),
        transport,
    )

    observation = provider.get_status(jira_record())

    assert observation.success is True
    assert observation.status is TicketStatus.IN_PROGRESS
    assert transport.calls == 1
    assert "secret-token" not in repr(observation)


def test_jira_status_provider_maps_permission_failure_safely() -> None:
    transport = JiraStatusTransport(HttpResponse(403, "sensitive provider text"))
    provider = JiraTicketStatusProvider(
        JiraConfig("https://example.atlassian.net", "employee@example.com", "secret-token", "KIT", "Task", 4.0),
        transport,
    )

    observation = provider.get_status(jira_record())

    assert observation.success is False
    assert observation.status is TicketStatus.UNKNOWN
    assert observation.error_category == "PERMISSION"
    assert "sensitive provider text" not in repr(observation)
