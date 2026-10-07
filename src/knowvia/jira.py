from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.parse import urlparse

from knowvia.agent_models import ToolResult
from knowvia.agent_tools import ToolSpecification, ToolValidationError


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    user_email: str
    api_token: str
    project_key: str
    issue_type: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_environment(cls) -> "JiraConfig":
        names = ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY", "JIRA_ISSUE_TYPE")
        missing = [name for name in names if not os.getenv(name, "").strip()]
        if missing:
            raise ValueError(f"Missing Jira configuration: {', '.join(missing)}")
        config = cls(
            base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
            user_email=os.environ["JIRA_USER_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
            project_key=os.environ["JIRA_PROJECT_KEY"],
            issue_type=os.environ["JIRA_ISSUE_TYPE"],
            timeout_seconds=float(os.getenv("JIRA_TIMEOUT_SECONDS", "15")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("JIRA_BASE_URL must be an HTTPS site URL without credentials")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", self.project_key):
            raise ValueError("JIRA_PROJECT_KEY has an invalid format")
        if not self.issue_type.strip() or self.timeout_seconds <= 0:
            raise ValueError("Jira issue type and timeout must be valid")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str


class JiraHttpTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, object], headers: dict[str, str], timeout: float) -> HttpResponse: ...


class UrllibJiraTransport:
    def post_json(self, url: str, payload: dict[str, object], headers: dict[str, str], timeout: float) -> HttpResponse:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, error.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ConnectionError("Jira request failed before a reliable response was received") from None


class JiraTicketTool:
    specification = ToolSpecification(
        name="create_it_ticket",
        description="Propose creating a Jira Cloud issue for an explicit employee IT support request.",
        required_arguments=("title", "description"),
        argument_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Concise summary of the IT problem."},
                "description": {"type": "string", "description": "Employee-provided details of the IT problem."},
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        },
        requires_confirmation=True,
    )

    def __init__(self, config: JiraConfig, transport: JiraHttpTransport | None = None) -> None:
        config.validate()
        self.config = config
        self.transport = transport or UrllibJiraTransport()

    def validate_arguments(self, arguments: Mapping[str, object]) -> dict[str, str]:
        if set(arguments) != {"title", "description"}:
            raise ToolValidationError("Jira ticket requires exactly title and description")
        values: dict[str, str] = {}
        for name, maximum in (("title", 255), ("description", 5000)):
            value = arguments[name]
            if not isinstance(value, str) or not value.strip():
                raise ToolValidationError(f"Argument '{name}' must be a non-empty string")
            value = value.strip()
            if len(value) > maximum:
                raise ToolValidationError(f"Argument '{name}' is too long")
            values[name] = value
        return values

    def execute(self, arguments: Mapping[str, str]) -> ToolResult:
        values = self.validate_arguments(arguments)
        payload = {
            "fields": {
                "project": {"key": self.config.project_key},
                "issuetype": {"name": self.config.issue_type},
                "summary": values["title"],
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": values["description"]}]}],
                },
            }
        }
        credential = base64.b64encode(
            f"{self.config.user_email}:{self.config.api_token}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Accept": "application/json", "Content-Type": "application/json",
            "Authorization": f"Basic {credential}",
        }
        try:
            response = self.transport.post_json(
                f"{self.config.base_url}/rest/api/3/issue", payload, headers,
                self.config.timeout_seconds,
            )
        except ConnectionError:
            return self._failure("Jira did not return a reliable response; the outcome is unknown.", "UNKNOWN_EXECUTION_OUTCOME")

        if response.status == 201:
            try:
                body = json.loads(response.body)
                issue_key, issue_id = str(body["key"]), str(body["id"])
            except (json.JSONDecodeError, KeyError, TypeError):
                return self._failure("Jira returned an invalid success response.", "INVALID_RESPONSE")
            return ToolResult(
                success=True, tool_name=self.specification.name,
                message=f"Created Jira issue {issue_key}.",
                data={
                    "issue_key": issue_key, "issue_id": issue_id, "status": "CREATED",
                    "url": f"{self.config.base_url}/browse/{issue_key}",
                },
            )
        category = {
            400: "VALIDATION", 401: "AUTHENTICATION", 403: "PERMISSION",
            404: "CONFIGURATION", 422: "VALIDATION", 429: "RATE_LIMIT",
        }.get(response.status, "SERVER" if response.status >= 500 else "JIRA_ERROR")
        messages = {
            "AUTHENTICATION": "Jira authentication failed.",
            "PERMISSION": "Jira denied permission to create the issue.",
            "RATE_LIMIT": "Jira rate-limited the request.",
            "VALIDATION": "Jira rejected the configured issue fields.",
            "CONFIGURATION": "The configured Jira resource was not found.",
            "SERVER": "Jira is currently unavailable.",
        }
        return self._failure(messages.get(category, "Jira rejected the request."), category)

    def _failure(self, message: str, category: str) -> ToolResult:
        return ToolResult(False, self.specification.name, message, error=message, error_category=category)
