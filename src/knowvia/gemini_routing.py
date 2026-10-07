from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import AgentRoutingError
from knowvia.agent_tools import ToolSpecification

DEFAULT_GEMINI_ROUTER_MODEL = "gemini-3.7-flash"
DEFAULT_GEMINI_ROUTER_TIMEOUT_SECONDS = 30.0
GEMINI_ROUTER_MAX_ATTEMPTS = 2
GEMINI_ROUTER_RETRY_BACKOFF_SECONDS = 0.25
_TRANSIENT_HTTP_STATUSES = frozenset({499, 503, 504})


class GeminiRoutingFailureCategory(str, Enum):
    """Sanitized failure categories safe to retain in internal diagnostics."""

    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROPOSAL_VALIDATION_ERROR = "PROPOSAL_VALIDATION_ERROR"


class GeminiRoutingError(AgentRoutingError):
    """A fail-closed Gemini routing error without provider response details."""

    def __init__(
        self,
        category: GeminiRoutingFailureCategory,
        message: str,
        *,
        http_status: int | None = None,
        attempt_count: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.attempt_count = attempt_count
        self.transient = transient


@dataclass(frozen=True)
class FunctionProposal:
    name: str
    arguments: object


@dataclass(frozen=True)
class GeminiProviderDiagnostic:
    """Sanitized provider-boundary metadata for development diagnostics."""

    exception_class: str | None
    http_status: int | None
    structured_status: str | None
    response_received: bool
    attempt_count: int
    elapsed_seconds: float


class GeminiRoutingTransport(Protocol):
    def propose(self, employee_request: str, declarations: list[dict[str, Any]]) -> list[FunctionProposal]: ...


class GoogleGenAIRoutingTransport:
    """Official SDK adapter. It returns proposals and never executes functions."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_GEMINI_ROUTER_MODEL,
        timeout_seconds: float | None = None,
    ) -> None:
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY is required for the Gemini router")
        if timeout_seconds is None:
            timeout_seconds = float(
                os.getenv(
                    "GEMINI_ROUTER_TIMEOUT_SECONDS",
                    str(DEFAULT_GEMINI_ROUTER_TIMEOUT_SECONDS),
                )
            )
        if timeout_seconds <= 0:
            raise ValueError("Gemini routing timeout must be positive")
        try:
            from google import genai
        except ImportError as error:
            raise RuntimeError("Install the optional Gemini dependency: pip install -e .[gemini]") from error
        self._client = genai.Client(
            api_key=key,
            http_options={"timeout": int(timeout_seconds * 1000)},
        )
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.last_diagnostic: GeminiProviderDiagnostic | None = None

    def propose(self, employee_request: str, declarations: list[dict[str, Any]]) -> list[FunctionProposal]:
        function_declarations = []
        for declaration in declarations:
            function_declaration = {
                key: value
                for key, value in declaration.items()
                if key not in {"type", "parameters"}
            }
            function_declaration["parameters_json_schema"] = declaration["parameters"]
            function_declarations.append(function_declaration)
        started = time.monotonic()
        response = None
        for attempt in range(1, GEMINI_ROUTER_MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=employee_request,
                    config={
                        "tools": [{"function_declarations": function_declarations}],
                        "tool_config": {
                            "function_calling_config": {
                                "mode": "ANY",
                                "allowed_function_names": [
                                    declaration["name"] for declaration in function_declarations
                                ],
                            }
                        },
                    },
                )
                break
            except Exception as error:
                provider_response = getattr(error, "response", None)
                status = getattr(provider_response, "status_code", None)
                if not isinstance(status, int):
                    candidate = getattr(error, "code", getattr(error, "status_code", None))
                    status = candidate if isinstance(candidate, int) else None
                structured = getattr(error, "status", None)
                self.last_diagnostic = GeminiProviderDiagnostic(
                    exception_class=type(error).__name__,
                    http_status=status,
                    structured_status=structured if isinstance(structured, str) else None,
                    response_received=provider_response is not None,
                    attempt_count=attempt,
                    elapsed_seconds=time.monotonic() - started,
                )
                if attempt >= GEMINI_ROUTER_MAX_ATTEMPTS or not self._is_transient(error, status):
                    raise
                time.sleep(GEMINI_ROUTER_RETRY_BACKOFF_SECONDS)
        self.last_diagnostic = GeminiProviderDiagnostic(
            exception_class=None,
            http_status=200,
            structured_status="OK",
            response_received=True,
            attempt_count=attempt,
            elapsed_seconds=time.monotonic() - started,
        )
        return [
            FunctionProposal(call.name, call.args)
            for call in (response.function_calls or [])
        ]

    @staticmethod
    def _is_transient(error: Exception, status: int | None) -> bool:
        class_names = {kind.__name__ for kind in type(error).__mro__}
        return "ConnectError" in class_names or status in _TRANSIENT_HTTP_STATUSES


class GeminiAgentRouter:
    """Maps one Gemini function-call proposal into a validated decision shape."""

    def __init__(
        self,
        tool_specifications: list[ToolSpecification],
        transport: GeminiRoutingTransport,
    ) -> None:
        self.specifications = {spec.name: spec for spec in tool_specifications}
        if len(self.specifications) != len(tool_specifications):
            raise ValueError("Tool names must be unique")
        self.transport = transport

    def route(self, employee_request: str) -> AgentDecision:
        request = employee_request.strip()
        if not request:
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question="What would you like Knowvia to help with?",
                reason="The employee request is empty.",
            )
        try:
            proposals = self.transport.propose(request, self._declarations())
        except GeminiRoutingError:
            raise
        except Exception as error:
            raise self._provider_error(
                error, getattr(self.transport, "last_diagnostic", None)
            ) from None
        if len(proposals) != 1:
            raise self._proposal_error("Gemini must return exactly one function call")
        proposal = proposals[0]
        if not isinstance(proposal.name, str) or not isinstance(proposal.arguments, dict):
            raise self._proposal_error("Gemini returned a malformed function call")

        if proposal.name == "route_to_company_knowledge":
            if proposal.arguments:
                raise self._proposal_error("Knowledge route must not contain arguments")
            return AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="Gemini selected the company knowledge capability.")
        if proposal.name == "request_clarification":
            question = proposal.arguments.get("clarification_question")
            if set(proposal.arguments) != {"clarification_question"} or not isinstance(question, str) or not question.strip():
                raise self._proposal_error("Gemini clarification is invalid")
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question=question.strip(),
                reason="Gemini identified missing information for an action.",
            )
        if proposal.name == "route_unsupported_action":
            if proposal.arguments:
                raise self._proposal_error("Unsupported route must not contain arguments")
            return AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="Gemini selected an unsupported action route.")
        specification = self.specifications.get(proposal.name)
        if specification is None:
            raise self._proposal_error("Gemini selected an unknown tool")
        arguments = self._validate_schema(proposal.arguments, specification)
        if arguments is None:
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                tool_name=specification.name,
                clarification_question="Please provide all information required for this action.",
                reason="Gemini omitted required tool arguments.",
            )
        return AgentDecision(
            AgentDecisionKind.ACTION,
            tool_name=specification.name,
            tool_arguments=arguments,
            reason="Gemini proposed a registered tool with structured arguments.",
        )

    @staticmethod
    def _validate_schema(arguments: dict[str, object], specification: ToolSpecification) -> dict[str, str] | None:
        expected = set(specification.required_arguments)
        if not expected.issubset(arguments):
            return None
        if set(arguments) != expected:
            raise GeminiAgentRouter._proposal_error("Gemini returned unknown tool arguments")
        values: dict[str, str] = {}
        for name in specification.required_arguments:
            value = arguments[name]
            if not isinstance(value, str) or not value.strip():
                raise GeminiAgentRouter._proposal_error("Gemini tool arguments must be non-empty strings")
            values[name] = value.strip()
        return values

    @staticmethod
    def _proposal_error(message: str) -> GeminiRoutingError:
        return GeminiRoutingError(
            GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR,
            message,
        )

    @staticmethod
    def _provider_error(
        error: Exception,
        diagnostic: GeminiProviderDiagnostic | None = None,
    ) -> GeminiRoutingError:
        class_names = {kind.__name__ for kind in type(error).__mro__}
        status = diagnostic.http_status if diagnostic else None
        if not isinstance(status, int):
            candidate = getattr(error, "status_code", getattr(error, "code", None))
            status = candidate if isinstance(candidate, int) else None
        attempts = diagnostic.attempt_count if diagnostic else None
        transient = (
            "ConnectError" in class_names
            or isinstance(error, TimeoutError)
            or bool(class_names.intersection(
                {"TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
            ))
            or status == 429
            or status == 499
            or (isinstance(status, int) and 500 <= status <= 599)
        )
        if isinstance(error, TimeoutError) or class_names.intersection(
            {"TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
        ):
            return GeminiRoutingError(
                GeminiRoutingFailureCategory.PROVIDER_TIMEOUT,
                "Gemini routing provider timed out.",
                http_status=status,
                attempt_count=attempts,
                transient=transient,
            )
        if isinstance(status, int) or class_names.intersection(
            {"APIError", "ClientError", "ServerError", "HTTPStatusError", "BadRequestError"}
        ):
            return GeminiRoutingError(
                GeminiRoutingFailureCategory.PROVIDER_HTTP_ERROR,
                "Gemini routing provider returned an HTTP error.",
                http_status=status,
                attempt_count=attempts,
                transient=transient,
            )
        return GeminiRoutingError(
            GeminiRoutingFailureCategory.PROVIDER_ERROR,
            "Gemini routing provider failed.",
            http_status=status,
            attempt_count=attempts,
            transient=transient,
        )

    def _declarations(self) -> list[dict[str, Any]]:
        declarations = [
            {
                "type": "function", "name": "route_to_company_knowledge",
                "description": "Use for requests that ask for company information. This never performs an action.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function", "name": "request_clarification",
                "description": "Use when an explicit action lacks required information. Do not guess it.",
                "parameters": {
                    "type": "object", "properties": {"clarification_question": {"type": "string"}},
                    "required": ["clarification_question"], "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "route_unsupported_action",
                "description": "Use for an explicit action that none of the declared tools supports.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]
        declarations.extend(
            {
                "type": "function", "name": specification.name,
                "description": specification.description,
                "parameters": specification.argument_schema,
            }
            for specification in self.specifications.values()
        )
        return declarations
