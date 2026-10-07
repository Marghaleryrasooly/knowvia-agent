import pytest

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_routing import AgentRoutingError
from knowvia.agent_tools import LocalITTicketTool
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.gemini_routing import (
    FunctionProposal,
    GeminiAgentRouter,
    GeminiRoutingError,
    GeminiRoutingFailureCategory,
    GoogleGenAIRoutingTransport,
)


class FakeTransport:
    def __init__(self, proposals=None, error=None):
        self.proposals, self.error, self.calls = proposals or [], error, []

    def propose(self, request, declarations):
        self.calls.append((request, declarations))
        if self.error:
            raise self.error
        return self.proposals


def router_for(proposal):
    return GeminiAgentRouter([LocalITTicketTool.specification], FakeTransport([proposal]))


def test_gemini_knowledge_decision() -> None:
    decision = router_for(FunctionProposal("route_to_company_knowledge", {})).route("What is leave policy?")
    assert decision.decision is AgentDecisionKind.KNOWLEDGE
    assert decision.tool_name is None


def test_gemini_action_is_structured_but_not_executed() -> None:
    decision = router_for(FunctionProposal("create_it_ticket", {"title": "VPN", "description": "VPN fails"})).route("Create a ticket")
    assert decision.decision is AgentDecisionKind.ACTION
    assert decision.tool_arguments == {"title": "VPN", "description": "VPN fails"}


def test_missing_argument_becomes_clarification() -> None:
    decision = router_for(FunctionProposal("create_it_ticket", {"title": "VPN"})).route("Create a ticket")
    assert decision.decision is AgentDecisionKind.CLARIFY


@pytest.mark.parametrize("proposal", [
    FunctionProposal("unknown_tool", {}),
    FunctionProposal("create_it_ticket", {"title": "x", "description": 4}),
    FunctionProposal("route_to_company_knowledge", {"extra": "x"}),
])
def test_invalid_model_proposals_fail_closed(proposal) -> None:
    with pytest.raises(AgentRoutingError):
        router_for(proposal).route("request")


def test_provider_failure_and_multiple_calls_fail_closed() -> None:
    failing = GeminiAgentRouter([LocalITTicketTool.specification], FakeTransport(error=RuntimeError("key-secret")))
    with pytest.raises(GeminiRoutingError, match="provider failed") as caught:
        failing.route("request")
    assert "key-secret" not in str(caught.value)
    assert caught.value.category is GeminiRoutingFailureCategory.PROVIDER_ERROR
    multiple = GeminiAgentRouter(
        [LocalITTicketTool.specification],
        FakeTransport([FunctionProposal("route_to_company_knowledge", {}), FunctionProposal("route_unsupported_action", {})]),
    )
    with pytest.raises(GeminiRoutingError, match="exactly one") as multiple_error:
        multiple.route("request")
    assert multiple_error.value.category is GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR


def test_provider_timeout_fails_closed_with_sanitized_category() -> None:
    router = GeminiAgentRouter(
        [LocalITTicketTool.specification],
        FakeTransport(error=TimeoutError("secret endpoint and token")),
    )

    with pytest.raises(GeminiRoutingError, match="timed out") as caught:
        router.route("Create a ticket")

    assert caught.value.category is GeminiRoutingFailureCategory.PROVIDER_TIMEOUT
    assert "secret" not in str(caught.value)


def test_provider_http_failure_has_sanitized_category() -> None:
    class FakeProviderHttpError(Exception):
        status_code = 503

    router = GeminiAgentRouter(
        [LocalITTicketTool.specification],
        FakeTransport(error=FakeProviderHttpError("secret response body")),
    )

    with pytest.raises(GeminiRoutingError, match="HTTP error") as caught:
        router.route("Create a ticket")

    assert caught.value.category is GeminiRoutingFailureCategory.PROVIDER_HTTP_ERROR
    assert "secret" not in str(caught.value)


def test_transport_configures_bounded_sdk_timeout(monkeypatch) -> None:
    from google import genai

    captured = {}

    class FakeClient:
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(genai, "Client", fake_client)
    transport = GoogleGenAIRoutingTransport(api_key="fake-key", timeout_seconds=12.5)

    assert transport.timeout_seconds == 12.5
    assert captured["http_options"] == {"timeout": 12_500}
    assert captured["api_key"] == "fake-key"


class FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class FakeGenerateContentResponse:
    def __init__(self, function_calls=None):
        self.function_calls = function_calls


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class SequencedModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_google_transport(models):
    transport = object.__new__(GoogleGenAIRoutingTransport)
    transport._client = type("FakeClient", (), {"models": models})()
    transport.model = "gemini-test"
    transport.timeout_seconds = 30.0
    transport.last_diagnostic = None
    return transport


class FakeHttpError(Exception):
    def __init__(self, status_code):
        super().__init__("secret provider response")
        self.status_code = status_code


class ConnectError(Exception):
    pass


def test_generate_content_transport_returns_function_proposals() -> None:
    models = FakeModels(FakeGenerateContentResponse([
        FakeFunctionCall("create_it_ticket", {"title": "VPN", "description": "VPN fails"})
    ]))
    transport = fake_google_transport(models)
    declarations = GeminiAgentRouter(
        [LocalITTicketTool.specification], transport
    )._declarations()

    proposals = transport.propose("Create a ticket", declarations)

    assert proposals == [
        FunctionProposal("create_it_ticket", {"title": "VPN", "description": "VPN fails"})
    ]
    request = models.calls[0]
    assert request["contents"] == "Create a ticket"
    assert request["config"]["tool_config"]["function_calling_config"]["mode"] == "ANY"
    sent_declarations = request["config"]["tools"][0]["function_declarations"]
    assert all("type" not in declaration for declaration in sent_declarations)
    assert all("parameters" not in declaration for declaration in sent_declarations)
    assert all("parameters_json_schema" in declaration for declaration in sent_declarations)
    assert {declaration["name"] for declaration in sent_declarations} == {
        "route_to_company_knowledge",
        "request_clarification",
        "route_unsupported_action",
        "create_it_ticket",
    }
    assert transport.last_diagnostic.http_status == 200
    assert transport.last_diagnostic.exception_class is None
    assert transport.last_diagnostic.attempt_count == 1


def test_transient_failure_retries_once_then_succeeds(monkeypatch) -> None:
    models = SequencedModels([
        ConnectError("secret connection detail"),
        FakeGenerateContentResponse([FakeFunctionCall("route_to_company_knowledge", {})]),
    ])
    transport = fake_google_transport(models)
    monkeypatch.setattr("knowvia.gemini_routing.time.sleep", lambda _: None)

    proposals = transport.propose(
        "What is the leave policy?",
        GeminiAgentRouter([], transport)._declarations(),
    )

    assert proposals == [FunctionProposal("route_to_company_knowledge", {})]
    assert len(models.calls) == 2
    assert transport.last_diagnostic.attempt_count == 2


def test_routing_retry_never_retries_tool_execution(monkeypatch) -> None:
    models = SequencedModels([
        FakeHttpError(499),
        FakeGenerateContentResponse([
            FakeFunctionCall(
                "create_it_ticket",
                {"title": "VPN", "description": "VPN is unavailable"},
            )
        ]),
    ])
    transport = fake_google_transport(models)
    monkeypatch.setattr("knowvia.gemini_routing.time.sleep", lambda _: None)

    class Knowledge:
        def answer(self, question, top_k=3):
            return AnswerResult("unused", [], [])

    class CountingTool(LocalITTicketTool):
        def __init__(self):
            super().__init__()
            self.executions = 0

        def execute(self, arguments):
            self.executions += 1
            return super().execute(arguments)

    tool = CountingTool()
    router = GeminiAgentRouter([tool.specification], transport)
    result = AgentOrchestrator(router, Knowledge(), [tool]).handle(
        "Create an IT ticket because the VPN is unavailable."
    )

    assert len(models.calls) == 2
    assert tool.executions == 1
    assert result.tool_executed is True


def test_two_transient_failures_stop_after_one_retry(monkeypatch) -> None:
    models = SequencedModels([FakeHttpError(504), FakeHttpError(503)])
    transport = fake_google_transport(models)
    monkeypatch.setattr("knowvia.gemini_routing.time.sleep", lambda _: None)

    with pytest.raises(FakeHttpError):
        transport.propose("fictional request", GeminiAgentRouter([], transport)._declarations())

    assert len(models.calls) == 2
    assert transport.last_diagnostic.attempt_count == 2
    assert transport.last_diagnostic.http_status == 503


def test_non_transient_failure_is_not_retried(monkeypatch) -> None:
    models = SequencedModels([FakeHttpError(401), AssertionError("must not retry")])
    transport = fake_google_transport(models)
    monkeypatch.setattr("knowvia.gemini_routing.time.sleep", lambda _: None)

    with pytest.raises(FakeHttpError):
        transport.propose("fictional request", GeminiAgentRouter([], transport)._declarations())

    assert len(models.calls) == 1
    assert transport.last_diagnostic.attempt_count == 1


def test_generate_content_transport_allows_router_to_reject_no_function_call() -> None:
    transport = fake_google_transport(FakeModels(FakeGenerateContentResponse(None)))
    router = GeminiAgentRouter([LocalITTicketTool.specification], transport)

    with pytest.raises(GeminiRoutingError, match="exactly one") as caught:
        router.route("Create a ticket")

    assert caught.value.category is GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR


@pytest.mark.parametrize(
    ("arguments", "expected_kind"),
    [
        ({"title": "VPN"}, AgentDecisionKind.CLARIFY),
        ({"title": "VPN", "description": 4}, None),
        ({"title": "VPN", "description": "fails", "extra": "bad"}, None),
    ],
)
def test_generate_content_transport_preserves_argument_validation(arguments, expected_kind) -> None:
    transport = fake_google_transport(FakeModels(FakeGenerateContentResponse([
        FakeFunctionCall("create_it_ticket", arguments)
    ])))
    router = GeminiAgentRouter([LocalITTicketTool.specification], transport)

    if expected_kind is None:
        with pytest.raises(GeminiRoutingError) as caught:
            router.route("Create a ticket")
        assert caught.value.category is GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR
    else:
        assert router.route("Create a ticket").decision is expected_kind


@pytest.mark.parametrize(
    ("provider_error", "category"),
    [
        (TimeoutError("secret timeout detail"), GeminiRoutingFailureCategory.PROVIDER_TIMEOUT),
        (RuntimeError("secret provider detail"), GeminiRoutingFailureCategory.PROVIDER_ERROR),
    ],
)
def test_generate_content_transport_preserves_provider_failure_categories(provider_error, category) -> None:
    transport = fake_google_transport(FakeModels(error=provider_error))
    router = GeminiAgentRouter([LocalITTicketTool.specification], transport)

    with pytest.raises(GeminiRoutingError) as caught:
        router.route("Create a ticket")

    assert caught.value.category is category
    assert "secret" not in str(caught.value)


def test_provider_diagnostic_retains_only_sanitized_metadata() -> None:
    class FakeResponse:
        status_code = 429

    class FakeRateLimitError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        response = FakeResponse()

    transport = fake_google_transport(FakeModels(error=FakeRateLimitError("secret body")))

    with pytest.raises(FakeRateLimitError):
        transport.propose("fictional request", [])

    diagnostic = transport.last_diagnostic
    assert diagnostic.http_status == 429
    assert diagnostic.structured_status == "RESOURCE_EXHAUSTED"
    assert diagnostic.exception_class == "FakeRateLimitError"
    assert diagnostic.response_received is True
    assert diagnostic.attempt_count == 1
    assert diagnostic.elapsed_seconds >= 0
    assert "secret" not in repr(diagnostic)
    assert "fictional request" not in repr(diagnostic)


def test_only_original_request_and_public_schemas_are_sent() -> None:
    transport = FakeTransport([FunctionProposal("route_to_company_knowledge", {})])
    GeminiAgentRouter([LocalITTicketTool.specification], transport).route("original employee request")
    request, declarations = transport.calls[0]
    serialized = repr(declarations)
    assert request == "original employee request"
    assert "API_TOKEN" not in serialized and "Authorization" not in serialized
    assert "create_it_ticket" in serialized
